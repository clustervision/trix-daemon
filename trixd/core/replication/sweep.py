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

import threading
import time

import requests

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, TOKEN_HEADER
from trixd.common.settings import current as settings
from trixd.core.replication import outbox, tables
from trixd.core.replication.peering import TIMEOUT, peer_token, trust
from trixd.db import engine as database
from trixd.utils import threads

LOGGER = trixlog.get("sweep")

# How many passes a table may still differ after being merged before it is said loudly.
# One is ordinary: our row was the newer one, and the peer takes it on its own next pass.
# Several in a row is not, and means neither side will take the other's.
STUBBORN: int = 3
# A pass counts towards that only this share of an interval after the last one that did: the
# peer needs a pass of its own to take our row, and a timed pass may come a little early.
EARLY: float = 0.9


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
        # Consecutive passes one table has still differed from one peer after merging.
        self.stubborn: dict = {}
        # What the timer waits between passes; 0 until it runs, and every pass then counts.
        self.interval: int = 0

    def _get(self, peer: str, path: str):
        """
        One read from a peer, or None when it is away.
        """
        # The peer serves what this controller serves: the protocol trixd.ini names.
        url = f"{settings().api.protocol}://{peer}{API_PREFIX}/system/replication/{path}"
        verify = self._trust()
        for attempt in (1, 2):
            headers = {TOKEN_HEADER: peer_token(peer, renew=attempt == 2)}
            answer = requests.get(url, headers=headers, timeout=TIMEOUT, verify=verify)
            if answer.status_code in (401, 403) and attempt == 1:
                # The token the peer gave us is no longer good to it: ask for another.
                continue
            if answer.status_code >= 400:
                # A refusal that survives the renewal included, which is why there is no
                # path out of here that answers nothing: it raises, and the caller is left
                # before it can read that as the peer being up.
                raise RuntimeError(f"{peer} answered {answer.status_code}")
            return answer.json()

    def _trust(self):
        """
        The cluster CA to check a peer against; the sweep's own name for it.
        """
        return trust()

    def _converged(self, peer: str, comparable: list, still: list,
                   theirs: dict, ours: dict) -> None:
        """
        Keep count of a difference that is standing still, and say so once it has stood
        long enough. A pass that cannot converge said nothing and reported success, so the
        only sign was the full table being fetched every interval for ever.

        Two things this is careful about. Only tables that could actually be compared are
        touched: a peer that refuses both attempts answers no hashes at all, and treating
        that as "nothing differs" cleared the count for exactly the table nobody could
        look at. And the count only grows while *neither* side has moved - a table differing
        at the end of a pass is ordinary, because our row may simply be the newer one and
        the peer takes it on its own next pass, so counting passes alone called a healthy
        pair with a trickle of writes unconverged after three of them. For the same reason
        a pass asked for between two timed ones is not counted: the peer has had no pass.
        """
        for table in comparable:
            key = f"{peer}/{table}"
            if table not in still:
                self.stubborn.pop(key, None)
                continue
            seen = (theirs.get(table), ours.get(table))
            count, before, at = self.stubborn.get(key, (0, None, 0))
            now = time.monotonic()
            if before == seen and now - at < self.interval * EARLY:
                continue
            count = count + 1 if before == seen else 1
            self.stubborn[key] = (count, seen, now)
            if count >= STUBBORN:
                LOGGER.error("%s has differed from %s for %d passes with neither side "
                             "moving: neither controller is taking the other's row, so "
                             "this will not resolve on its own", table, peer, count)

    def forget(self, peers: list) -> None:
        """
        Drop what is counted against a peer that is no longer configured, which nothing
        else clears: only a pass over that same peer does, and there are no more of those.
        """
        for key in [key for key in self.stubborn if key.split("/")[0] not in peers]:
            self.stubborn.pop(key, None)

    def stalled(self) -> int:
        """
        How many peer and table pairs have not converged for long enough to be said. For
        health, which carries counts only.
        """
        return len([1 for count, *_ in self.stubborn.values() if count >= STUBBORN])

    def unreconciled(self) -> int:
        """
        How many peers the last pass did not get through, whatever stopped it: a peer that
        is away, or one that answered and whose pass failed all the same. For health.
        """
        return len([1 for result in (self.last or {}).get("results") or []
                    if "error" in result])

    def pass_over(self, peer: str) -> dict:
        """
        Compare hashes with one peer and take whatever differs.
        """
        handle = database.session()
        try:
            # It answered, so it is up: that closes its breaker whether or not anything was
            # owed to it. Waiting for a replicated row to drain would leave health
            # reporting a healthy peer as not answering until one happened along.
            #
            # Reaching this line is the whole of the test. A refusal that survives the
            # renewal raises out of _get, so a credential mismatch never gets here - which
            # it must not, or the breaker is closed by every pass and reopened by the next
            # three calls.
            theirs = (self._get(peer, "hashes") or {}).get("hashes") or {}
            outbox.record_success(peer)
            ours = tables.hashes(handle)
            comparable = [table for table in tables.ORDER if theirs.get(table)]
            absent = [table for table in tables.ORDER if table not in comparable]
            if absent:
                LOGGER.warning("%s sent no hash for %s, so %s not compared this pass",
                               peer, ", ".join(absent),
                               "they were" if len(absent) > 1 else "it was")
            differing = [table for table in comparable if theirs[table] != ours.get(table)]
            if not differing:
                self._converged(peer, comparable, [], theirs, ours)
                return {"peer": peer, "tables": [], "changes": {}, "unconverged": []}

            changes = {}
            for table in differing:
                documents = (self._get(peer, f"table/{table}") or {}).get("documents") or []
                changes[table] = tables.merge_table(handle, table, documents)
            # Compared again against what the peer answered, rather than assumed: taking
            # everything of theirs that was newer is not the same as agreeing with them.
            after = tables.hashes(handle)
            still = [table for table in differing if theirs[table] != after.get(table)]
            self._converged(peer, comparable, still, theirs, after)
            LOGGER.info("reconciled %s with %s: %s", ", ".join(differing), peer, changes)
            return {"peer": peer, "tables": differing, "changes": changes,
                    "unconverged": still}
        finally:
            handle.close()

    def once(self) -> list:
        """
        One pass over every configured peer: what it owes the peer, then what differs.
        """
        results = []
        # The outbox's list, which is empty when replication is switched off: reading the
        # peers straight out of the settings meant `ENABLED = no` stopped the timer and left
        # health saying peers: 0, while POST /system/replication/sweep still reconciled and
        # still drained the outbox across.
        configured = outbox.peers()
        self.forget(configured)
        for peer in configured:
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
        self.interval = settings().replication.interval
        while not self.stop.wait(self.interval):
            self.once()

    def start(self) -> bool:
        """
        Only when peers are configured: one controller has nothing to reconcile.
        """
        config = settings().replication
        # Two different states, and reporting the switch as an empty peer list is the
        # opposite of true on a pair: peers first, so the switch names what it holds up.
        if not config.peers:
            LOGGER.info("no replication peers configured, so no sweep is running")
            return False
        if not config.enabled:
            LOGGER.info("[REPLICATION] ENABLED is no, so no sweep is running: %s is "
                        "configured and is not reconciled", ", ".join(config.peers))
            return False
        try:
            if self._trust() != config.ca_file:
                LOGGER.warning("CA_FILE %s is missing: peers are checked against the system "
                               "trust store instead, which carries the cluster CA only "
                               "where this controller installed it there", config.ca_file)
        except requests.exceptions.SSLError as error:
            # Said, not raised: the sweep still runs, and each pass reports the peer it
            # could not check, exactly as it reports one that is away.
            LOGGER.error("CA_FILE %s is missing and %s, so no peer can be verified and "
                         "every pass will fail", config.ca_file, error)
        self.stop.clear()
        self.thread = threads.Thread(target=self._loop, name="trixd-sweep", daemon=True)
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
