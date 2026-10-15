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
Minting and verifying JSON web tokens, in the shape and algorithm Luna uses.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from datetime import datetime, timedelta, timezone

import jwt

from trixd.common import logging as trixlog
from trixd.common.constants import (LUNA_CLAIM_ID, LUNA_CLAIM_NODE, LUNA_CLAIM_SCOPE,
                                    LUNA_NO_USER_ID, LUNA_SCOPE_PROVISION,
                                    LUNA_SUPERUSER_ID, TOKEN_KIND_NODE,
                                    TOKEN_KIND_SERVICE, TOKEN_KIND_USER, TOKEN_KINDS)
from trixd.common.errors import Unauthenticated
from trixd.common.settings import current as settings
from trixd.core.security import keys

LOGGER = trixlog.get("tokens")


class Claims:
    """
    What a verified token said, whichever of the two daemons minted it.
    """

    def __init__(self, payload: dict) -> None:
        """
        Read the claims, filling in what Luna leaves out.
        """
        self.raw = payload
        self.subject: str = payload.get("sub", "")
        self.kind: str = payload.get("kind", "")
        self.source: str = payload.get("src", "")
        self.issuer: str = payload.get("iss", "")
        self.node: str = payload.get(LUNA_CLAIM_NODE, "")
        self.scope: str = payload.get(LUNA_CLAIM_SCOPE, "")
        self.luna_id = payload.get(LUNA_CLAIM_ID)
        if not self.kind:
            # Luna names no kind. A provision scope is its node token; anything else is a
            # user token, and only id 0 below has a counterpart here.
            self.kind = (TOKEN_KIND_NODE if self.scope == LUNA_SCOPE_PROVISION
                         else TOKEN_KIND_USER)
            self.source = self.source or "luna"
        if not self.subject and self.kind == TOKEN_KIND_NODE:
            self.subject = self.node
        if not self.subject and self.luna_id == LUNA_SUPERUSER_ID:
            # Luna's id 0 is the administrator its ini names, the same account as ours.
            # Any other id has no counterpart here, so the token has no subject and fails.
            self.subject = settings().api.username

    def __repr__(self) -> str:
        """
        Enough to identify the token in a log.
        """
        return f"<Claims {self.subject} kind={self.kind} iss={self.issuer}>"


def mint(subject: str, kind: str = TOKEN_KIND_USER, expiry: int = 3600,
         source: str = "local", node: str = "") -> str:
    """
    Sign a token with this daemon's key: the shared secret, or its private key under EdDSA.
    """
    if kind not in TOKEN_KINDS:
        raise Unauthenticated(f"unknown token kind {kind}")
    ring = keys.current()
    signing = ring.signing_key()
    if signing is None:
        raise Unauthenticated("this daemon holds nothing to sign with")
    issued = datetime.now(timezone.utc)
    payload = {
        "iss": ring.issuer,
        "sub": subject,
        "kind": kind,
        "src": source,
        "iat": issued,
        "exp": issued + timedelta(seconds=expiry),
    }
    if kind == TOKEN_KIND_NODE:
        payload[LUNA_CLAIM_NODE] = node or subject
        # Luna confines a provision-scoped token to the node it names. Saying so
        # narrows the token: without it Luna would read this as an administrator.
        payload[LUNA_CLAIM_SCOPE] = LUNA_SCOPE_PROVISION
    else:
        # Luna knows a caller by id alone and reads a missing one as its administrator.
        # Only the ini account is that administrator; anyone else is nobody there.
        rootus = kind == TOKEN_KIND_USER and subject == settings().api.username
        payload[LUNA_CLAIM_ID] = LUNA_SUPERUSER_ID if rootus else LUNA_NO_USER_ID

    # One secret means no identifier to choose between keys, so no kid header goes
    # out. That is what Luna's own tokens carry, or rather what they do not.
    headers = None if ring.shared else {"kid": ring.key_id}
    return jwt.encode(payload, signing, algorithm=ring.algorithm, headers=headers)


def verify(token: str) -> Claims:
    """
    Check a token against whatever this daemon is configured to accept.
    """
    if not token:
        raise Unauthenticated("a valid token is missing")
    ring = keys.current()

    key_id = ""
    if not ring.shared:
        try:
            key_id = jwt.get_unverified_header(token).get("kid", "")
        except jwt.PyJWTError:
            raise Unauthenticated("token is invalid") from None

    verifying = ring.verifying_key(key_id)
    if verifying is None:
        LOGGER.warning("nothing here verifies a token with key id %r", key_id)
        raise Unauthenticated("token was signed by an issuer this daemon does not trust")

    try:
        payload = jwt.decode(token, verifying, algorithms=[ring.algorithm])
    except jwt.ExpiredSignatureError:
        raise Unauthenticated("token has expired") from None
    except jwt.PyJWTError:
        raise Unauthenticated("token is invalid") from None

    claims = Claims(payload)
    if claims.kind not in TOKEN_KINDS:
        raise Unauthenticated("token carries an unknown kind")
    return claims


def service_token(expiry: int = 300) -> str:
    """
    A short-lived token naming this daemon, for talking to the other one.
    """
    ring = keys.current()
    return mint(ring.issuer, kind=TOKEN_KIND_SERVICE, expiry=expiry, source="local")
