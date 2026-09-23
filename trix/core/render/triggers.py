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

from trix.common import logging as trixlog
from trix.common.settings import current as settings
from trix.core.luna.client import current as luna
from trix.core.render.engine import current as renders

LOGGER = trixlog.get('triggers')


class LunaHashes:
    """One request and one comparison per pass.

    This is what makes the hook allowed to be lossy: a notification that never
    arrives is picked up here instead.
    """

    def __init__(self) -> None:
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
            LOGGER.warning('could not read Luna table hashes: %s', error)
            return []

        if not current:
            # Luna answers this route only on the HA master, so an empty answer is
            # normal on a single controller and must not be read as "nothing
            # changed". Said once, then left alone.
            if not self.quiet:
                LOGGER.warning('Luna returned no table hashes, so this fallback is inert '
                               'here; Luna answers that route on the HA master only')
                self.quiet = True
            return []
        self.quiet = False

        changed = [table for table, digest in current.items()
                   if self.seen.get(table) != digest]
        first = not self.seen
        self.seen = dict(current)
        if first:
            LOGGER.info('baseline taken for %d Luna tables', len(current))
            return []

        woken = []
        for table in changed:
            woken.extend(renders().notify(f'luna.{table}'))
        if changed:
            LOGGER.info('Luna tables changed: %s; woke %s',
                        ', '.join(changed), ', '.join(woken) or 'nothing')
        return woken

    def _loop(self) -> None:
        interval = settings().triggers.luna_hashes
        while not self.stop.wait(interval):
            self.once()

    def start(self) -> bool:
        interval = settings().triggers.luna_hashes
        if interval <= 0:
            LOGGER.info('the Luna hash fallback is switched off')
            return False
        self.stop.clear()
        self.thread = threading.Thread(target=self._loop, name='trix-hashes', daemon=True)
        self.thread.start()
        LOGGER.info('comparing Luna table hashes every %ss', interval)
        return True

    def shutdown(self) -> None:
        self.stop.set()

    def state(self) -> dict:
        return {'interval': settings().triggers.luna_hashes,
                'last_pass': self.last_pass,
                'tables_seen': len(self.seen),
                'answering': not self.quiet and bool(self.seen)}


_hashes: LunaHashes = LunaHashes()


def current() -> LunaHashes:
    """
    The Luna hash poller for this process.
    """
    return _hashes
