#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# This code is part of the TrinityX software suite
# Copyright (C) 2026  ClusterVision Solutions b.v.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>

"""
The role gate: one dependency per endpoint, built from what the manifest declared.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from fastapi import Request, Security
from fastapi.routing import APIRoute
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from trixd.common import logging as trixlog
from trixd.common.constants import ROLE_NONE, TOKEN_KIND_NODE, TOKEN_KIND_USER
from trixd.common.errors import Forbidden, Unauthenticated
from trixd.core.security import access, audit, principals, tokens
from trixd.db import engine

LOGGER = trixlog.get("gate")

NODE_PATH_PARAMS: tuple = ("node", "name", "hostname")
# The two headers a token may come in, declared as schemes so the document offers one
# Authorize for both. Neither refuses on its own: the gate decides.
TOKEN_HEADER = APIKeyHeader(
    name="x-access-tokens", scheme_name="token", auto_error=False,
    description="The token from POST /api/v1/auth/token, as Luna takes it.")
BEARER = HTTPBearer(scheme_name="bearer", auto_error=False,
                    description="The same token, as Authorization: Bearer <token>.")


class Caller:
    """
    Who is making this request, and what they may do.
    """

    def __init__(self, claims: tokens.Claims, who: principals.Resolved, role: str) -> None:
        """
        Who is asking, and where they sit on the ladder.
        """
        self.claims = claims
        self.who = who
        self.name = claims.subject
        self.kind = claims.kind
        self.role = role
        self.rootus = access.is_rootus(claims.subject)

    def __repr__(self) -> str:
        """
        Enough to identify the caller in a log.
        """
        return f"<Caller {self.name} kind={self.kind}>"


def _present(x_access_tokens: str, bearer: HTTPAuthorizationCredentials) -> str:
    """
    The token, from either header a client may use.
    """
    if x_access_tokens:
        return x_access_tokens
    return bearer.credentials.strip() if bearer is not None else ""


def _target(request: Request) -> str:
    """
    What the request names, for the audit row: the path is the object here.
    """
    return str(getattr(request, "url", "") and request.url.path or "")


def guard(minimum: str = ROLE_NONE, token_kind: str = TOKEN_KIND_USER) -> callable:
    """
    Build the dependency for one endpoint: the token it takes and the role it needs.
    """

    def dependency(request: Request,
                   x_access_tokens: str = Security(TOKEN_HEADER),
                   bearer: HTTPAuthorizationCredentials = Security(BEARER)) -> Caller:
        """
        The check FastAPI runs before the handler.
        """
        claims = tokens.verify(_present(x_access_tokens, bearer))

        if token_kind == TOKEN_KIND_NODE:
            if claims.kind != TOKEN_KIND_NODE:
                raise Forbidden("this endpoint accepts a node token only")
            target = ""
            for key in NODE_PATH_PARAMS:
                if key in request.path_params:
                    target = str(request.path_params[key])
                    break
            if target and claims.node.split(".")[0] != target.split(".")[0]:
                LOGGER.warning("node token for %s used on %s", claims.node, target)
                audit.refused(claims.subject, request.method, target,
                              f"node token for {claims.node}")
                raise Forbidden("token is not valid for this node")
            return Caller(claims, None, ROLE_NONE)

        if claims.kind == TOKEN_KIND_NODE:
            audit.refused(claims.subject, request.method, _target(request),
                          "a node token on a user endpoint")
            raise Forbidden("a node token is not permitted on this endpoint")

        handle = engine.session()
        try:
            who = principals.resolve(handle, claims.subject)
            if who is None:
                audit.refused(claims.subject, request.method, _target(request),
                              "the subject cannot be resolved here")
                raise Unauthenticated(f"{claims.subject} cannot be resolved here")
        finally:
            handle.close()

        allowed, held = access.permits(who, minimum)
        if not allowed:
            audit.refused(claims.subject, request.method, _target(request),
                          f"needs {minimum}, holds {held}")
            raise Forbidden(f"this needs {minimum}; you hold {held}")
        audit.granted(claims.subject, request.method, _target(request))
        return Caller(claims, who, held)

    # What the published document says about a route this guards.
    dependency.minimum = minimum
    dependency.token_kind = token_kind
    return dependency


def access_of(route) -> dict:
    """
    Who may call a route, as the document shows it: a role and its minimum, a node token,
    or public when no guard stands in front of it.
    """
    for item in getattr(getattr(route, "dependant", None), "dependencies", []):
        call = getattr(item, "call", None)
        if hasattr(call, "minimum"):
            if call.token_kind == TOKEN_KIND_NODE:
                return {"x-trixd-access": "node", "x-trixd-minimum": ""}
            return {"x-trixd-access": "role", "x-trixd-minimum": call.minimum}
    return {"x-trixd-access": "public", "x-trixd-minimum": ""}


def stamp(router) -> None:
    """
    Put x-trixd-access and x-trixd-minimum on every route of a router that has none yet.
    """
    for route in router.routes:
        if isinstance(route, APIRoute) and "x-trixd-access" not in (route.openapi_extra or {}):
            route.openapi_extra = {**(route.openapi_extra or {}), **access_of(route)}
