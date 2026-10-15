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


def register(name: str, trigger) -> None:
    """
    Offer a background trigger under a name, which is the key it reports health by.
    Registering the same name twice replaces it, so a re-plug does not stack pollers.
    """
    if name in _triggers and _triggers[name] is not trigger:
        LOGGER.info("trigger %s replaced", name)
    _triggers[name] = trigger


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
    for name, trigger in _triggers.items():
        try:
            trigger.start()
        except Exception as error:
            LOGGER.error("trigger %s could not start: %s", name, error)


def shutdown_all() -> None:
    """
    Ask every trigger to stop; each ends at its next wake-up.
    """
    for name, trigger in _triggers.items():
        try:
            trigger.shutdown()
        except Exception as error:
            LOGGER.error("trigger %s could not be stopped: %s", name, error)


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
