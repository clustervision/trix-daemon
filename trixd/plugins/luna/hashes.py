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
The fallback trigger: compare Luna's table hashes on a slow timer.
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
from trixd.core.render.engine import current as renders
from trixd.plugins.luna.client import current as luna

LOGGER = trixlog.get("luna.hashes")

# How often the hashes are compared. This is the fallback for a change made at Luna
# directly; what trixd runs on is a push from Luna, so an hour is plenty and there is
# nothing here worth configuring. 0 switches it off, which is what a test wants.
INTERVAL: int = 3600


class LunaHashes:
    """
    One request and one comparison per pass. A change made at Luna directly, never
    passing through this daemon, is picked up here wherever Luna answers the route.
    """

    def __init__(self) -> None:
        """
        The poller, not yet running.
        """
        self.stop = threading.Event()
        self.thread = None
        self.seen: dict = {}
        self.last_pass: float = 0.0
        self.quiet: bool = False

    def once(self) -> list:
        """
        Fetch the hashes, wake whatever declared a changed table as an input.
        """
        self.last_pass = time.time()
        try:
            current = luna().table_hashes()
        except Exception as error:
            LOGGER.warning("could not read Luna table hashes: %s", error)
            return []

        if not current:
            # Luna answers this only on the HA master, so empty is normal here and
            # is not "nothing changed". Said once, then left alone.
            if not self.quiet:
                LOGGER.warning("Luna returned no table hashes, so this fallback is inert "
                               "here; Luna answers that route on the HA master only")
                self.quiet = True
            return []
        self.quiet = False

        changed = [table for table, digest in current.items()
                   if self.seen.get(table) != digest]
        first = not self.seen
        self.seen = dict(current)
        if first:
            LOGGER.info("baseline taken for %d Luna tables", len(current))
            return []

        woken = []
        for table in changed:
            woken.extend(renders().notify(f"luna.{table}"))
        if changed:
            LOGGER.info("Luna tables changed: %s; woke %s",
                        ", ".join(changed), ", ".join(woken) or "nothing")
        return woken

    def baseline(self) -> None:
        """
        Read the hashes once before the reconcile, so that a change landing after it is
        seen as a change rather than absorbed into the first reading.
        """
        if INTERVAL > 0:
            self.once()

    def _loop(self) -> None:
        """
        One pass per interval until the daemon stops.
        """
        while not self.stop.wait(INTERVAL):
            self.once()

    def start(self) -> bool:
        """
        Begin polling, unless the interval says not to.
        """
        if INTERVAL <= 0:
            LOGGER.info("the Luna hash fallback is switched off")
            return False
        self.stop.clear()
        self.thread = threading.Thread(target=self._loop, name="trixd-hashes", daemon=True)
        self.thread.start()
        LOGGER.info("comparing Luna table hashes every %ss", INTERVAL)
        return True

    def shutdown(self) -> None:
        """
        Ask the poller to stop; it ends at its next wake-up.
        """
        self.stop.set()

    def state(self) -> dict:
        """
        What the poller is doing, for the control endpoint.
        """
        return {"interval": INTERVAL,
                "last_pass": self.last_pass,
                "tables_seen": len(self.seen),
                "answering": not self.quiet and bool(self.seen)}


_hashes: LunaHashes = LunaHashes()


def current() -> LunaHashes:
    """
    The Luna hash poller for this process.
    """
    return _hashes
