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
What serving the sockets costs where no request is left to report it: a connection the
server closed with nothing answered on it, counted so health can say it happened.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import ssl

from trixd.common import logging as trixlog

LOGGER = trixlog.get("serving")

# The one teardown race this daemon absorbs: a caller writes on a connection the server
# has already said close_notify on, and TLS raises as the socket closes. Matched by the
# reason and not by the type, because every other SSLError - a handshake that failed, a
# certificate neither side would accept - is a fault somebody has to see, and still goes
# to asyncio with its traceback.
CLOSE_NOTIFY_RACE: str = "APPLICATION_DATA_AFTER_CLOSE_NOTIFY"
# Process-local, as the outbox's breaker is: one worker, so this is what this process has
# seen of its own sockets rather than anything about the pair. No lock - the event loop is
# the only writer, and health only reads it.
_closed_unanswered: int = 0


def exception_handler(loop, context: dict) -> None:
    """
    Count the teardown race and say so in one line; hand everything else to asyncio.

    On the loop because the exception is raised while the socket is being closed, in a
    task nothing awaits: there is no request left to answer with it, and asyncio reports
    it whenever the task is collected, which reads as unrelated to the call that caused it.
    """
    global _closed_unanswered
    error = context.get("exception")
    if isinstance(error, ssl.SSLError) and getattr(error, "reason", "") == CLOSE_NOTIFY_RACE:
        _closed_unanswered += 1
        LOGGER.warning("a caller wrote on a connection already being closed, so that "
                       "connection was closed without an answer; %s since start-up",
                       _closed_unanswered)
        return
    loop.default_exception_handler(context)


def states() -> dict:
    """
    What the health endpoint publishes about serving the sockets.
    """
    return {"closed_unanswered": _closed_unanswered}
