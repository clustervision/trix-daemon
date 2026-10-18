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

import hmac

from fastapi import APIRouter, Body, Depends, Path
from pydantic import BaseModel
from sqlalchemy.orm import Session

from trixd.api.v1 import docs
from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, ROLE_OPERATOR, TOKEN_KIND_NODE
from trixd.common.errors import Unauthenticated
from trixd.common.settings import current as settings
from trixd.core.security import access, audit, nodes, principals, tokens
from trixd.core.security.gate import Caller, guard
from trixd.db import engine as database
from trixd.db.engine import scoped

LOGGER = trixlog.get("auth")

router = APIRouter(prefix=f"{API_PREFIX}/auth", tags=["auth"])
# What the document shows: a token's shape, and a measurement as Luna stores one.
TOKEN_EXAMPLE: dict = {"token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                                "eyJzdWIiOiJhbGljZSIsImtpbmQiOiJ1c2VyIn0.c2lnbmF0dXJl"}
MEASUREMENT: str = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


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
             summary="Sign in: exchange a username and password for a token",
             description=(
                 "The ini account is checked against `trixd.ini`; anyone else against the "
                 "sources in `[AUTH] CHAIN` in order, local, pam and ldap, and the first that "
                 "knows the name decides.\n\n"
                 "The token is valid for `[API] EXPIRY`, an hour by default. Send it on every "
                 "call as `x-access-tokens: <token>` or `Authorization: Bearer <token>`, or "
                 "paste it into **Authorize** on this page. Luna accepts it too, as its "
                 "administrator for the ini account only."),
             responses=docs.answers(TOKEN_EXAMPLE, "The token", status=201,
                                    errors={401: "The username or password is incorrect"}))
def issue(body: Credentials = Body(openapi_examples=docs.example(
              {"username": "alice", "password": "a long passphrase"})),
          handle: Session = Depends(scoped)) -> Token:
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
             summary="A node gets a token for itself",
             description=(
                 "How a node signs in after boot, on the terms Luna uses. When the cluster "
                 "enforces security the node presents its TPM measurement, `tpm_sha256`, "
                 "which must match what Luna holds for it; otherwise the daemon's own "
                 "account, or one holding a role that may act for a node. Never on the node "
                 "name alone, and never on being merely a known user: this token is the "
                 "node's identity, and Luna honours it as such.\n\n"
                 "The token is confined to that node: it is accepted only on node routes for "
                 "the node it names, such as `PATCH /api/v1/prometheus/hw-rules/{node}`."),
             responses=docs.answers(TOKEN_EXAMPLE, "A token for this node alone",
                                    status=201, errors={401: (
                                        "Luna does not know the node, the measurement does "
                                        "not match, or no valid credential was given")}))
def issue_node(node: str = Path(description="The node, as Luna names it",
                                openapi_examples=docs.example("node001")),
               body: NodeProof = Body(openapi_examples=docs.example({"tpm_sha256": MEASUREMENT}))
               ) -> Token:
    """
    A node token, on the same terms Luna issues one: the measurement when the cluster
    enforces security, a credential when it does not. Never on the node name alone.
    """
    record = nodes.current().node(node)
    if not record:
        raise Unauthenticated(f"{node} is not a node Luna knows")

    if nodes.current().enforcing_security():
        expected = record.get("tpm_sha256") or ""
        if not expected:
            LOGGER.warning("node %s has no measurement and the cluster enforces one", node)
            raise Unauthenticated(f"{node} has no measurement recorded")
        # Compared the way the ini password beside it is: this value is the node's only
        # authenticator on this path. Bytes, not str - compare_digest refuses a non-ASCII
        # str, and the measurement comes off the wire.
        if not hmac.compare_digest((body.tpm_sha256 or "").encode(), expected.encode()):
            LOGGER.warning("node %s presented a measurement that does not match", node)
            audit.refused(node, "auth.node", node, "the measurement does not match")
            raise Unauthenticated("the measurement does not match")
        audit.granted(node, "auth.node", node, "on a matching measurement")
        return _node_token(node)

    # Luna answers this on a username and password when security is off. The token it
    # mints is one Luna accepts too, so the node name alone can never be enough.
    handle = database.session()
    try:
        who = principals.authenticate(handle, body.username, body.password)
    finally:
        handle.close()
    if who is None:
        LOGGER.warning("node token for %s refused: no measurement and no credential", node)
        audit.refused(body.username, "auth.node", node, "no measurement, no credential")
        raise Unauthenticated("a measurement or a valid credential is required")
    # Being known is not enough. This mints a node's identity, and Luna honours it as that
    # node's own, so any account the chain resolves - a person with a shell on the
    # controller - could become any node on both daemons. The daemon's own account passes
    # as rootus; anyone else needs a role that says they may act for a node.
    allowed, held = access.permits(who, ROLE_OPERATOR)
    if not allowed:
        LOGGER.warning("node token for %s refused: %s holds %s", node, who.name, held)
        audit.refused(who.name, "auth.node", node, f"holds {held}, needs {ROLE_OPERATOR}")
        # The same answer as no credential at all: which of the two it was belongs in the
        # trail, not in a reply that would say whether the password was right.
        raise Unauthenticated("a measurement or a valid credential is required")
    audit.granted(who.name, "auth.node", node, "on a credential")
    return _node_token(node)


def _node_token(node: str) -> Token:
    """
    A token scoped to one node, which Luna confines to that node's own endpoints.
    """
    return Token(token=tokens.mint(node, kind=TOKEN_KIND_NODE,
                                   expiry=settings().api.expiry, node=node))


@router.get("/whoami", summary="Who this token names, and what they hold here",
            description=(
                "Who the token names and how trixd sees them: `kind` is `user`, `node` or "
                "`service`; `source` is where the name was checked, `ini`, `local`, `pam`, "
                "`ldap`, or `luna` for a token Luna issued; `role` is the role held here; "
                "`rootus` is true for the ini account, which passes every check whatever "
                "its role. Any signed-in user may ask, role `none` included."),
            responses=docs.answers({"principal": "alice", "kind": "user", "source": "local",
                                    "role": "reader", "rootus": False, "issuer": "trixd"},
                                   "Who the token names"))
def whoami(caller: Caller = Depends(guard())) -> dict:
    """
    Who the token names and where they sit on the ladder.
    """
    return {
        "principal": caller.name,
        "kind": caller.kind,
        "source": caller.claims.source,
        "role": caller.role,
        "rootus": caller.rootus,
        "issuer": caller.claims.issuer,
    }
