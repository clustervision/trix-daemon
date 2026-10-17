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
What this controller has already applied for a peer, so a repeat answers rather than runs.

Delivery is at-least-once: the sender gives a call three seconds, and a peer that applies
it more slowly than that keeps the row and offers it again at every pass. The call was
being replayed rather than the answer, which is how the same write lands repeatedly and
how a second application comes back saying it changed nothing - an answer the sender then
cannot tell from the peer declining to do the work.

Held in memory and not in a table on purpose: a repeat arrives within a sweep interval of
the first, which is minutes, and the cost of forgetting one across a restart is the
behaviour we have today. A table would be a migration and a second thing to replicate.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import threading
import time

from trixd.common import logging as trixlog

LOGGER = trixlog.get("applied")

# Long enough to cover several sweep intervals, which is where a repeat comes from.
KEEP: int = 3600
# A ceiling so a storm cannot grow this without bound; the oldest go first.
LIMIT: int = 4096

_answers: dict = {}
_lock = threading.Lock()


def declined(result) -> str:
    """
    What a handler said it did not do, or empty when it did the work.

    One definition, because two sides read it: the sender takes it out of the apply answer
    to decide whether the row is done, and the receiver out of the handler's own return to
    decide whether to remember the answer at all. The two disagreeing is how a decline gets
    remembered as a success and then handed to every retry for an hour.

    A non-empty string only. `skipped` is a reserved key in a replicated handler's answer,
    and a count - three of ten items skipped - is not a handler declining to act.
    """
    if isinstance(result, dict) and isinstance(result.get("skipped"), str):
        return result["skipped"][:200]
    return ""


def _forget(now: float) -> None:
    """
    Drop what is too old to be a repeat, and the oldest beyond the ceiling. Called with
    the lock held.
    """
    for key in [key for key, (when, _) in _answers.items() if now - when > KEEP]:
        del _answers[key]
    if len(_answers) > LIMIT:
        for key in sorted(_answers, key=lambda item: _answers[item][0])[:len(_answers) - LIMIT]:
            del _answers[key]


def key_of(origin: str, request_id: str) -> str:
    """
    What identifies one call. `origin` is the principal the call was made under on the other
    controller, not the controller's own name - the word means something else on a
    replicated row - so it narrows the key without being the thing that makes it unique.
    That is the request id, which carries a time, a pid and a uuid.
    """
    return f"{origin}:{request_id}"


def recorded(origin: str, request_id: str):
    """
    The answer already given for this call, or None when it has not been applied here.
    """
    if not request_id:
        return None
    with _lock:
        held = _answers.get(key_of(origin, request_id))
        return held[1] if held else None


def record(origin: str, request_id: str, answer: dict) -> None:
    """
    Remember what one call answered, so offering it again answers the same.
    """
    if not request_id:
        return
    now = time.time()
    with _lock:
        _answers[key_of(origin, request_id)] = (now, answer)
        _forget(now)


def forget_all() -> None:
    """
    Drop everything held. For the tests, and for a plugin reload that invalidates nothing
    else - an answer is about a call that happened, so nothing here expires on a reload.
    """
    with _lock:
        _answers.clear()
