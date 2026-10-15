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
Anti-entropy: exchange per-table hashes with each peer and copy what differs.
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

import jwt
import requests

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, TOKEN_HEADER
from trixd.common.errors import Unauthenticated
from trixd.common.settings import current as settings
from trixd.core.replication import tables
from trixd.db import engine as database

LOGGER = trixlog.get("sweep")

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


def _sign_in(peer: str, verify) -> str:
    """
    The account this daemon's own ini names, offered to the peer, which answers with a
    token of its own. The peer's certificate is checked first: a password goes with it.
    """
    config = settings()
    url = f"{config.api.protocol}://{peer}{API_PREFIX}/auth/token"
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


def trust():
    """
    The cluster CA to check a peer against, or the system store when it is missing. Module
    level because the outbox sends to the same peers and must check them the same way.
    """
    ca_file = settings().replication.ca_file
    return ca_file if ca_file and os.path.isfile(ca_file) else True


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


def _token(peer: str, verify, renew: bool = False) -> str:
    """
    The token held for a peer, signing in when there is none worth using.
    """
    with _token_lock:
        if not renew and _usable(_tokens.get(peer, "")):
            return _tokens[peer]
        _tokens[peer] = _sign_in(peer, verify)
        return _tokens[peer]


class Sweep:
    """
    One thread, one pass per interval, one comparison per table. Documents keep no
    order, so there is no gap to stop on: a peer that is away misses a pass.
    """

    def __init__(self) -> None:
        """
        The sweep, not yet running.
        """
        self.stop = threading.Event()
        self.thread = None
        self.last: dict = {}

    def _get(self, peer: str, path: str):
        """
        One read from a peer, or None when it is away.
        """
        # The peer serves what this controller serves: the protocol trixd.ini names.
        url = f"{settings().api.protocol}://{peer}{API_PREFIX}/system/replication/{path}"
        verify = self._trust()
        for attempt in (1, 2):
            headers = {TOKEN_HEADER: _token(peer, verify, renew=attempt == 2)}
            answer = requests.get(url, headers=headers, timeout=TIMEOUT, verify=verify)
            if answer.status_code in (401, 403) and attempt == 1:
                # The token the peer gave us is no longer good to it: ask for another.
                continue
            if answer.status_code >= 400:
                raise RuntimeError(f"{peer} answered {answer.status_code}")
            return answer.json()
        return None

    def _trust(self):
        """
        The cluster CA to check a peer against; the sweep's own name for it.
        """
        return trust()

    def pass_over(self, peer: str) -> dict:
        """
        Compare hashes with one peer and take whatever differs.
        """
        handle = database.session()
        try:
            theirs = (self._get(peer, "hashes") or {}).get("hashes") or {}
            ours = tables.hashes(handle)
            differing = [table for table in tables.ORDER
                         if theirs.get(table) and theirs[table] != ours.get(table)]
            if not differing:
                return {"peer": peer, "tables": [], "changes": {}}

            changes = {}
            for table in differing:
                documents = (self._get(peer, f"table/{table}") or {}).get("documents") or []
                changes[table] = tables.merge_table(handle, table, documents)
            LOGGER.info("reconciled %s with %s: %s", ", ".join(differing), peer, changes)
            return {"peer": peer, "tables": differing, "changes": changes}
        finally:
            handle.close()

    def once(self) -> list:
        """
        One pass over every configured peer: what it owes the peer, then what differs.
        """
        # Imported here because the outbox needs this module's sign-in and certificate,
        # so importing it at the top would be a cycle.
        from trixd.core.replication import outbox

        results = []
        for peer in settings().replication.peers:
            try:
                owed = outbox.drain(peer)
                result = self.pass_over(peer)
                results.append({**result, "owed": owed})
            except Exception as error:
                # A peer being away is ordinary. The next pass picks it up.
                LOGGER.warning("peer %s not reconciled this pass: %s", peer, error)
                results.append({"peer": peer, "error": str(error)})
        self.last = {"at": time.time(), "results": results}
        return results

    def _loop(self) -> None:
        """
        One pass per interval until the daemon stops.
        """
        interval = settings().replication.interval
        while not self.stop.wait(interval):
            self.once()

    def start(self) -> bool:
        """
        Only when peers are configured: one controller has nothing to reconcile.
        """
        config = settings().replication
        if not config.enabled or not config.peers:
            LOGGER.info("no replication peers configured, so no sweep is running")
            return False
        if self._trust() is True:
            LOGGER.warning("CA_FILE %s is missing: peers are checked against the system store, "
                           "which does not hold the cluster CA, so every pass will fail",
                           config.ca_file)
        self.stop.clear()
        self.thread = threading.Thread(target=self._loop, name="trixd-sweep", daemon=True)
        self.thread.start()
        LOGGER.info("reconciling with %s every %ss", ", ".join(config.peers), config.interval)
        return True

    def shutdown(self) -> None:
        """
        Ask the sweep to stop; it ends at its next wake-up.
        """
        self.stop.set()


_sweep: Sweep = Sweep()


def current() -> Sweep:
    """
    The sweep for this process.
    """
    return _sweep
