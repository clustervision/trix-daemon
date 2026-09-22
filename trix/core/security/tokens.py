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
Minting and verifying JSON web tokens. Same format as Luna, asymmetric algorithm.
"""

__author__      = 'Sumit Sharma'
__copyright__   = 'Copyright 2026, TrinityX Project'
__license__     = 'GPL'
__version__     = '1.0'
__maintainer__  = 'Sumit Sharma'
__email__       = 'sumit.sharma@clustervision.com'
__status__      = 'Development'

from datetime import datetime, timedelta, timezone

import jwt

from trix.common import logging as trixlog
from trix.common.constants import (TOKEN_ALGORITHM, TOKEN_KIND_NODE, TOKEN_KIND_SERVICE,
                                   TOKEN_KIND_USER, TOKEN_KINDS)
from trix.common.errors import Unauthenticated
from trix.core.security import keys

LOGGER = trixlog.get('tokens')


class Claims:
    """What a verified token said."""

    def __init__(self, payload: dict) -> None:
        self.raw = payload
        self.subject: str = payload.get('sub', '')
        self.kind: str = payload.get('kind', TOKEN_KIND_USER)
        self.source: str = payload.get('src', '')
        self.issuer: str = payload.get('iss', '')
        self.node: str = payload.get('node', '')

    def __repr__(self) -> str:
        return f'<Claims {self.subject} kind={self.kind} iss={self.issuer}>'


def mint(subject: str, kind: str = TOKEN_KIND_USER, expiry: int = 3600,
         source: str = 'local', node: str = '') -> str:
    """Sign a token with this daemon's private key."""
    if kind not in TOKEN_KINDS:
        raise Unauthenticated(f'unknown token kind {kind}')
    ring = keys.current()
    if ring.private is None:
        raise Unauthenticated('this daemon holds no signing key')
    issued = datetime.now(timezone.utc)
    payload = {
        'iss': ring.issuer,
        'sub': subject,
        'kind': kind,
        'src': source,
        'iat': issued,
        'exp': issued + timedelta(seconds=expiry),
    }
    if kind == TOKEN_KIND_NODE:
        payload['node'] = node or subject
    return jwt.encode(payload, ring.private, algorithm=TOKEN_ALGORITHM,
                      headers={'kid': ring.key_id})


def verify(token: str) -> Claims:
    """Check a token against the issuers this daemon is configured to trust."""
    if not token:
        raise Unauthenticated('a valid token is missing')
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise Unauthenticated('token is invalid') from None

    key_id = header.get('kid', '')
    public = keys.current().public_key(key_id)
    if public is None:
        LOGGER.warning('token presented with untrusted key id %r', key_id)
        raise Unauthenticated('token was signed by an issuer this daemon does not trust')

    try:
        payload = jwt.decode(token, public, algorithms=[TOKEN_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise Unauthenticated('token has expired') from None
    except jwt.PyJWTError:
        raise Unauthenticated('token is invalid') from None

    claims = Claims(payload)
    if claims.kind not in TOKEN_KINDS:
        raise Unauthenticated('token carries an unknown kind')
    return claims


def service_token(expiry: int = 300) -> str:
    """A short-lived token naming this daemon, for talking to the other one."""
    ring = keys.current()
    return mint(ring.issuer, kind=TOKEN_KIND_SERVICE, expiry=expiry, source='local')
