# Copyright (c) conda-store development team. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

import pytest

from conda_store_server import api
from conda_store_server._internal.orm import NamespaceRoleMapping
from conda_store_server.exception import BuildPathError


@pytest.fixture
def populated_db(db):
    """A database fixture populated with 4 envs in 3 namespaces."""
    description = "Hello World"
    namespace1 = api.create_namespace(db, name="pytest1")
    namespace2 = api.create_namespace(db, name="pytest2")
    namespace3 = api.create_namespace(db, name="pytest3")
    db.commit()

    api.create_environment(
        db,
        namespace_id=namespace1.id,
        name="env1",
        description=description,
    )
    api.create_environment(
        db,
        namespace_id=namespace2.id,
        name="env2",
        description=description,
    )
    api.create_environment(
        db,
        namespace_id=namespace3.id,
        name="env3",
        description=description,
    )
    api.create_environment(
        db,
        namespace_id=namespace3.id,
        name="foo",
        description=description,
    )
    db.commit()
    return db


def test_namespace_crud(db):
    namespace_name = "pytest-namespace"

    # starts with no namespaces for test
    assert len(api.list_namespaces(db).all()) == 0

    # create namespace
    namespace = api.create_namespace(db, name=namespace_name)
    db.commit()

    # check that only one namespace exists
    assert len(api.list_namespaces(db).all()) == 1

    # check that ensuring a namespace doesn't create a new one
    api.ensure_namespace(db, name=namespace_name)

    assert len(api.list_namespaces(db).all()) == 1

    # check that getting namespace works
    namespace = api.get_namespace(db, id=namespace.id)
    assert namespace is not None

    # check that deleting a namespace works
    api.delete_namespace(db, id=namespace.id)
    db.commit()

    assert len(api.list_namespaces(db).all()) == 0

    # check that ensuring a namespace doesn't creates one
    api.ensure_namespace(db, name=namespace_name)

    assert len(api.list_namespaces(db).all()) == 1


@pytest.mark.parametrize(
    ("role_bindings", "expected_envs"),
    [
        ({"*/env1": ["viewer"]}, ["env1"]),
        ({"pytest2/*": ["viewer"], "e*/e*": ["admin"]}, ["env2"]),
        ({"pytest3/env3": ["viewer"]}, ["env3"]),
        (
            {
                "pytest*/env*": ["viewer"],
            },
            ["env1", "env2", "env3"],
        ),
        (
            {
                "*/*": ["viewer"],
            },
            ["env1", "env2", "env3", "foo"],
        ),
    ],
)
def test_list_environments_role_bindings(populated_db, role_bindings, expected_envs):
    """Test that environments can be listed and filtered by role mappings."""
    assert len(api.list_namespaces(populated_db).all()) == 3
    assert set(
        obj.name
        for obj in api.list_environments(
            populated_db, role_bindings=role_bindings
        ).all()
    ) == set(expected_envs)


def test_namespace_role_mapping(db):
    namespace_name = "pytest-namespace"

    # starts with no namespaces for test
    assert len(api.list_namespaces(db).all()) == 0

    # create namespace
    namespace = api.create_namespace(db, name=namespace_name)
    db.commit()

    # check that only one namespace exists
    assert len(api.list_namespaces(db).all()) == 1

    # Creates role mappings with valid entity names
    db.add(
        NamespaceRoleMapping(
            namespace=namespace,
            namespace_id=namespace.id,
            entity="org/*",
            role="editor",
        )
    )
    db.add(
        NamespaceRoleMapping(
            namespace=namespace,
            namespace_id=namespace.id,
            entity="*/team",
            role="editor",
        )
    )
    db.add(
        NamespaceRoleMapping(
            namespace=namespace,
            namespace_id=namespace.id,
            entity="org/team",
            role="editor",
        )
    )
    db.add(
        NamespaceRoleMapping(
            namespace=namespace,
            namespace_id=namespace.id,
            entity="*/*",
            role="editor",
        )
    )

    # Check that the namespace role mappings were correctly inserted;
    # this will fail if they were not correctly instantiated.
    db.commit()

    # Create a role mapping with a failing entity
    with pytest.raises(Exception):
        db.add(
            NamespaceRoleMapping(
                namespace=namespace,
                namespace_id=namespace.id,
                entity="invalid_entity_name",
            )
        )


