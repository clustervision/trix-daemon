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
The hardware rule files, compared with the other controller: what a peer holds and this
controller lacks is copied, and what differs is counted.
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

from trixd.common import logging as trixlog
from trixd.core.plugins.registry import current as registry
from trixd.core.replication import outbox

LOGGER = trixlog.get("prometheus.peer")

PLUGIN: str = "prometheus"

# How often a peer is asked what it holds, which is the sweep's own default. A pair that
# agrees pays for two lists of names, so there is nothing here worth configuring. 0
# switches it off, which is what a test wants.
INTERVAL: int = 300


class PeerRules:
    """
    One pass over every peer per interval, the first at once. A write the other
    controller was never told about leaves nothing owed and nothing to compare, so the
    files themselves are what is asked after.
    """

    def __init__(self) -> None:
        """
        The comparison, not yet running.
        """
        self.stop = threading.Event()
        self.thread = None
        self.last_pass: float = 0.0
        self.failing: bool = False
        self.counts: dict = {"lacking": 0, "differing": 0}
        # Peers already said to be away or not to serve the list: once each, not per pass.
        self.said: set = set()

    def once(self) -> dict:
        """
        Compare with every peer the outbox replicates to, which is none when replication
        is switched off. A peer that cannot be read is said once and the others still run.
        """
        self.last_pass = time.time()
        backend = getattr(registry().get(PLUGIN), "backend", None)
        counts, failing = {"lacking": 0, "differing": 0}, False
        for peer in outbox.peers() if backend is not None else []:
            try:
                found = backend.hw_rules_from(peer)
                if found is not None:
                    counts = {key: counts[key] + found[key] for key in counts}
            except Exception as error:
                failing = True
                if peer not in self.said:
                    LOGGER.warning("the hardware rule files could not be compared with %s, "
                                   "and are again at the next pass: %s", peer, error)
                self.said.add(peer)
                continue
            if found is None:
                if peer not in self.said:
                    LOGGER.info("%s does not serve its list of hardware rule files, so "
                                "nothing is compared with it: it runs an older trixd, or "
                                "without this plugin", peer)
                self.said.add(peer)
                continue
            self.said.discard(peer)
        self.failing, self.counts = failing, counts
        return counts

    def baseline(self) -> None:
        """
        Nothing to read before the reconcile: the first pass follows it.
        """

    def _loop(self) -> None:
        """
        One pass at once, then one per interval until the daemon stops.
        """
        wait = 0
        while not self.stop.wait(wait):
            self.once()
            wait = INTERVAL

    def start(self) -> bool:
        """
        Begin comparing, where there is a peer to compare with.
        """
        if INTERVAL <= 0 or not outbox.peers():
            return False
        if self.thread is not None and self.thread.is_alive():
            return False
        self.stop.clear()
        self.thread = threading.Thread(target=self._loop, name="trixd-hw-rules", daemon=True)
        self.thread.start()
        LOGGER.info("comparing the hardware rule files with %s every %ss",
                    ", ".join(outbox.peers()), INTERVAL)
        return True

    def shutdown(self) -> None:
        """
        Ask the comparison to stop; it ends at its next wake-up.
        """
        self.stop.set()

    def state(self) -> dict:
        """
        What the comparison is doing, for the health endpoint. Differing files are
        ordinary, so the counts say how the pair stands and decide nothing here.
        """
        return {"interval": INTERVAL, "last_pass": self.last_pass,
                "answering": not self.failing, "counts": dict(self.counts)}


_rules: PeerRules = PeerRules()


def current() -> PeerRules:
    """
    The comparison for this process.
    """
    return _rules
