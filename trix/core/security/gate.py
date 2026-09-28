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
The capability gate: one dependency per endpoint, built from what the manifest declared.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from fastapi import Header, Request

from trix.common import logging as trixlog
from trix.common.constants import TOKEN_KIND_NODE, TOKEN_KIND_SERVICE, TOKEN_KIND_USER
from trix.common.errors import Forbidden, Unauthenticated
from trix.core.security import access, audit, principals, tokens
from trix.db import engine

LOGGER = trixlog.get("gate")

NODE_PATH_PARAMS: tuple = ("node", "name", "hostname")

# What the daemon's own endpoints are governed as, when no plugin owns them.
CORE: str = "trix"


class Caller:
    """
    Who is making this request, and what they may do.
    """

    def __init__(self, claims: tokens.Claims, who: principals.Resolved, held: set) -> None:
        self.claims = claims
        self.who = who
        self.name = claims.subject
        self.kind = claims.kind
        self.usergroups = who.usergroups if who else []
        # The three characters held on the object this request named.
        self.bits = "".join(sorted(held)) if held else ""

    def __repr__(self) -> str:
        return f"<Caller {self.name} kind={self.kind}>"


def _present(x_access_tokens: str, authorization: str) -> str:
    if x_access_tokens:
        return x_access_tokens
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _target(request: Request) -> str:
    """
    What the request names, for the audit row: the path is the object here.
    """
    return str(getattr(request, "url", "") and request.url.path or "")


def guard(capability: str = "", token_kind: str = TOKEN_KIND_USER,
          plugin: str = "", bit: str = "") -> callable:
    """
    Build the dependency for one endpoint.
    """

    def dependency(request: Request,
                   x_access_tokens: str = Header(default=None),
                   authorization: str = Header(default=None)) -> Caller:
        claims = tokens.verify(_present(x_access_tokens, authorization))

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
                audit.refused(claims.subject, capability, target,
                              f"node token for {claims.node}")
                raise Forbidden("token is not valid for this node")
            return Caller(claims, None, {capability})

        if claims.kind == TOKEN_KIND_NODE:
            audit.refused(claims.subject, capability, _target(request),
                          "a node token on a user endpoint")
            raise Forbidden("a node token is not permitted on this endpoint")

        handle = engine.session()
        try:
            who = principals.resolve(handle, claims.subject)
            if who is None and claims.kind != TOKEN_KIND_SERVICE:
                audit.refused(claims.subject, capability, _target(request),
                              "the subject cannot be resolved here")
                raise Unauthenticated(f"{claims.subject} cannot be resolved here")
            wanted = access.bit_for(request.method, _target(request), bit)
            allowed, bits = access.permits(handle, who, plugin or CORE, wanted)
            handle.commit()
        finally:
            handle.close()

        if capability and not allowed:
            audit.refused(claims.subject, capability, _target(request),
                          f"needs {wanted}, holds {bits}")
            raise Forbidden(f"{plugin or CORE} requires {wanted}; you hold {bits}")
        if capability:
            audit.granted(claims.subject, capability, _target(request))
        return Caller(claims, who, set(bits))

    return dependency