@pytest.mark.parametrize(
    "editor_role",
    [
        "editor",
        "developer",
    ],
)
def test_namespace_role_mapping_v2(db, editor_role):
    namespace_name = "pytest-namespace"
    other_namespace_name1 = "pytest-other-namespace1"
    other_namespace_name2 = "pytest-other-namespace2"
    other_namespace_name3 = "pytest-other-namespace3"

    # Starts with no namespaces
    assert len(api.list_namespaces(db).all()) == 0

    # Creates namespaces
    api.create_namespace(db, name=namespace_name)
    api.create_namespace(db, name=other_namespace_name1)
    api.create_namespace(db, name=other_namespace_name2)
    api.create_namespace(db, name=other_namespace_name3)
    db.commit()

    # Checks that all namespaces exist
    assert len(api.list_namespaces(db).all()) == 4

    # Creates role mappings
    api.create_namespace_role(
        db, name=namespace_name, other=other_namespace_name1, role="admin"
    )
    api.create_namespace_role(
        db, name=namespace_name, other=other_namespace_name2, role="admin"
    )
    api.create_namespace_role(
        db, name=namespace_name, other=other_namespace_name3, role="viewer"
    )
    db.commit()

    # Attempts to create a role mapping with an invalid role
    with pytest.raises(ValueError, match=r"invalid role=invalid-role"):
        api.create_namespace_role(
            db, name=namespace_name, other=other_namespace_name3, role="invalid-role"
        )
        db.commit()

    # Attempts to create a role mapping violating the uniqueness constraint
    with pytest.raises(
        Exception,
        match=(
            r"UNIQUE constraint failed: "
            r"namespace_role_mapping_v2.namespace_id, "
            r"namespace_role_mapping_v2.other_namespace_id"
        ),
    ):
        # Runs in a nested transaction since a constraint violation will cause a rollback
        with db.begin_nested():
            api.create_namespace_role(
                db, name=namespace_name, other=other_namespace_name2, role=editor_role
            )
            db.commit()

    # Updates a role mapping
    api.update_namespace_role(
        db, name=namespace_name, other=other_namespace_name2, role=editor_role
    )
    db.commit()

    # Gets all role mappings
    roles = api.get_namespace_roles(db, namespace_name)
    db.commit()
    assert len(roles) == 3

    assert roles[0].id == 1
    assert roles[0].namespace == namespace_name
    assert roles[0].other_namespace == other_namespace_name1
    assert roles[0].role == "admin"

    assert roles[1].id == 2
    assert roles[1].namespace == namespace_name
    assert roles[1].other_namespace == other_namespace_name2
    assert roles[1].role == "developer"  # always developer in the DB

    assert roles[2].id == 3
    assert roles[2].namespace == namespace_name
    assert roles[2].other_namespace == other_namespace_name3
    assert roles[2].role == "viewer"

    # Gets other role mappings
    roles = api.get_other_namespace_roles(db, other_namespace_name1)
    db.commit()
    assert len(roles) == 1
    roles = api.get_other_namespace_roles(db, namespace_name)
    db.commit()
    assert len(roles) == 0

    # Deletes one role mapping
    api.delete_namespace_role(db, name=namespace_name, other=other_namespace_name2)
    db.commit()

    # Gets all role mappings again
    roles = api.get_namespace_roles(db, namespace_name)
    db.commit()
    assert len(roles) == 2

    assert roles[0].id == 1
    assert roles[0].namespace == namespace_name
    assert roles[0].other_namespace == other_namespace_name1
    assert roles[0].role == "admin"

    assert roles[1].id == 3
    assert roles[1].namespace == namespace_name
    assert roles[1].other_namespace == other_namespace_name3
    assert roles[1].role == "viewer"

    # Deletes all role mappings
    api.delete_namespace_roles(db, name=namespace_name)
    db.commit()

    # Checks that roles were deleted
    roles = api.get_namespace_roles(db, name=namespace_name)
    db.commit()
    assert len(roles) == 0


def test_environment_crud(db):
    namespace_name = "pytest-namespace"
    environment_name = "pytest-environment"
    description = "Hello World"

    namespace = api.ensure_namespace(db, name=namespace_name)

    assert len(api.list_environments(db).all()) == 0

    # create environment
    environment = api.create_environment(
        db,
        namespace_id=namespace.id,
        name=environment_name,
        description=description,
    )
    db.commit()

    # check that only one environment exists
    assert len(api.list_environments(db).all()) == 1

    # ensure environment
    api.ensure_environment(db, name=environment_name, namespace_id=namespace.id)

    assert len(api.list_environments(db).all()) == 1

    # check that getting environment works
    environment = api.get_environment(
        db, namespace_id=namespace.id, name=environment_name
    )
    assert environment is not None


def test_get_set_keyvaluestore(db):
    setting_1 = {"a": 1, "b": 2}
    setting_2 = {"c": 1, "d": 2}
    setting_3 = {"e": 1, "f": 2}

    api.set_kvstore_key_values(db, "pytest", setting_1)
    api.set_kvstore_key_values(db, "pytest/1", setting_2)
    api.set_kvstore_key_values(db, "pytest/1/2", setting_3)

    # check get_kvstore_key_values
    assert setting_1 == api.get_kvstore_key_values(db, "pytest")
    assert setting_2 == api.get_kvstore_key_values(db, "pytest/1")
    assert setting_3 == api.get_kvstore_key_values(db, "pytest/1/2")

    # check get_kvstore_value
    assert api.get_kvstore_key(db, "pytest", "b") == 2
    assert api.get_kvstore_key(db, "pytest/1", "d") == 2
    assert api.get_kvstore_key(db, "pytest/1/2", "f") == 2

    # test updating a prefix
    api.set_kvstore_key_values(db, "pytest", setting_2)
    assert api.get_kvstore_key_values(db, "pytest") == {**setting_1, **setting_2}
    assert api.get_kvstore_key(db, "pytest", "d") == 2

    # test updating a prefix
    api.set_kvstore_key_values(db, "pytest", {"c": 999, "d": 999}, update=False)
    assert api.get_kvstore_key_values(db, "pytest") == {**setting_1, **setting_2}
    assert api.get_kvstore_key(db, "pytest", "d") == 2


def test_get_kvstore_key_dne(db):
    # db starts empty, try to get a value that does not exist
    assert api.get_kvstore_key(db, "pytest", "c") is None


def test_build_path_too_long(db, conda_store, simple_specification):
    conda_store.config.store_directory = "A" * 800
    build_id = conda_store.register_environment(
        db, specification=simple_specification, namespace="pytest"
    )
    build = api.get_build(db, build_id=build_id)
    with pytest.raises(
        BuildPathError, match=r"build_path too long: must be <= 255 characters"
    ):
        build.build_path(conda_store)
