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
What trixd shows Luna to be let in, and what a neighbouring plugin imports to do the
same. Everything here is read from Luna's own luna.ini, so there is one copy of it.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
import requests
import urllib3

from trixd.common import logging as trixlog
from trixd.common.constants import (DAEMON_NAME, LUNA_CLAIM_ID, LUNA_SUPERUSER_ID,
                                    TOKEN_ALGORITHM_SHARED, TOKEN_KIND_SERVICE)
from trixd.common.errors import ServiceUnavailable, TrixdError, Unauthenticated
from trixd.core.security import keys
from trixd.plugins.luna import config as luna_config
from trixd.utils import files

LOGGER = trixlog.get("luna.credentials")

urllib3.disable_warnings()

# Where the token is kept between restarts, as the design draws it. luna2-cli caches a
# person's own token in the same file; as root both read the same luna.ini, so it is the
# same token. What a non-root caller should see is not settled yet.
DEFAULT_TOKEN_FILE: str = "~/.luna/token"
TOKEN_FILE_ENV: str = "TRIXD_LUNA_TOKEN"
# How long a token trixd signs itself lives, and how early any token is replaced.
TOKEN_LIFETIME: int = 300
RENEW_MARGIN: int = 60

_lock = threading.Lock()
_held: str = ""
_held_for: tuple = ()
_warned_unwritable: bool = False


def _fingerprint() -> tuple:
    """
    What a held token was obtained under. luna.ini naming another Luna, or another
    account, makes what we hold the wrong token however long it has left to live.
    """
    config = luna_config.current()
    return (config.path, config.base, config.username, config.secret)


def token_path() -> str:
    """
    The file the token is cached in, with the home directory resolved.
    """
    return os.path.expanduser(os.environ.get(TOKEN_FILE_ENV) or DEFAULT_TOKEN_FILE)


def get_credentials() -> tuple:
    """
    The account Luna's own luna.ini names, as (username, password). Luna's file is the
    one place it is written; trixd never holds a second copy.
    """
    config = luna_config.current()
    username, password = config.username, config.password
    if not username or not password:
        raise Unauthenticated(f"{config.path} has no [API] USERNAME and PASSWORD")
    return username, password


def get_token(renew: bool = False) -> str:
    """
    A token Luna accepts. What is already held comes first, then the cached file, then
    the secret luna.ini shares, and only then a sign-in over the wire. renew skips
    everything held and goes for a new one.

    Raises ServiceUnavailable when Luna cannot be reached and nothing usable is held:
    a render answers 503 rather than pretending it rendered.
    """
    global _held, _held_for
    with _lock:
        now = _fingerprint()
        if _held and _held_for != now:
            LOGGER.info("luna.ini names a different Luna or account; the held token goes")
            _held = ""
        if not renew:
            if _usable(_held):
                return _held
            found = _read_file()
            if _usable(found):
                _held, _held_for = found, now
                return found
        _held, _held_for = _mint(), now
        return _held


def get_token_for(username: str, password: str) -> str:
    """
    A token for the account the caller names, signed in for on the spot. For a plugin
    acting as somebody other than the account luna.ini names.

    Deliberately cached nowhere: what is held and what is on disk belong to the daemon's
    own account, and a token for another one must never be handed to a caller that
    asked for that. Every call is a request to Luna, so ask once and keep what you got
    for as long as you need it.
    """
    if not username or not password:
        raise Unauthenticated("a username and a password are needed to sign in at Luna")
    return _sign_in(username, password)


def _mint() -> str:
    """
    A token from the cheapest source that can still produce one. Signing costs nothing
    and is tried first; signing in costs a request to Luna, so what it returns is kept.
    """
    config = luna_config.current()
    if config.secret:
        return _self_signed()
    username, password = get_credentials()
    token = _sign_in(username, password)
    _write_file(token)
    return token


def _self_signed() -> str:
    """
    What trixd shows Luna for its own calls: Luna's administrator, id 0, signed with the
    secret from luna.ini, so no Luna password goes over the wire. Always HS256, which is
    what Luna verifies, whatever trixd signs its own tokens with.
    """
    issued = datetime.now(timezone.utc)
    payload = {"iss": keys.current().issuer, "sub": DAEMON_NAME, "kind": TOKEN_KIND_SERVICE,
               "src": "local", "iat": issued,
               "exp": issued + timedelta(seconds=TOKEN_LIFETIME),
               LUNA_CLAIM_ID: LUNA_SUPERUSER_ID}
    return jwt.encode(payload, luna_config.current().secret, algorithm=TOKEN_ALGORITHM_SHARED)


def _sign_in(username: str, password: str) -> str:
    """
    Luna's own /token, with the account its ini names. Every failure here is the same to
    a caller: Luna cannot be used right now, and the log says why.
    """
    config = luna_config.current()
    try:
        answer = requests.post(f"{config.base}/token",
                               json={"username": username, "password": password},
                               timeout=luna_config.TIMEOUT, verify=config.verify)
    except requests.RequestException as error:
        raise ServiceUnavailable(f"Luna is not answering: {error}") from None
    if answer.status_code >= 400:
        raise ServiceUnavailable(
            f"Luna refused the account {config.path} names: {answer.status_code}")
    try:
        token = answer.json()["token"]
    except (ValueError, KeyError, TypeError):
        raise ServiceUnavailable("Luna answered the token request without a token") from None
    LOGGER.info("signed in at Luna as %s", username)
    return token


def _usable(token: str) -> bool:
    """
    Whether a token is worth presenting: it decodes, it carries an expiry, and that
    expiry is far enough away to survive the call it is about to be used for.
    """
    if not token:
        return False
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "require": ["exp"]})
    except jwt.PyJWTError:
        return False
    return float(claims["exp"]) - time.time() > RENEW_MARGIN


def _read_file() -> str:
    """
    The cached token, or empty when there is none to be had. A file that cannot be read
    is the same as no file: the token is minted again.
    """
    path = token_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return ""
    except OSError as error:
        LOGGER.warning("could not read the cached token at %s: %s", path, error)
        return ""


def _write_file(token: str) -> None:
    """
    Keep what cost a request to Luna, so a restart does not ask again. Failing to cache is
    not failing: the token is good and is held in memory.
    """
    global _warned_unwritable
    path = token_path()
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # makedirs applies its mode only when it creates the directory, and the Luna command
        # line creates this one first on most boxes - so on every box but a clean install the
        # 0600 file sat in whatever directory was already there. Set it every time.
        os.chmod(directory, 0o700)
        # Through the shared writer, which names its temporary file unpredictably. The old
        # name was the path plus this process's id, opened without O_EXCL: a symlink waiting
        # at that name would have been followed and written through, as root.
        files.write(path, token + "\n", mode=0o600)
        _warned_unwritable = False
    except (OSError, TrixdError) as error:
        if not _warned_unwritable:
            LOGGER.warning("could not cache the Luna token at %s: %s; it is held in "
                           "memory and fetched again after a restart", path, error)
            _warned_unwritable = True
