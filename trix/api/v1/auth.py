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
Where a caller logs in, and where a node exchanges its measurement for a token.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from trix.common import logging as trixlog
from trix.common.constants import API_PREFIX, TOKEN_KIND_NODE
from trix.common.errors import Unauthenticated
from trix.common.settings import current as settings
from trix.core.luna.client import current as luna
from trix.core.security import access, audit, principals, tokens
from trix.core.security.gate import Caller, guard
from trix.db.engine import scoped

LOGGER = trixlog.get("auth")

router = APIRouter(prefix=f"{API_PREFIX}/auth", tags=["auth"])


class Credentials(BaseModel):
    """
    What a person or a service presents to log in.
    """

    username: str
    password: str


class NodeProof(BaseModel):
    """
    What a node presents after boot: a measurement, or a credential when there is none.
    """

    tpm_sha256: str = ""
    username: str = ""
    password: str = ""


class Token(BaseModel):
    """
    A minted token.
    """

    token: str


@router.post("/token", response_model=Token, status_code=201,
             summary="Exchange credentials for a token")
def issue(body: Credentials, handle: Session = Depends(scoped)) -> Token:
    """
    Resolve the caller through the chain, then sign a token naming them.
    """
    who = principals.authenticate(handle, body.username, body.password)
    if who is None:
        LOGGER.warning("failed login for %s", body.username)
        audit.refused(body.username, "auth.token", detail="username or password")
        raise Unauthenticated("username or password is incorrect")
    audit.granted(who.name, "auth.token", detail=f"through {who.source}",
                  handle=handle)
    return Token(token=tokens.mint(who.name, expiry=settings().api.expiry, source=who.source))


@router.post("/node/{node}", response_model=Token, status_code=201,
             summary="Exchange a node measurement for a node-scoped token")
def issue_node(node: str, body: NodeProof) -> Token:
    """
    A node token, on the same terms Luna issues one: the measurement when the cluster
    enforces security, a credential when it does not. Never on the node name alone.
    """
    record = luna().node(node)
    if not record:
        raise Unauthenticated(f"{node} is not a node Luna knows")

    if luna().enforcing_security():
        expected = record.get("tpm_sha256") or ""
        if not expected:
            LOGGER.warning("node %s has no measurement and the cluster enforces one", node)
            raise Unauthenticated(f"{node} has no measurement recorded")
        if body.tpm_sha256 != expected:
            LOGGER.warning("node %s presented a measurement that does not match", node)
            audit.refused(node, "auth.node", node, "the measurement does not match")
            raise Unauthenticated("the measurement does not match")
        audit.granted(node, "auth.node", node, "on a matching measurement")
        return _node_token(node)

    # Luna answers this on a username and password when security is off. The token it
    # mints is one Luna accepts too, so the node name alone can never be enough.
    handle = next(scoped())
    try:
        who = principals.authenticate(handle, body.username, body.password)
    finally:
        handle.close()
    if who is None:
        LOGGER.warning("node token for %s refused: no measurement and no credential", node)
        audit.refused(body.username, "auth.node", node, "no measurement, no credential")
        raise Unauthenticated("a measurement or a valid credential is required")
    audit.granted(who.name, "auth.node", node, "on a credential")
    return _node_token(node)


def _node_token(node: str) -> Token:
    """
    A token scoped to one node, which Luna confines to that node's own endpoints.
    """
    return Token(token=tokens.mint(node, kind=TOKEN_KIND_NODE,
                                   expiry=settings().api.expiry, node=node))


@router.get("/whoami", summary="What this token says about its bearer")
def whoami(handle: Session = Depends(scoped),
           caller: Caller = Depends(guard())) -> dict:
    """
    Who the token names, and the role held in each usergroup rather than a bit list.
    """
    return {
        "principal": caller.name,
        "kind": caller.kind,
        "source": caller.claims.source,
        "admin": bool(getattr(caller.who, "admin", False)) or access.is_rootus(caller.name),
        "usergroups": access.roles_of(handle, caller.name),
        "issuer": caller.claims.issuer,
    }
