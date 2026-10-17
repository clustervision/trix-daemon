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
Background triggers: what wakes a render when the change did not come through this
daemon. The daemon holds them and runs them; a plugin says what one watches.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from trixd.common import logging as trixlog

LOGGER = trixlog.get("triggers")

# Name to trigger, in the order they registered. A trigger offers baseline, start,
# shutdown and state; what it watches, and whether it watches anything at all, is
# the plugin's business and never the daemon's. Its state() carries at least
# answering and last_pass, which are what /system/health publishes for it.
_triggers: dict = {}
# Which plugin registered each trigger, so unplugging that plugin can stop it.
_owners: dict = {}
# Whether the triggers have been started. One registered after that has to start itself:
# start_all runs once, at start-up, so a reload's trigger would never have been started.
_running: bool = False


def _stop(name: str, trigger) -> None:
    """
    Ask one trigger to stop, and say so rather than raise when it will not.
    """
    if trigger is None:
        return
    try:
        trigger.shutdown()
    except Exception as error:
        LOGGER.error("trigger %s could not be stopped: %s", name, error)


def _start(name: str, trigger) -> None:
    """
    Start one trigger, and say so rather than raise when it will not.
    """
    try:
        trigger.start()
    except Exception as error:
        LOGGER.error("trigger %s could not start: %s", name, error)


def register(name: str, trigger, owner: str = "") -> None:
    """
    Offer a background trigger under a name, which is the key it reports health by.

    Registering the same name twice replaces it and stops the one replaced, which it did
    not before: the old poller kept running with nothing referencing it, so nothing could
    stop it, and its baseline was the one still driving the renders while health reported
    the replacement that had never started.
    """
    held = _triggers.get(name)
    if held is not None and held is not trigger:
        LOGGER.info("trigger %s replaced", name)
        _stop(name, held)
    _triggers[name] = trigger
    _owners[name] = owner
    if _running:
        _start(name, trigger)


def forget(owner: str) -> list:
    """
    Stop and drop every trigger one plugin registered, for when it is unplugged.
    """
    dropped = []
    for name in [key for key, who in _owners.items() if who and who == owner]:
        _stop(name, _triggers.pop(name, None))
        _owners.pop(name, None)
        dropped.append(name)
    if dropped:
        LOGGER.info("triggers stopped with %s: %s", owner, ", ".join(dropped))
    return dropped


def baseline_all() -> None:
    """
    Take every trigger's baseline before the reconcile, so a change landing after it
    is seen as a change rather than absorbed into the first reading.
    """
    for name, trigger in _triggers.items():
        try:
            trigger.baseline()
        except Exception as error:
            LOGGER.error("trigger %s could not take its baseline: %s", name, error)


def start_all() -> None:
    """
    Start every trigger. One that refuses to start says so and the others still run.
    """
    global _running
    _running = True
    for name, trigger in list(_triggers.items()):
        _start(name, trigger)


def shutdown_all() -> None:
    """
    Ask every trigger to stop; each ends at its next wake-up.
    """
    global _running
    _running = False
    for name, trigger in list(_triggers.items()):
        _stop(name, trigger)


def states() -> dict:
    """
    What each trigger is doing, for the health endpoint.
    """
    found = {}
    for name, trigger in _triggers.items():
        try:
            found[name] = trigger.state()
        except Exception as error:
            LOGGER.error("trigger %s could not report its state: %s", name, error)
            found[name] = {"error": True}
    return found
