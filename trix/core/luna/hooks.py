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
What the daemon does itself once a change has gone through the pass-through to Luna.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import re

from trix.common import logging as trixlog
from trix.common.constants import LUNA_HOOK_SUBSYSTEM
from trix.core import tasks
from trix.core.plugins.registry import current as registry
from trix.core.render.engine import current as renders

LOGGER = trixlog.get("luna.hooks")

# The calls the pass-through recognises as a change to a node, by method and path.
NODE_UPDATE = re.compile(r"^config/node/(?P<name>[^/]+)$")
NODE_DELETE = re.compile(r"^config/node/(?P<name>[^/]+)/_delete$")
# The fields of a node update a hook acts on; anything else is Luna's business alone.
WATCHED: tuple = ("newnodename", "group")


def notice(method: str, path: str, body: bytes, status: int) -> dict:
    """
    The change this call made at Luna, as event and payload, or None when it made none.
    """
    if not 200 <= int(status) < 300:
        return None
    path = str(path or "").strip("/")
    method = str(method or "").upper()
    deleted = NODE_DELETE.match(path)
    if method == "GET" and deleted:
        return {"event": "node.delete", "payload": {"name": deleted.group("name")}}
    updated = NODE_UPDATE.match(path)
    if method != "POST" or not updated:
        return None
    name = updated.group("name")
    try:
        data = json.loads(body or b"{}")["config"]["node"][name]
    except (ValueError, KeyError, TypeError):
        data = {}
    payload = {"name": name}
    for field in WATCHED:
        if isinstance(data, dict) and data.get(field):
            payload[field] = str(data[field])
    if len(payload) == 1:
        return None
    return {"event": "node.update", "payload": payload}


def changed_object(method: str, path: str, status: int) -> str:
    """
    The Luna object a successful write touched, for the renders that read it, else empty.
    """
    if not 200 <= int(status) < 300:
        return ""
    parts = str(path or "").strip("/").split("/")
    if len(parts) < 2 or parts[0] != "config":
        return ""
    if str(method).upper() == "POST" or parts[-1].startswith("_"):
        return parts[1]
    return ""


def after(method: str, path: str, body: bytes, status: int) -> None:
    """
    Queue the hooks a change calls for and wake the renders that read the object. Never raises.
    """
    try:
        found = notice(method, path, body, status)
        if found is not None:
            identifier = tasks.submit(LUNA_HOOK_SUBSYSTEM, found["event"],
                                      json.dumps(found["payload"]), "pass-through")
            LOGGER.info("%s at Luna for %s, queued as %s", found["event"],
                        found["payload"]["name"], identifier)
        touched = changed_object(method, path, status)
        if touched:
            renders().notify(f"luna.{touched}")
    except Exception as error:
        LOGGER.error("after %s %s: %s", method, path, error)


def run(task: str, param: str = "") -> str:
    """
    The task behind a queued change: every plugin that declared the event gets the payload.
    """
    payload = json.loads(param or "{}")
    outcomes = []
    for plugin, handler in registry().hooks_for(task):
        try:
            outcomes.append(f"{plugin}: {handler(**payload)}")
        except Exception as error:
            LOGGER.error("%s could not act on %s: %s", plugin, task, error)
            outcomes.append(f"{plugin}: failed: {error}")
    return "; ".join(outcomes) or f"nothing declared {task}"


def install() -> None:
    """
    Register the task that runs the hooks, before the worker starts requeuing old ones.
    """
    tasks.register(LUNA_HOOK_SUBSYSTEM, run)
