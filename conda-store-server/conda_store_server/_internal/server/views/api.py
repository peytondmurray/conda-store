# Copyright (c) conda-store development team. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

import datetime
from functools import wraps
from typing import Any, Callable, Dict, List, Optional

import pydantic
import yaml
from celery.result import AsyncResult
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from conda_store_server import __version__, api
from conda_store_server._internal import orm, schema
from conda_store_server._internal.environment import filter_environments
from conda_store_server._internal.server import dependencies
from conda_store_server.conda_store import CondaStore
from conda_store_server.exception import CondaStoreError
from conda_store_server.server import schema as auth_schema
from conda_store_server.server.auth import Authentication
from conda_store_server.server.schema import AuthenticationToken, Permissions

router_api = APIRouter(
    tags=["api"],
    prefix="/api/v1",
)


def filter_distinct_on(
    query,
    distinct_on: List[str] = [],
    allowed_distinct_ons: Dict = {},
    default_distinct_on: List[str] = [],
):
    distinct_on = distinct_on or default_distinct_on
    distinct_on = [
        allowed_distinct_ons[d] for d in distinct_on if d in allowed_distinct_ons
    ]

    if distinct_on:
        return distinct_on, query.distinct(*distinct_on)
    return distinct_on, query


def get_sorts(
    order: str,
    sort_by: List[str] = [],
    allowed_sort_bys: Dict = {},
    required_sort_bys: List = [],
    default_sort_by: List = [],
    default_order: str = "asc",
):
    sort_by = sort_by or default_sort_by
    sort_by = [allowed_sort_bys[s] for s in sort_by if s in allowed_sort_bys]

    # required_sort_bys is needed when sorting is used with distinct
    # query see "SELECT DISTINCT ON expressions must match initial
    # ORDER BY expressions"
    if required_sort_bys != sort_by[: len(required_sort_bys)]:
        sort_by = required_sort_bys + sort_by

    if order not in {"asc", "desc"}:
        order = default_order

    order_mapping = {"asc": lambda c: c.asc(), "desc": lambda c: c.desc()}
    return [order_mapping[order](k) for k in sort_by]


