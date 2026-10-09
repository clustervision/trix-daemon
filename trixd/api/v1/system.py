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

from datetime import datetime

from fastapi import APIRouter, Body, Depends, Path, Query, Request

from pydantic import BaseModel

from trixd.api.v1 import docs
from trixd.common import logging as trixlog
from trixd.common.constants import (API_PREFIX, DAEMON_NAME, DAEMON_VERSION,
                                   PLUGIN_STATE_LOADED, ROLE_READER, ROLE_ROOTUS)
from trixd.common.errors import BadRequest, NotFound, PluginRefused
from trixd.core.plugins import loader
from trixd.core.plugins.registry import current as registry
from trixd.core.replication import tables
from trixd.core.replication.sweep import current as sweep
from trixd.core.render.engine import current as renders
from trixd.core.render.triggers import current as luna_hashes
from trixd.core.security import audit
from trixd.core.security.gate import Caller, guard
from trixd.db import engine as database

LOGGER = trixlog.get("system")

router = APIRouter(prefix=f"{API_PREFIX}/system", tags=["system"])
# What the document shows for a plugin, as a controller answered it.
PLUGIN_EXAMPLE: dict = {
    "name": "directory", "state": "loaded", "reason": "", "digest": "8c1f4be27a90d3e5",
    "origin": "/trinity/local/python/lib/python3.10/site-packages/trixd/plugins/directory",
    "routes": 10}


class Change(BaseModel):
    """
    Which kind of Luna object changed, and optionally which one.
    """

    object: str = ""
    name: str = ""


@router.get("/health", summary="Whether this daemon is serving, its renders and triggers",
            description=(
                "For a monitor, with no token. `plugins` counts the loaded plugins. Each "
                "render says when it last ran, as Unix time, 0 before its first run since "
                "trixd or its plugin started, and whether that run worked. `luna_hashes` "
                "says whether Luna answers its table hashes, which it does on an HA master "
                "only, and `replication` when the last sweep ran, 0 for never. States and "
                "times only: an error's text stays in the log."),
            responses=docs.answers({
                "status": "ok", "daemon": "trixd", "plugins": 6,
                "renders": [{"render": "prometheus/renders/targets",
                             "rendered_at": 1791545863.87, "ok": True},
                            {"render": "slurm/renders/config",
                             "rendered_at": 1791545864.38, "ok": True}],
                "triggers": {"luna_hashes": {"answering": False, "last_pass": 1791545823.43},
                             "replication": {"at": 1791545526.87}}}, "How trixd is doing"))
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


@router.get("/version", summary="What is running here",
            description=(
                "The trixd build running here and the API version, with no token. A build's "
                "version is the release line and its commit's time."),
            responses=docs.answers({"version": {"trixd": "1.0.1791545294", "api": 1}},
                                   "The build and the API version"))
def version() -> dict:
    """
    What is running here, for a client that has to ask.
    """
    return {"version": {"trixd": DAEMON_VERSION, "api": 1}}


@router.get("/plugins", summary="Every plugin, its state and why",
            description=(
                "Every plugin trixd knows. `state` is `loaded`, `refused` or `disabled`, and "
                "`reason` says why a plugin was refused. `digest` is the start of its "
                "manifest's hash, which changes when the manifest does; `origin` is the "
                "directory it was loaded from; `routes` is how many routes it serves."),
            responses=docs.answers({"plugins": [PLUGIN_EXAMPLE]}, "Every plugin"))
def plugins(caller: Caller = Depends(guard(ROLE_READER))) -> dict:
    """
    Every plugin, its state, its digest and why it was refused.
    """
    return {"plugins": registry().listing()}


@router.get("/audit", summary="Who did what here, newest first",
            description=(
                "The audit trail: every decision the gate made and every change, with who, "
                "the verb or action, the object, and the outcome. For a refusal, `detail` "
                "says what the route needed and what the caller held. Rows are kept for "
                "`[AUDIT] KEEP`, 90 days by default."),
            responses=docs.answers({"audit": [{
                "at": "2026-10-09T11:37:43.532228+00:00", "principal": "alice",
                "action": "PATCH", "object": "/api/v1/prometheus/rules",
                "outcome": "refused", "detail": "needs manager, holds reader"}]},
                "The rows, newest first", errors={400: "since is not an ISO 8601 time"}))
def audit_trail(principal: str = Query("", description="Only this user's rows",
                                       openapi_examples=docs.example("alice")),
                outcome: str = Query("", pattern="^(|granted|refused)$",
                                     description="granted or refused; empty for both",
                                     openapi_examples=docs.example("refused")),
                since: str = Query("", description="Only rows from this ISO 8601 time on",
                                   openapi_examples=docs.example("2026-10-09T00:00:00Z")),
                limit: int = Query(100, ge=1, le=audit.READ_LIMIT,
                                   description=f"How many rows, 1 to {audit.READ_LIMIT}",
                                   openapi_examples=docs.example(100)),
                caller: Caller = Depends(guard(ROLE_READER))) -> dict:
    """
    The audit trail, filtered by who, outcome and an ISO 8601 start time.
    """
    try:
        start = datetime.fromisoformat(since.replace("Z", "+00:00")).replace(tzinfo=None)             if since else None
    except ValueError:
        raise BadRequest(f"{since} is not an ISO 8601 time") from None
    return {"audit": audit.read(principal, outcome, start, limit)}


