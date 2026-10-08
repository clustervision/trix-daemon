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
Health, version, the plugin control surface, the notify endpoint and replication.
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

from trixd.common import logging as trixlog
from trixd.common.constants import (API_PREFIX, DAEMON_NAME, DAEMON_VERSION,
                                   PLUGIN_STATE_LOADED, ROLE_READER, ROLE_ROOTUS)
from trixd.common.errors import NotFound, PluginRefused
from trixd.core.plugins import loader
from trixd.core.plugins.registry import current as registry
from trixd.core.replication import tables
from trixd.core.replication.sweep import current as sweep
from trixd.core.render.engine import current as renders
from trixd.core.render.triggers import current as luna_hashes
from trixd.core.security.gate import Caller, guard
from trixd.db import engine as database

LOGGER = trixlog.get("system")

router = APIRouter(prefix=f"{API_PREFIX}/system", tags=["system"])


class Change(BaseModel):
    """
    Which kind of Luna object changed, and optionally which one.
    """

    object: str = ""
    name: str = ""


@router.get("/health", summary="Whether this daemon is serving, its renders and triggers")
def health() -> dict:
    """
    Unauthenticated on purpose: a health check that needs a token is not one.
    So it carries states and times, never an error text: that stays in the log.
    """
    loaded = [entry for entry in registry().plugins.values()
              if entry.state == PLUGIN_STATE_LOADED]
    hashes = luna_hashes().state()
    return {"status": "ok", "daemon": DAEMON_NAME, "plugins": len(loaded),
            "renders": [{"render": item["render"], "rendered_at": item["rendered_at"],
                         "ok": not item["error"]} for item in renders().listing()],
            "triggers": {"luna_hashes": {"answering": hashes["answering"],
                                         "last_pass": hashes["last_pass"]},
                         "replication": {"at": (sweep().last or {}).get("at", 0)}}}


@router.get("/version", summary="What is running here")
def version() -> dict:
    """
    What is running here, for a client that has to ask.
    """
    return {"version": {"trixd": DAEMON_VERSION, "api": 1}}


@router.get("/plugins", summary="Every plugin, its state and why")
def plugins(caller: Caller = Depends(guard(ROLE_READER))) -> dict:
    """
    Every plugin, its state, its digest and why it was refused.
    """
    return {"plugins": registry().listing()}


@router.post("/plugins/{name}/reload", summary="Plug a plugin in, or reload it in place")
def reload_plugin(name: str, request: Request,
                  caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    Discover, validate and swap. Nothing changes if the plugin is refused.
    """
    entry = loader.load(request.app, name)
    if entry.state != PLUGIN_STATE_LOADED:
        raise PluginRefused(entry.reason or f"{name} was refused")
    return {"plugin": entry.summary()}


@router.post("/plugins/{name}/disable", summary="Unplug a plugin")
def disable_plugin(name: str, request: Request,
                   caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    Its paths stop resolving from the next request, and a restart agrees.
    """
    entry = loader.unload(request.app, name)
    return {"plugin": entry.summary()}


@router.post("/notify", summary="A Luna object changed")
def notify(body: Change, caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    Wake the renders that read this kind of object, or every render when none is named.
    Nothing calls it on its own; it is there for an operator or a script to nudge.
    """
    woken = renders().notify(f"luna.{body.object}" if body.object else "")
    LOGGER.info("notified of %s %s, woke %d render(s)", body.object, body.name, len(woken))
    return {"rendered": woken}


@router.get("/replication/hashes", summary="One hash per replicated table")
def replication_hashes(caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    What a peer compares against before copying anything.
    """
    handle = database.session()
    try:
        return {"hashes": tables.hashes(handle)}
    finally:
        handle.close()


@router.get("/replication/table/{name}", summary="One replicated table as documents")
def replication_table(name: str, caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
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
def replication_sweep(caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    The same pass the timer makes, run on demand.
    """
    return {"results": sweep().once()}
