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
Health, version, the plugin control surface and the notify endpoint Luna's hook calls.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from trix.common import logging as trixlog
from trix.common.constants import (API_PREFIX, DAEMON_NAME, DAEMON_VERSION,
                                   PLUGIN_STATE_LOADED)
from trix.common.errors import NotFound, PluginRefused
from trix.core.plugins import loader
from trix.core.plugins.registry import current as registry
from trix.core.replication import tables
from trix.core.replication.sweep import current as sweep
from trix.core.render.engine import current as renders
from trix.core.render.triggers import current as luna_hashes
from trix.core.security.gate import Caller, guard
from trix.db import engine as database

LOGGER = trixlog.get("system")

router = APIRouter(prefix=f"{API_PREFIX}/system", tags=["system"])


class Change(BaseModel):
    """
    What Luna's config hook posts when something it owns has changed.
    """

    object: str = ""
    name: str = ""


@router.get("/health", summary="Whether this daemon is serving")
def health() -> dict:
    """
    Unauthenticated on purpose: a health check that needs a token is not one.
    """
    loaded = [entry for entry in registry().plugins.values()
              if entry.state == PLUGIN_STATE_LOADED]
    return {"status": "ok", "daemon": DAEMON_NAME, "plugins": len(loaded)}


@router.get("/version", summary="What is running here")
def version() -> dict:
    return {"version": {"trix": DAEMON_VERSION, "api": 1}}


@router.get("/plugins", summary="Every plugin, its state and why")
def plugins(caller: Caller = Depends(guard("system.read"))) -> dict:
    return {"plugins": registry().listing()}


@router.post("/plugins/{name}/reload", summary="Plug a plugin in, or reload it in place")
def reload_plugin(name: str, request: Request,
                  caller: Caller = Depends(guard("system.write"))) -> dict:
    """
    Discover, validate and swap. Nothing changes if the plugin is refused.
    """
    entry = loader.load(request.app, name)
    if entry.state != PLUGIN_STATE_LOADED:
        raise PluginRefused(entry.reason or f"{name} was refused")
    return {"plugin": entry.summary()}


@router.post("/plugins/{name}/disable", summary="Unplug a plugin")
def disable_plugin(name: str, request: Request,
                   caller: Caller = Depends(guard("system.write"))) -> dict:
    """
    Its paths stop resolving from the next request, and a restart agrees.
    """
    entry = loader.unload(request.app, name)
    return {"plugin": entry.summary()}


@router.get("/renders", summary="Every declared render and when it last ran")
def render_listing(caller: Caller = Depends(guard("system.read"))) -> dict:
    return {"renders": renders().listing()}


@router.post("/notify", summary="A Luna object changed")
def notify(body: Change, caller: Caller = Depends(guard("system.notify"))) -> dict:
    """
    Luna's hook posts here and does not wait. Losing one is acceptable: the hash
    comparison on the slow timer catches up.
    """
    woken = renders().notify(f"luna.{body.object}" if body.object else "")
    LOGGER.info("notified of %s %s, woke %d render(s)", body.object, body.name, len(woken))
    return {"rendered": woken}


@router.get("/replication/hashes", summary="One hash per replicated table")
def replication_hashes(caller: Caller = Depends(guard("system.read"))) -> dict:
    """
    What a peer compares against before copying anything.
    """
    handle = database.session()
    try:
        return {"hashes": tables.hashes(handle)}
    finally:
        handle.close()


@router.get("/replication/table/{name}", summary="One replicated table as documents")
def replication_table(name: str, caller: Caller = Depends(guard("system.read"))) -> dict:
    """
    Addressed by name rather than by identifier, because identifiers are local.
    """
    if name not in tables.MODELS:
        raise NotFound(f"{name} is not a replicated table")
    handle = database.session()
    try:
        return {"table": name, "documents": tables.export_table(handle, name)}
    finally:
        handle.close()


@router.post("/replication/sweep", summary="Reconcile with every peer now")
def replication_sweep(caller: Caller = Depends(guard("system.write"))) -> dict:
    """
    The same pass the timer makes, run on demand.
    """
    return {"results": sweep().once()}


@router.get("/triggers", summary="The state of the fallback trigger")
def trigger_state(caller: Caller = Depends(guard("system.read"))) -> dict:
    """
    Whether Luna's table hashes are actually answering here.
    """
    return {"luna_hashes": luna_hashes().state(),
            "replication": sweep().last or {"at": 0, "results": []}}