def paginated_api_response(
    query,
    paginated_args,
    object_schema,
    sorts: List = [],
    exclude=None,
    allowed_sort_bys: Dict = {},
    required_sort_bys: List = [],
    default_sort_by: List = [],
    default_order: str = "asc",
):
    sorts = get_sorts(
        order=paginated_args["order"],
        sort_by=paginated_args["sort_by"],
        allowed_sort_bys=allowed_sort_bys,
        required_sort_bys=required_sort_bys,
        default_sort_by=default_sort_by,
        default_order=default_order,
    )

    count = query.count()
    query = (
        query.order_by(*sorts)
        .limit(paginated_args["limit"])
        .offset(paginated_args["offset"])
    )
    return {
        "status": "ok",
        "data": [
            object_schema.model_validate(_).model_dump(exclude=exclude)
            for _ in query.all()
        ],
        "page": (paginated_args["offset"] // paginated_args["limit"]) + 1,
        "size": paginated_args["limit"],
        "count": count,
    }


def deprecated(sunset_date: datetime.date) -> Callable:
    """Add deprecation headers to a HTTP request and response.

    This will include the deprecation date on the request object,
    which will then be picked up by the conda_store_middleware
    to add the deprecation date to the response object.

    See the conda-store backwards compatibility policy for appropriate use of
    deprecations https://conda.store/community/policies/backwards-compatibility.

    Note that decorated functions _must_ include the request parameter.
    This is not an elegant way of doing this, but FastAPI has no other
    way of achieving the same effect without confusing its request/response
    inference machinery, which relies on the type annotations of the
    routes.

    Parameters
    ----------
    sunset_date : datetime.date
        the date that the endpoint will have it's functionality removed

    Returns
    -------
    Callable
        Decorator which wraps an endpoint
    """

    def decorator(func):
        @wraps(func)
        async def add_deprecated_headers(request: Request, *args, **kwargs):
            # It's not possible to add the deprecation headers to the
            # output of `func(*args, **kwargs)`, since that may be a
            # simple dict object, not a Response
            request.state.deprecation_date = sunset_date.strftime(
                "%a, %d %b %Y 00:00:00 UTC"
            )
            result = await func(*args, request=request, **kwargs)
            return result

        return add_deprecated_headers

    return decorator


@router_api.get(
    "/",
    response_model=schema.APIGetStatus,
)
async def api_status():
    return {"status": "ok", "data": {"version": __version__}}


@router_api.get(
    "/permission/",
    response_model=schema.APIGetPermission,
)
async def api_get_permissions(
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    entity=Depends(dependencies.get_entity),
):
    authenticated = entity is not None
    entity_binding_roles = auth.authorization.get_entity_bindings(entity)

    entity_binding_permissions = auth.authorization.get_entity_binding_permissions(
        entity
    )

    # convert Dict[str, set[enum]] -> Dict[str, List[str]]
    # to be json serializable
    entity_binding_permissions = {
        entity_arn: sorted([_.value for _ in entity_permissions])
        for entity_arn, entity_permissions in entity_binding_permissions.items()
    }

    return {
        "status": "ok",
        "data": {
            "authenticated": authenticated,
            "primary_namespace": (
                entity.primary_namespace
                if authenticated
                else conda_store.config.default_namespace
            ),
            "entity_permissions": entity_binding_permissions,
            "entity_roles": entity_binding_roles,
            "expiration": entity.exp if authenticated else None,
        },
    }


@router_api.get(
    "/usage/",
    response_model=schema.APIGetUsage,
)
async def api_get_usage(
    request: Request,
    auth=Depends(dependencies.get_auth),
    entity=Depends(dependencies.get_entity),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        namespace_usage_metrics = auth.filter_namespaces(
            entity, api.get_namespace_metrics(db)
        )

        data = {}
        for namespace, num_environments, num_builds, storage in namespace_usage_metrics:
            data[namespace] = {
                "num_environments": num_environments,
                "num_builds": num_builds,
                "storage": storage,
            }

        return {
            "status": "ok",
            "data": data,
            "message": None,
        }


@router_api.post(
    "/token/",
    response_model=schema.APIPostToken,
)
async def api_post_token(
    request: Request,
    primary_namespace: Optional[str] = Body(None),
    expiration: Optional[datetime.datetime] = Body(None),
    role_bindings: Optional[Dict[str, List[str]]] = Body(None),
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    entity=Depends(dependencies.get_entity),
):
    if entity is None:
        entity = auth_schema.AuthenticationToken(
            exp=datetime.datetime.now(tz=datetime.timezone.utc)
            + datetime.timedelta(days=1),
            primary_namespace=conda_store.config.default_namespace,
            role_bindings={},
        )

    new_entity = auth_schema.AuthenticationToken(
        exp=expiration or entity.exp,
        primary_namespace=primary_namespace or entity.primary_namespace,
        role_bindings=role_bindings or auth.authorization.get_entity_bindings(entity),
    )

    if not auth.authorization.is_subset_entity_permissions(entity, new_entity):
        raise HTTPException(
            status_code=400,
            detail="Requested role_bindings are not a subset of current permissions",
        )

    if new_entity.exp > entity.exp:
        raise HTTPException(
            status_code=400,
            detail="Requested expiration of token is greater than current permissions",
        )

    return {
        "status": "ok",
        "data": {"token": auth.authentication.encrypt_token(new_entity)},
    }


@router_api.get(
    "/namespace/",
    response_model=schema.APIListNamespace,
    # don't send metadata_ and role_mappings
    response_model_exclude_defaults=True,
)
async def api_list_namespaces(
    auth=Depends(dependencies.get_auth),
    entity=Depends(dependencies.get_entity),
    paginated_args: dependencies.PaginatedArgs = Depends(
        dependencies.get_paginated_args
    ),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        orm_namespaces = auth.filter_namespaces(
            entity, api.list_namespaces(db, show_soft_deleted=False)
        )
        return paginated_api_response(
            orm_namespaces,
            paginated_args,
            schema.Namespace,
            exclude={"role_mappings", "metadata_"},
            allowed_sort_bys={
                "name": orm.Namespace.name,
            },
            default_sort_by=["name"],
        )


@router_api.get(
    "/namespace/{namespace}/",
    response_model=schema.APIGetNamespace,
)
async def api_get_namespace(
    namespace: str,
    request: Request,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request, namespace, {Permissions.NAMESPACE_READ}, require=True
        )

        namespace = api.get_namespace(db, namespace, show_soft_deleted=False)
        if namespace is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        return {
            "status": "ok",
            "data": schema.Namespace.model_validate(namespace).model_dump(),
        }


@router_api.post(
    "/namespace/{namespace}/",
    response_model=schema.APIAckResponse,
)
async def api_create_namespace(
    namespace: str,
    request: Request,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request, namespace, {Permissions.NAMESPACE_CREATE}, require=True
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm:
            raise HTTPException(status_code=409, detail="namespace already exists")

        try:
            api.create_namespace(db, namespace)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        return {"status": "ok"}


@router_api.put(
    "/namespace/{namespace}/",
    response_model=schema.APIAckResponse,
)
async def api_update_namespace(
    namespace: str,
    request: Request,
    metadata: Dict[str, Any] = None,
    role_mappings: Dict[str, List[str]] = None,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            namespace,
            {
                Permissions.NAMESPACE_UPDATE,
                Permissions.NAMESPACE_ROLE_MAPPING_CREATE,
                Permissions.NAMESPACE_ROLE_MAPPING_DELETE,
            },
            require=True,
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            api.update_namespace(db, namespace, metadata, role_mappings)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        return {"status": "ok"}


@router_api.put("/namespace/{namespace}/metadata", response_model=schema.APIAckResponse)
async def api_update_namespace_metadata(
    namespace: str,
    request: Request,
    metadata: Dict[str, Any] = None,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            namespace,
            {
                Permissions.NAMESPACE_UPDATE,
            },
            require=True,
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            api.update_namespace_metadata(db, namespace, metadata_=metadata)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        return {"status": "ok"}


@router_api.get("/namespace/{namespace}/roles", response_model=schema.APIResponse)
async def api_get_namespace_roles(
    namespace: str,
    request: Request,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            namespace,
            {
                Permissions.NAMESPACE_READ,
                Permissions.NAMESPACE_ROLE_MAPPING_READ,
            },
            require=True,
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            data = api.get_namespace_roles(db, namespace)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        return {
            "status": "ok",
            "data": [x.model_dump() for x in data],
        }


@router_api.delete("/namespace/{namespace}/roles", response_model=schema.APIAckResponse)
async def api_delete_namespace_roles(
    namespace: str,
    request: Request,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            namespace,
            {
                Permissions.NAMESPACE_READ,
                Permissions.NAMESPACE_ROLE_MAPPING_DELETE,
            },
            require=True,
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            api.delete_namespace_roles(db, namespace)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        return {"status": "ok"}


@router_api.get("/namespace/{namespace}/role", response_model=schema.APIResponse)
async def api_get_namespace_role(
    namespace: str,
    request: Request,
    other_namespace: str,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            namespace,
            {
                Permissions.NAMESPACE_READ,
                Permissions.NAMESPACE_ROLE_MAPPING_READ,
            },
            require=True,
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            data = api.get_namespace_role(db, namespace, other=other_namespace)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        if data is None:
            raise HTTPException(status_code=404, detail="failed to find role")
        return {
            "status": "ok",
            "data": data.model_dump(),
        }


@router_api.post("/namespace/{namespace}/role", response_model=schema.APIAckResponse)
async def api_create_namespace_role(
    namespace: str,
    request: Request,
    role_mapping: schema.APIPostNamespaceRole,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            namespace,
            {
                Permissions.NAMESPACE_READ,
                Permissions.NAMESPACE_ROLE_MAPPING_CREATE,
            },
            require=True,
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            api.create_namespace_role(
                db,
                namespace,
                other=role_mapping.other_namespace,
                role=role_mapping.role,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        return {"status": "ok"}


@router_api.put("/namespace/{namespace}/role", response_model=schema.APIAckResponse)
async def api_update_namespace_role(
    namespace: str,
    request: Request,
    role_mapping: schema.APIPutNamespaceRole,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            namespace,
            {
                Permissions.NAMESPACE_READ,
                Permissions.NAMESPACE_ROLE_MAPPING_UPDATE,
            },
            require=True,
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            api.update_namespace_role(
                db,
                namespace,
                other=role_mapping.other_namespace,
                role=role_mapping.role,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        return {"status": "ok"}


@router_api.delete("/namespace/{namespace}/role", response_model=schema.APIAckResponse)
async def api_delete_namespace_role(
    namespace: str,
    request: Request,
    role_mapping: schema.APIDeleteNamespaceRole,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            namespace,
            {
                Permissions.NAMESPACE_READ,
                Permissions.NAMESPACE_ROLE_MAPPING_DELETE,
            },
            require=True,
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            api.delete_namespace_role(db, namespace, other=role_mapping.other_namespace)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        db.commit()
        return {"status": "ok"}


@router_api.delete("/namespace/{namespace}/", response_model=schema.APIAckResponse)
async def api_delete_namespace(
    namespace: str,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request, namespace, {Permissions.NAMESPACE_DELETE}, require=True
        )

        namespace_orm = api.get_namespace(db, namespace)
        if namespace_orm is None:
            raise HTTPException(status_code=404, detail="namespace does not exist")

        try:
            conda_store.delete_namespace(db, namespace)
        except CondaStoreError as e:
            raise HTTPException(status_code=400, detail=e.message)

        return {"status": "ok"}


@router_api.get(
    "/environment/", response_model=schema.APIListEnvironment, deprecated=True
)
@deprecated(sunset_date=datetime.date(2025, 3, 17))
async def api_list_environments_v1(
    request: Request,
    auth: Authentication = Depends(dependencies.get_auth),
    conda_store: CondaStore = Depends(dependencies.get_conda_store),
    entity: AuthenticationToken = Depends(dependencies.get_entity),
    paginated_args: dependencies.PaginatedArgs = Depends(
        dependencies.get_paginated_args
    ),
    artifact: Optional[schema.BuildArtifactType] = None,
    jwt: Optional[str] = None,
    name: Optional[str] = None,
    namespace: Optional[str] = None,
    packages: Optional[List[str]] = Query([]),
    search: Optional[str] = None,
    status: Optional[schema.BuildStatus] = None,
):
    """Retrieve a list of environments.

    Parameters
    ----------
    auth : Authentication
        Authentication instance for the request. Used to get role bindings
        and filter environments returned to those visible by the user making
        the request
    entity : AuthenticationToken
        Token of the user making the request
    paginated_args : PaginatedArgs
        Arguments for controlling pagination of the response
    conda_store : app.CondaStore
        The running conda store application
    search : Optional[str]
        If specified, filter by environment names or namespace names containing the
        search term
    namespace : Optional[str]
        If specified, filter by environments in the given namespace
    name : Optional[str]
        If specified, filter by environments with the given name
    status : Optional[schema.BuildStatus]
        If specified, filter by environments with the given status
    packages : Optional[List[str]]
        If specified, filter by environments containing the given package name(s)
    artifact : Optional[schema.BuildArtifactType]
        If specified, filter by environments with the given BuildArtifactType
    jwt : Optional[auth_schema.AuthenticationToken]
        If specified, retrieve only the environments accessible to this token; that is,
        only return environments that the user has 'admin', 'editor', and 'viewer'
        role bindings for.

    Returns
    -------
    Dict
        Paginated JSON response containing the requested environments. Uses limit/offset-based
        pagination.

    """
    with conda_store.get_db() as db:
        if jwt:
            # Fetch the environments visible to the supplied token
            role_bindings = auth.entity_bindings(
                AuthenticationToken.model_validate(
                    auth.authentication.decrypt_token(jwt)
                )
            )
        else:
            role_bindings = None

        orm_environments = api.list_environments(
            db,
            search=search,
            namespace=namespace,
            name=name,
            status=status,
            packages=packages,
            artifact=artifact,
            show_soft_deleted=False,
            role_bindings=role_bindings,
        )

        # Filter by environments that the user who made the query has access to
        orm_environments = filter_environments(
            query=orm_environments,
            role_bindings=auth.entity_bindings(entity),
        )

        return paginated_api_response(
            orm_environments,
            paginated_args,
            schema.Environment,
            exclude={"current_build"},
            allowed_sort_bys={
                "namespace": orm.Namespace.name,
                "name": orm.Environment.name,
            },
            default_sort_by=["namespace", "name"],
        )


@router_api.get(
    "/environment/{namespace}/{environment_name}/",
    response_model=schema.APIGetEnvironment,
)
async def api_get_environment(
    namespace: str,
    environment_name: str,
    request: Request,
    auth: Authentication = Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            f"{namespace}/{environment_name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )

        environment = api.get_environment(
            db, namespace=namespace, name=environment_name
        )
        if environment is None:
            raise HTTPException(status_code=404, detail="environment does not exist")

        return {
            "status": "ok",
            "data": schema.Environment.model_validate(environment).model_dump(
                exclude={"current_build"}
            ),
        }


@router_api.put(
    "/environment/{namespace}/{name}/",
    response_model=schema.APIAckResponse,
)
async def api_update_environment_build(
    namespace: str,
    name: str,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    build_id: int = Body(None, embed=True),
    description: str = Body(None, embed=True),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            f"{namespace}/{name}",
            {Permissions.ENVIRONMENT_UPDATE},
            require=True,
        )

        try:
            if build_id is not None:
                conda_store.update_environment_build(db, namespace, name, build_id)

            if description is not None:
                conda_store.update_environment_description(
                    db, namespace, name, description
                )

        except CondaStoreError as e:
            raise HTTPException(status_code=400, detail=e.message)

        return {"status": "ok"}


@router_api.delete(
    "/environment/{namespace}/{name}/",
    response_model=schema.APIAckResponse,
)
async def api_delete_environment(
    namespace: str,
    name: str,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        auth.authorize_request(
            request,
            f"{namespace}/{name}",
            {Permissions.ENVIRONMENT_DELETE},
            require=True,
        )

        try:
            conda_store.delete_environment(db, namespace, name)
        except CondaStoreError as e:
            raise HTTPException(status_code=400, detail=e.message)

        return {"status": "ok"}


@router_api.get(
    "/specification/",
)
async def api_get_specification(
    request: Request,
    channel: List[str] = Query([]),
    conda: List[str] = Query([]),
    pip: List[str] = Query([]),
    format: schema.APIGetSpecificationFormat = Query(
        schema.APIGetSpecificationFormat.YAML
    ),
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    entity=Depends(dependencies.get_entity),
):
    with conda_store.get_db() as db:
        # GET is used for the solve to make this endpoint easily
        # cachable
        if pip:
            conda.append({"pip": pip})

        specification = schema.CondaSpecification(
            name="conda-store-solve",
            channels=channel,
            dependencies=conda,
        )

        try:
            task, solve_id = conda_store.register_solve(db, specification)
            AsyncResult(task).wait()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))

        solve = api.get_solve(db, solve_id)

        return {"solve": solve.package_builds}


@router_api.post(
    "/specification/",
    response_model=schema.APIPostSpecification,
)
async def api_post_specification(
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    entity=Depends(dependencies.get_entity),
    specification: str = Body(""),
    namespace: Optional[str] = Body(None),
    is_lockfile: Optional[bool] = Body(False, embed=True),
    environment_name: Optional[str] = Body("", embed=True),
    environment_description: Optional[str] = Body("", embed=True),
):
    with conda_store.get_db() as db:
        permissions = {Permissions.ENVIRONMENT_CREATE}

        default_namespace = (
            entity.primary_namespace if entity else conda_store.config.default_namespace
        )

        namespace_name = namespace or default_namespace
        namespace = api.get_namespace(db, namespace_name)
        if namespace is None:
            permissions.add(Permissions.NAMESPACE_CREATE)

        try:
            specification = yaml.safe_load(specification)
            if is_lockfile:
                lockfile_spec = {
                    "name": environment_name,
                    "description": environment_description,
                    "lockfile": specification,
                }
                specification = schema.LockfileSpecification.model_validate(
                    lockfile_spec
                )
            else:
                specification = schema.CondaSpecification.model_validate(specification)
        except yaml.error.YAMLError:
            raise HTTPException(status_code=400, detail="Unable to parse. Invalid YAML")
        except CondaStoreError as e:
            raise HTTPException(status_code=400, detail="\n".join(e.args[0]))
        except pydantic.ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))

        auth.authorize_request(
            request,
            f"{namespace_name}/{specification.name}",
            permissions,
            require=True,
        )

        try:
            build_id = conda_store.register_environment(
                db,
                specification,
                namespace_name,
                force=True,
                is_lockfile=is_lockfile,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0]))
        except CondaStoreError as e:
            raise HTTPException(status_code=400, detail=str(e.message))

        return {"status": "ok", "data": {"build_id": build_id}}


@router_api.get("/build/", response_model=schema.APIListBuild)
async def api_list_builds(
    status: Optional[schema.BuildStatus] = None,
    packages: Optional[List[str]] = Query([]),
    artifact: Optional[schema.BuildArtifactType] = None,
    environment_id: Optional[int] = None,
    name: Optional[str] = None,
    namespace: Optional[str] = None,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    entity=Depends(dependencies.get_entity),
    paginated_args=Depends(dependencies.get_paginated_args),
):
    with conda_store.get_db() as db:
        orm_builds = auth.filter_builds(
            entity,
            api.list_builds(
                db,
                status=status,
                packages=packages,
                artifact=artifact,
                environment_id=environment_id,
                name=name,
                namespace=namespace,
                show_soft_deleted=True,
            ),
        )
        return paginated_api_response(
            orm_builds,
            paginated_args,
            schema.Build,
            exclude={"specification", "packages", "build_artifacts"},
            allowed_sort_bys={
                "id": orm.Build.id,
                "started_on": orm.Build.started_on,
                "scheduled_on": orm.Build.scheduled_on,
                "ended_on": orm.Build.ended_on,
            },
            default_sort_by=["id"],
        )


@router_api.get("/build/{build_id}/", response_model=schema.APIGetBuild)
async def api_get_build(
    build_id: int,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        if build is None:
            raise HTTPException(status_code=404, detail="build id does not exist")

        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )

        return {
            "status": "ok",
            "data": schema.Build.model_validate(build).model_dump(exclude={"packages"}),
        }


@router_api.put(
    "/build/{build_id}/",
    response_model=schema.APIPostSpecification,
)
async def api_put_build(
    build_id: int,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        if build is None:
            raise HTTPException(status_code=404, detail="build id does not exist")

        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.ENVIRONMENT_UPDATE},
            require=True,
        )

        try:
            new_build = conda_store.create_build(
                db, build.environment_id, build.specification.sha256
            )
        except CondaStoreError as e:
            raise HTTPException(status_code=400, detail=e.message)

        return {
            "status": "ok",
            "message": "rebuild triggered",
            "data": {"build_id": new_build.id},
        }


@router_api.put(
    "/build/{build_id}/cancel/",
    response_model=schema.APIAckResponse,
)
async def api_put_build_cancel(
    build_id: int,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        if build is None:
            raise HTTPException(status_code=404, detail="build id does not exist")

        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.BUILD_CANCEL},
            require=True,
        )

        if conda_store.celery_app.control.inspect().ping() is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "conda-store celery broker does not support task cancelation. "
                    "Use redis or rabbitmq message queues. "
                    "See docs for a more detailed explanation"
                ),
            )

        conda_store.celery_app.control.revoke(
            [
                f"build-{build_id}-conda-env-export",
                f"build-{build_id}-conda-pack",
                f"build-{build_id}-constructor-installer",
                f"build-{build_id}-environment",
            ],
            terminate=True,
            signal="SIGTERM",
        )

        from conda_store_server._internal.worker import tasks

        # Waits 5 seconds to ensure enough time for the task to actually be
        # canceled
        tasks.task_cleanup_builds.si(
            build_ids=[build_id],
            reason=f"""
    build {build_id} marked as CANCELED due to being canceled from the REST API
    """,
            is_canceled=True,
        ).apply_async(countdown=5)

        return {
            "status": "ok",
            "message": f"build {build_id} canceled",
        }


@router_api.delete(
    "/build/{build_id}/",
    response_model=schema.APIAckResponse,
)
async def api_delete_build(
    build_id: int,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        if build is None:
            raise HTTPException(status_code=404, detail="build id does not exist")

        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.BUILD_DELETE},
            require=True,
        )

        try:
            conda_store.delete_build(db, build_id)
        except CondaStoreError as e:
            raise HTTPException(status_code=400, detail=e.message)

        return {"status": "ok"}


@router_api.get(
    "/build/{build_id}/packages/",
    response_model=schema.APIListCondaPackage,
)
async def api_get_build_packages(
    build_id: int,
    request: Request,
    search: Optional[str] = None,
    exact: Optional[str] = None,
    build: Optional[str] = None,
    auth=Depends(dependencies.get_auth),
    conda_store=Depends(dependencies.get_conda_store),
    paginated_args=Depends(dependencies.get_paginated_args),
):
    with conda_store.get_db() as db:
        build_orm = api.get_build(db, build_id)
        if build_orm is None:
            raise HTTPException(status_code=404, detail="build id does not exist")

        auth.authorize_request(
            request,
            f"{build_orm.environment.namespace.name}/{build_orm.environment.name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )
        orm_packages = api.get_build_packages(
            db, build_orm.id, search=search, exact=exact, build=build
        )
        return paginated_api_response(
            orm_packages,
            paginated_args,
            schema.CondaPackage,
            allowed_sort_bys={
                "channel": orm.CondaChannel.name,
                "name": orm.CondaPackage.name,
            },
            default_sort_by=["channel", "name"],
            exclude={"channel": {"last_update"}},
        )


@router_api.get("/build/{build_id}/logs/")
async def api_get_build_logs(
    build_id: int,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        if build is None:
            raise HTTPException(status_code=404, detail="build id does not exist")

        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )

        return RedirectResponse(conda_store.storage.get_url(build.log_key))


@router_api.get(
    "/channel/",
    response_model=schema.APIListCondaChannel,
)
async def api_list_channels(
    conda_store=Depends(dependencies.get_conda_store),
    paginated_args=Depends(dependencies.get_paginated_args),
):
    with conda_store.get_db() as db:
        orm_channels = api.list_conda_channels(db)
        return paginated_api_response(
            orm_channels,
            paginated_args,
            schema.CondaChannel,
            allowed_sort_bys={"name": orm.CondaChannel.name},
            default_sort_by=["name"],
        )


@router_api.get(
    "/package/",
    response_model=schema.APIListCondaPackage,
)
async def api_list_packages(
    search: Optional[str] = None,
    exact: Optional[str] = None,
    build: Optional[str] = None,
    paginated_args=Depends(dependencies.get_paginated_args),
    conda_store=Depends(dependencies.get_conda_store),
    distinct_on: List[str] = Query([]),
):
    with conda_store.get_db() as db:
        orm_packages = api.list_conda_packages(
            db, search=search, exact=exact, build=build
        )
        required_sort_bys, distinct_orm_packages = filter_distinct_on(
            orm_packages,
            distinct_on=distinct_on,
            allowed_distinct_ons={
                "channel": orm.CondaChannel.name,
                "name": orm.CondaPackage.name,
                "version": orm.CondaPackage.version,
            },
        )
        return paginated_api_response(
            distinct_orm_packages,
            paginated_args,
            schema.CondaPackage,
            allowed_sort_bys={
                "channel": orm.CondaChannel.name,
                "name": orm.CondaPackage.name,
                "version": orm.CondaPackage.version,
            },
            default_sort_by=["channel", "name", "version", "build"],
            required_sort_bys=required_sort_bys,
            exclude={"channel": {"last_update"}},
        )


@router_api.get("/build/{build_id}/yaml/")
async def api_get_build_yaml(
    build_id: int,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        if build is None:
            raise HTTPException(status_code=404, detail="build id does not exist")

        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )
        return RedirectResponse(conda_store.storage.get_url(build.conda_env_export_key))


@router_api.get(
    "/environment/{namespace}/{environment_name}/conda-lock.yml",
    response_class=PlainTextResponse,
)
@router_api.get(
    "/environment/{namespace}/{environment_name}/conda-lock.yaml",
    response_class=PlainTextResponse,
)
@router_api.get(
    "/environment/{namespace}/{environment_name}/lockfile/",
    response_class=PlainTextResponse,
)
@router_api.get("/build/{build_id}/conda-lock.yml", response_class=PlainTextResponse)
@router_api.get(
    "/build/{build_id}/conda-lock.yaml",
    name="api_get_build_conda_lock_file",
    response_class=PlainTextResponse,
)
@router_api.get("/build/{build_id}/lockfile/", response_class=PlainTextResponse)
async def api_get_build_lockfile(
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    namespace: str = None,
    environment_name: str = None,
    build_id: int = None,
):
    with conda_store.get_db() as db:
        if build_id is None:
            environment = api.get_environment(
                db, namespace=namespace, name=environment_name
            )
            if environment is None:
                raise HTTPException(
                    status_code=404, detail="environment does not exist"
                )
            build = environment.current_build
        else:
            build = api.get_build(db, build_id)

        if build is None:
            raise HTTPException(status_code=404, detail="build id does not exist")

        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )

        build_artifacts = api.list_build_artifacts(
            db,
            build_id=build_id,
            included_artifact_types=[schema.BuildArtifactType.LOCKFILE],
        )
        # Checks if this is a legacy-style (v0.4.15) build, with a lockfile
        # generated by conda-store (newer builds use conda-lock)
        # https://github.com/conda-incubator/conda-store/issues/544
        if any(ba.key == "" for ba in build_artifacts):
            return api.get_build_lockfile_legacy(db, build_id)

        return RedirectResponse(conda_store.storage.get_url(build.conda_lock_key))


@router_api.get("/build/{build_id}/archive/")
async def api_get_build_archive(
    build_id: int,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )

        return RedirectResponse(conda_store.storage.get_url(build.conda_pack_key))


@router_api.get("/build/{build_id}/docker/", deprecated=True)
@deprecated(sunset_date=datetime.date(2025, 3, 17))
async def api_get_build_docker_image_url(
    request: Request,
    build_id: int,
    conda_store=Depends(dependencies.get_conda_store),
    server=Depends(dependencies.get_server),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )

        if build.has_docker_manifest:
            url = f"{server.registry_external_url}/{build.environment.namespace.name}/{build.environment.name}:{build.build_key}"
            return PlainTextResponse(url)

        else:
            content = {
                "status": "error",
                "message": f"Build {build_id} doesn't have a docker manifest",
            }
            return JSONResponse(
                status_code=400,
                content=content,
            )


@router_api.get("/build/{build_id}/installer/")
async def api_get_build_installer(
    build_id: int,
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
):
    with conda_store.get_db() as db:
        build = api.get_build(db, build_id)
        auth.authorize_request(
            request,
            f"{build.environment.namespace.name}/{build.environment.name}",
            {Permissions.ENVIRONMENT_READ},
            require=True,
        )

        if build.has_constructor_installer:
            return RedirectResponse(
                conda_store.storage.get_url(build.constructor_installer_key)
            )

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Build {build_id} doesn't have an installer",
            )


@router_api.get(
    "/setting/",
    response_model=schema.APIGetSetting,
)
@router_api.get(
    "/setting/{namespace}/",
    response_model=schema.APIGetSetting,
)
@router_api.get(
    "/setting/{namespace}/{environment_name}/",
    response_model=schema.APIGetSetting,
)
async def api_get_settings(
    request: Request,
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    namespace: str = None,
    environment_name: str = None,
):
    if namespace is None:
        arn = ""
    elif environment_name is None:
        arn = namespace
    else:
        arn = f"{namespace}/{environment_name}"

    auth.authorize_request(
        request,
        arn,
        {Permissions.SETTING_READ},
        require=True,
    )

    return {
        "status": "ok",
        "data": conda_store.get_settings(namespace, environment_name).model_dump(),
        "message": None,
    }


@router_api.put(
    "/setting/",
    response_model=schema.APIPutSetting,
)
@router_api.put(
    "/setting/{namespace}/",
    response_model=schema.APIPutSetting,
)
@router_api.put(
    "/setting/{namespace}/{environment_name}/",
    response_model=schema.APIPutSetting,
)
async def api_put_settings(
    request: Request,
    data: Dict[str, Any],
    conda_store=Depends(dependencies.get_conda_store),
    auth=Depends(dependencies.get_auth),
    namespace: str = None,
    environment_name: str = None,
):
    if namespace is None:
        arn = ""
    elif environment_name is None:
        arn = namespace
    else:
        arn = f"{namespace}/{environment_name}"

    auth.authorize_request(
        request,
        arn,
        {Permissions.SETTING_UPDATE},
        require=True,
    )

    try:
        conda_store.set_settings(namespace, environment_name, data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "ok",
        "data": None,
        "message": f"global setting keys {list(data.keys())} updated",
    }
