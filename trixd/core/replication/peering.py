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
How this controller reaches a peer: what its certificate is checked against, the account
it is offered, and the token it issued us.

One module because the sweep and the outbox talk to the same peers and have to do it the
same way: a second way to authenticate to the same controller is a second thing to get
wrong, and a token held by one of them is good for the other.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import os
import threading
import time

import jwt
import requests

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, TOKEN_HEADER
from trixd.common.errors import Unauthenticated
from trixd.common.settings import current as settings
from trixd.utils import tls

LOGGER = trixlog.get("peering")

TIMEOUT: int = 30
# A token is replaced this long before it expires, so one never dies mid-pass.
RENEW_MARGIN: int = 60

# What each peer issued us, by peer. A peer signs us in and decides what its token may
# do, which is why nothing here mints one: the two controllers of a pair are configured
# with the same account but generate their own signing secret, so a token minted here
# is not one the peer can verify.
_tokens: dict = {}
_token_lock = threading.Lock()


def _usable(token: str) -> bool:
    """
    Whether a held token is worth presenting: it decodes, it says when it expires, and
    that is far enough away to survive the pass it is about to be used for.
    """
    if not token:
        return False
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "require": ["exp"]})
    except jwt.PyJWTError:
        return False
    return float(claims["exp"]) - time.time() > RENEW_MARGIN


def trust():
    """
    The cluster CA to check a peer against, or the store the operating system keeps when
    CA_FILE names nothing, which is where a controller installs that same CA. Never what
    `requests` reaches for on its own: a peer is a remote address carrying this daemon's
    password, and inside the virtual environment that bundle is certifi's.
    """
    ca_file = settings().replication.ca_file
    return ca_file if ca_file and os.path.isfile(ca_file) else tls.system_trust()


def _sign_in(peer: str) -> str:
    """
    The account this daemon's own ini names, offered to the peer, which answers with a
    token of its own. The peer's certificate is checked first: a password goes with it.
    """
    config = settings()
    url = f"{config.api.protocol}://{peer}{API_PREFIX}/auth/token"
    verify = trust()
    answer = requests.post(url, json={"username": config.api.username,
                                      "password": config.api.password},
                           timeout=TIMEOUT, verify=verify)
    if answer.status_code >= 400:
        raise Unauthenticated(f"{peer} refused the account trixd.ini names: "
                              f"{answer.status_code}")
    token = (answer.json() or {}).get("token", "")
    if not token:
        raise Unauthenticated(f"{peer} answered the token request without a token")
    LOGGER.info("signed in at %s", peer)
    return token


def held_token(peer: str) -> str:
    """
    The token already held for a peer, or empty when there is none worth using.

    Signing in is what costs time when a peer is dark: it is a request with this module's
    own thirty-second timeout. A caller on somebody's request path must not pay that, so
    it asks for what is held and gives up cheaply when the answer is nothing.
    """
    with _token_lock:
        token = _tokens.get(peer, "")
    return token if _usable(token) else ""


def peer_token(peer: str, renew: bool = False) -> str:
    """
    The token held for a peer, signing in when there is none worth using.
    """
    with _token_lock:
        if not renew and _usable(_tokens.get(peer, "")):
            return _tokens[peer]
        _tokens[peer] = _sign_in(peer)
        return _tokens[peer]


def read(peer: str, path: str, limit: int, params: dict = None):
    """
    One document read from a peer's API, or None where the peer serves no such route,
    which is what a controller on an older build answers.

    Bounded, because what comes back is held in memory: an answer longer than `limit`
    bytes is refused part way rather than read to its end.
    """
    url = f"{settings().api.protocol}://{peer}{API_PREFIX}/{path}"
    for attempt in (1, 2):
        headers = {TOKEN_HEADER: peer_token(peer, renew=attempt == 2)}
        with requests.get(url, params=params, headers=headers, timeout=TIMEOUT,
                          verify=trust(), stream=True) as answer:
            if answer.status_code in (401, 403) and attempt == 1:
                continue
            if answer.status_code == 404:
                return None
            if answer.status_code >= 400:
                raise RuntimeError(f"{peer} answered {answer.status_code} for {path}")
            body = b""
            for chunk in answer.iter_content(65536):
                body += chunk
                if len(body) > limit:
                    raise ValueError(f"{peer} answered more than {limit} bytes for {path}")
            return json.loads(body)
