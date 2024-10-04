# Copyright (c) conda-store development team. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

from fastapi import Depends, Request

from conda_store_server._internal.schema import AuthenticationToken
from conda_store_server.app import CondaStore
from conda_store_server.server.auth import Authentication


async def get_conda_store(request: Request) -> CondaStore:
    return request.state.conda_store


async def get_server(request: Request):
    return request.state.server


async def get_auth(request: Request) -> Authentication:
    return request.state.authentication


async def get_entity(request: Request, auth=Depends(get_auth)) -> AuthenticationToken:
    return auth.authenticate_request(request)


async def get_templates(request: Request):
    return request.state.templates
