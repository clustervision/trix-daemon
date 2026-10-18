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
Hook dispatch: one queued change, acted on by every plugin that declared it.

This is the core's, not any plugin's. A change is named `<source>.<object>.<event>` and the
queue it runs on is its source, so `luna.node.update` runs on `luna`. The plugin that
*produces* those changes - the Luna pass-through - is not the plugin that acts on them: the
Prometheus plugin declares a hook on `luna.node.update` and wants it whether or not trixd is
proxying Luna at that moment, and a plugin added later will want the same.

Registered here, for every source a loaded plugin declares a hook on, rather than by the
plugin that happens to publish them. Registered by the publisher, unplugging that plugin took
the dispatcher with it and silently stopped the hooks of every other plugin - Prometheus's
hardware rules then drift from Luna with nothing to say so.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import json

from trixd.common import logging as trixlog
from trixd.common.constants import PLUGIN_STATE_LOADED
from trixd.core import tasks
from trixd.core.plugins.registry import current as registry
from trixd.core.render.engine import current as renders

LOGGER = trixlog.get("hooks")

# The sources this module currently answers for. Held here rather than read back out of the
# task registry, so nothing else's handler is ever mistaken for this one.
_dispatching: set = set()


def run(task: str, param: str = "") -> str:
    """
    The task behind a queued change: every plugin that declared it acts, then the renders wake.
    """
    payload = json.loads(param or "{}")
    outcomes = []
    # Under the engine's lock, so no render, the hash poller's included, runs mid-hook and
    # retires a file the hook is moving.
    with renders().lock:
        for plugin, handler in registry().hooks_for(task):
            try:
                outcomes.append(f"{plugin}: {handler(**payload)}")
            except Exception as error:
                LOGGER.error("%s could not act on %s: %s", plugin, task, error)
                outcomes.append(f"{plugin}: failed: {error}")
    woken = renders().notify(".".join(task.split(".")[:2]))
    if woken:
        outcomes.append(f"woke {', '.join(woken)}")
    return "; ".join(outcomes) or f"nothing declared {task}"


def sources() -> set:
    """
    The event sources a loaded plugin declares a hook on, which is the first segment of each
    declared event name. Derived from the manifests, so there is no list to keep anywhere.
    """
    found = set()
    for entry in registry().plugins.values():
        if entry.state != PLUGIN_STATE_LOADED:
            continue
        for event in (getattr(entry.manifest, "hooks", None) or {}):
            source = str(event).split(".")[0]
            if source:
                found.add(source)
    return found


def reconcile() -> list:
    """
    Answer for every source a loaded plugin wants, and stop answering for the rest.

    Called after any load or unload, so a source stays answered for while *any* loaded
    plugin still declares a hook on it - which is what makes unplugging the publisher
    harmless, and what lets a plugin added later declare a hook on Luna's events without
    anything being added to a list here or there. `POST /system/bulk` refuses an event whose
    source has no handler, and that refusal is now true by derivation: nothing would act on it.

    Written to be idempotent and to read the task registry rather than trust what it last
    did: `unload` takes back the handler under the plugin's own name, which for the publisher
    is also a hook source, so what this module last registered can be gone without it knowing.
    """
    wanted = sources()
    for source in sorted(wanted):
        standing = tasks.handler(source)
        if standing is run:
            _dispatching.add(source)
            continue
        if standing is not None:
            # A plugin of that name already answers for its own queue. Taking it over would
            # unplug that plugin's work, so the hooks are left undispatched and said.
            LOGGER.error("%s already answers for its own queue, so hooks on %s events are "
                         "not dispatched", source, source)
            _dispatching.discard(source)
            continue
        tasks.register(source, run)
        _dispatching.add(source)
    for source in sorted(_dispatching - wanted):
        if tasks.handler(source) is run:
            tasks.unregister(source)
        _dispatching.discard(source)
    LOGGER.info("hooks dispatched for %s", ", ".join(sorted(_dispatching)) or "nothing")
    return sorted(_dispatching)
