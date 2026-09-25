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
from trix.core.security import principals, tokens
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
    What a node presents after boot.
    """

    tpm_sha256: str = ""


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
        raise Unauthenticated("username or password is incorrect")
    return Token(token=tokens.mint(who.name, expiry=settings().api.expiry, source=who.source))


@router.post("/node/{node}", response_model=Token,
             summary="Exchange a node measurement for a node-scoped token")
def issue_node(node: str, body: NodeProof) -> Token:
    """
    Verify the measurement against Luna's record of that node: Luna is asked what
    it holds and the answer is compared, so no signing secret is involved.
    """
    record = luna().node(node)
    if not record:
        raise Unauthenticated(f"{node} is not a node Luna knows")
    expected = record.get("tpm_sha256") or ""
    if not expected:
        # Luna is not enforcing cluster security for this node, so neither do we.
        LOGGER.info("node %s has no measurement recorded; issuing on Luna terms", node)
    elif body.tpm_sha256 != expected:
        LOGGER.warning("node %s presented a measurement that does not match", node)
        raise Unauthenticated("the measurement does not match")
    return Token(token=tokens.mint(node, kind=TOKEN_KIND_NODE,
                                   expiry=settings().api.expiry, node=node))


@router.get("/whoami", summary="What this token says about its bearer")
def whoami(caller: Caller = Depends(guard())) -> dict:
    """
    Useful when a client wants to know what it is holding.
    """
    return {
        "principal": caller.name,
        "kind": caller.kind,
        "usergroups": caller.usergroups,
        "capabilities": sorted(caller.capabilities),
        "issuer": caller.claims.issuer,
    }