@router.post("/plugins/{name}/reload", summary="Plug a plugin in, or reload it in place",
             description=(
                 "Reads the plugin from disk again and swaps its routes in, with no restart: "
                 "a request already running finishes on the old code, the next one sees the "
                 "new. It also plugs in a plugin that is not loaded yet, such as a site's "
                 "new one. When the plugin is refused, nothing changes and the answer is 409 "
                 "with the reason."),
             responses=docs.answers({"plugin": PLUGIN_EXAMPLE}, "The plugin as now loaded",
                                    errors={404: "No plugin has that name",
                                            409: "The plugin was refused; the message says why"}))
def reload_plugin(request: Request,
                  name: str = Path(description="The plugin's name",
                                   openapi_examples=docs.example("directory")),
                  caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    Discover, validate and swap. Nothing changes if the plugin is refused.
    """
    entry = loader.load(request.app, name)
    if entry.state != PLUGIN_STATE_LOADED:
        raise PluginRefused(entry.reason or f"{name} was refused")
    return {"plugin": entry.summary()}


@router.post("/plugins/{name}/disable", summary="Unplug a plugin",
             description=(
                 "Its routes stop answering from the next request, and a restart keeps it "
                 "unplugged until it is reloaded."),
             responses=docs.answers({"plugin": {**PLUGIN_EXAMPLE, "state": "disabled",
                                                "routes": 0}},
                                    "The plugin as now unplugged",
                                    errors={404: "No plugin has that name"}))
def disable_plugin(request: Request,
                   name: str = Path(description="The plugin's name",
                                    openapi_examples=docs.example("directory")),
                   caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    Its paths stop resolving from the next request, and a restart agrees.
    """
    entry = loader.unload(request.app, name)
    return {"plugin": entry.summary()}


@router.post("/notify", summary="Tell trixd a Luna object changed",
             description=(
                 "For a change trixd could not notice itself: the renders that read that "
                 "kind of object run again. `object` is a Luna table such as `node`, `group` "
                 "or `network`; left empty, every render runs. `rendered` lists the renders "
                 "that ran now; one that ran inside its two-second window runs again when the "
                 "window closes. Nothing calls this on its own; it is for an operator or a "
                 "script."),
             responses=docs.answers({"rendered": [
                 "genders/renders/genders", "prometheus/renders/rule-files",
                 "prometheus/renders/targets", "slurm/renders/config"]}, "The renders that ran"))
def notify(body: Change = Body(openapi_examples=docs.example({"object": "node",
                                                              "name": "node001"})),
           caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    Wake the renders that read this kind of object, or every render when none is named.
    Nothing calls it on its own; it is there for an operator or a script to nudge.
    """
    woken = renders().notify(f"luna.{body.object}" if body.object else "")
    LOGGER.info("notified of %s %s, woke %d render(s)", body.object, body.name, len(woken))
    return {"rendered": woken}


@router.get("/replication/hashes", summary="One hash per replicated table",
            description=(
                "One hash per replicated table, `principal` and `declaration`, which the "
                "other controller of a pair compares before copying anything: equal hashes "
                "mean there is nothing to copy."),
            responses=docs.answers({"hashes": {
                "principal": "22e22be985d048fd4994b5cd03e61f56ee19dbdcb40fefc50eae2db45145e61e",
                "declaration": "e55ea11cd9f8b8cc609f6eeae589853776487c1e5c01f0ef798e7564d83c8b6f"}},
                "One hash per table"))
def replication_hashes(caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    What a peer compares against before copying anything.
    """
    handle = database.session()
    try:
        return {"hashes": tables.hashes(handle)}
    finally:
        handle.close()


@router.get("/replication/table/{name}", summary="One replicated table as documents",
            description=(
                "One replicated table as the documents the other controller reads: each row "
                "keyed by name, with the `version`, `origin` and `updated_at` that decide "
                "which copy wins, and a deletion kept as a marker. `principal` rows carry "
                "the password digest the peer needs, which is why this is rootus only."),
            responses=docs.answers({"table": "declaration", "documents": [{
                "version": 53, "origin": "dev2-rocky9-controller1",
                "updated_at": "2026-10-06T09:09:16.177187+00:00",
                "key": ["genders", "config", ""], "plugin": "genders", "kind": "config",
                "name": "", "body": {"genders_file": "/etc/genders", "manager": "TrinityX"}}]},
                "The table's documents", errors={404: "Not a replicated table"}))
def replication_table(name: str = Path(description="principal or declaration",
                                       openapi_examples=docs.example("declaration")),
                      caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
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


@router.post("/replication/sweep", summary="Reconcile with every peer now",
             description=(
                 "The pass the timer makes every `[REPLICATION] INTERVAL`, run now: for each "
                 "peer, compare the table hashes and copy what differs. One result per peer, "
                 "naming the tables that differed and what was copied; an empty list on a "
                 "single controller, which has no peer."),
             responses=docs.answers({"results": []}, "One result per peer"))
def replication_sweep(caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    The same pass the timer makes, run on demand.
    """
    return {"results": sweep().once()}
