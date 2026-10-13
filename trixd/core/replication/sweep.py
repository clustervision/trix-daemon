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

import requests

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, TOKEN_HEADER
from trixd.common.settings import current as settings
from trixd.core.luna import config as luna_config
from trixd.core.replication import tables
from trixd.core.security import tokens
from trixd.db import engine as database

LOGGER = trixlog.get("sweep")

TIMEOUT: int = 30


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
        # The peer serves what this controller serves: the protocol luna.ini names.
        url = f"{luna_config.current().protocol}://{peer}{API_PREFIX}/system/replication/{path}"
        headers = {TOKEN_HEADER: tokens.service_token()}
        # The service token passes every check, so it goes only to a peer that proves
        # itself with a certificate the cluster's own CA signed.
        answer = requests.get(url, headers=headers, timeout=TIMEOUT, verify=self._trust())
        if answer.status_code >= 400:
            raise RuntimeError(f"{peer} answered {answer.status_code}")
        return answer.json()

    def _trust(self):
        """
        The cluster CA to check a peer against, or the system store when it is missing.
        """
        ca_file = settings().replication.ca_file
        return ca_file if ca_file and os.path.isfile(ca_file) else True

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
        One pass over every configured peer.
        """
        results = []
        for peer in settings().replication.peers:
            try:
                results.append(self.pass_over(peer))
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
