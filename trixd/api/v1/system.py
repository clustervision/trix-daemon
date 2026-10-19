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

import inspect
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, Path, Query, Request

from pydantic import BaseModel, Field, ValidationError

from trixd.api.v1 import docs
from trixd.common import logging as trixlog
from trixd.common.constants import (API_PREFIX, DAEMON_NAME, DAEMON_VERSION,
                                   HOOK_EVENT_PARTS, NODE_PATH_PARAMS, PLUGIN_STATE_LOADED,
                                   ROLE_OPERATOR, ROLE_READER, ROLE_ROOTUS)
from trixd.common.errors import BadRequest, NotFound, PluginRefused, ServiceUnavailable
from trixd.core import tasks
from trixd.core.plugins import loader
from trixd.core.plugins.registry import current as registry
from trixd.core.replication import applied, outbox, tables
from trixd.core.replication.sweep import current as sweep
from trixd.core.render.engine import current as renders
from trixd.core.render import triggers
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
    What changed, named as <source>.<object>.<event>, and whatever the source holds about it.
    """

    event: str = ""
    payload: dict = Field(default_factory=dict)


class Changes(BaseModel):
    """
    Several changes in one call, in the order they are to be acted on.
    """

    changes: list[Change] = Field(default_factory=list)


class Action(BaseModel):
    """
    One call a peer has already made and this controller has to make too.
    """

    plugin: str = ""
    path: str = ""
    method: str = ""
    variables: dict = Field(default_factory=dict)
    # Optional, not "dict with a None default": an endpoint that takes no body sends null,
    # and a plain `dict = None` refuses that with a 422 the sender can only treat as
    # terminal. A DELETE is exactly that case.
    body: Optional[dict] = None
    origin: str = ""
    request_id: str = ""


@router.get("/health", summary="Whether this daemon is serving, its renders and triggers",
            description=(
                "For a monitor, with no token. `plugins` counts the loaded plugins. Each "
                "render says when it last ran, as Unix time, 0 before its first run since "
                "trixd or its plugin started, and whether that run worked. `triggers` carries one "
                "entry per loaded trigger: `luna_hashes` says whether Luna answers its table "
                "hashes, which it does on an HA master only, and `replication` when the last "
                "sweep ran, 0 for never, with `not_converging` counting the peer and table "
                "pairs that have still differed after several passes - which means neither "
                "controller is taking the other's row. `owed` counts the replicated calls still "
                "waiting for a peer, the ones given up on, the peers configured and the "
                "peers not answering - counts only, and no peer is named, because this "
                "route takes no token: `GET /system/replication/owed` says where, and "
                "`oldest_pending` says in seconds how long the oldest waiting call has "
                "waited, which is what tells a busy queue from a stuck one. States "
                "and times only: an error's text stays in the log."),
            responses=docs.answers({
                "status": "ok", "daemon": "trixd", "plugins": 6,
                "renders": [{"render": "prometheus/renders/targets",
                             "rendered_at": 1791545863.87, "ok": True},
                            {"render": "slurm/renders/config",
                             "rendered_at": 1791545864.38, "ok": True}],
                "triggers": {"luna_hashes": {"answering": False, "last_pass": 1791545823.43},
                             "replication": {"at": 1791545526.87, "not_converging": 0}},
                "owed": {"pending": 0, "failed": 0, "peers": 1, "not_answering": 0,
                         "oldest_pending": 0}},
                "How trixd is doing"))
def health() -> dict:
    """
    Unauthenticated on purpose: a health check that needs a token is not one.
    So it carries states and times, never an error text: that stays in the log.
    """
    loaded = [entry for entry in registry().plugins.values()
              if entry.state == PLUGIN_STATE_LOADED]
    return {"status": "ok", "daemon": DAEMON_NAME, "plugins": len(loaded),
            "renders": [{"render": item["render"], "rendered_at": item["rendered_at"],
                         "ok": not item["error"]} for item in renders().listing()],
            "triggers": {**{name: {"answering": state.get("answering", False),
                                   "last_pass": state.get("last_pass", 0)}
                            for name, state in triggers.states().items()},
                         "replication": {"at": (sweep().last or {}).get("at", 0),
                                         "not_converging": sweep().stalled()}},
            "owed": outbox.states()}


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
                "The audit trail: every decision the gate reached about a caller it could "
                "identify, granted and refused alike, every change, and a sign-in whose "
                "password was wrong - with who, the verb or action, the object, and the "
                "outcome. For a refusal, `detail` says what the route needed and what the "
                "caller held. A credential that did not verify at all is not here: a token "
                "missing, malformed, expired, signed by an issuer this daemon does not "
                "trust, or of the wrong kind is refused before the gate knows who is "
                "asking, and leaves its trace in the log instead. Rows are kept for "
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
        start = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    except ValueError:
        raise BadRequest(f"{since} is not an ISO 8601 time") from None
    # `Audit.at` is the naive UTC time the column stores, so an offset is converted rather
    # than dropped: discarding it filters from the wall-clock reading and loses every row
    # between the two, under a 200. Same idiom as core/replication/tables.py.
    if start is not None and start.tzinfo is not None:
        start = start.astimezone(timezone.utc).replace(tzinfo=None)
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


@router.post("/notify", summary="Tell trixd that something it did not see has changed",
             description=(
                 "For a change trixd could not notice itself. `event` names it as "
                 "`<source>.<object>.<event>`: who holds the thing, what it is, and what "
                 "happened to it, such as `luna.node.update`. Every plugin that declared "
                 "that event acts on it, and the renders that read the object run again. "
                 "`payload` is whatever the source holds about the change and is passed "
                 "on unchanged.\n\nA plugin that declared nothing is not called, and an "
                 "event nobody declared is not an error: `acted` then says so and the "
                 "renders still run. Ring it once after a sweep rather than once per "
                 "node; a whole set belongs in the plugin's own bulk route.\n\nLeft "
                 "empty, every render runs and nothing is dispatched, which is the "
                 "operator's nudge."),
             responses=docs.answers({"event": "luna.node.update",
                                     "acted": "prometheus: node001: moved [], rewritten []",
                                     "rendered": []}, "What acted, and what ran"))
def notify(body: Change = Body(openapi_examples=docs.example(
               {"event": "luna.node.update", "payload": {"name": "node001"}})),
           caller: Caller = Depends(guard(ROLE_OPERATOR))) -> dict:
    """
    Hand the change to whatever publishes that kind of event, which runs the plugins
    that declared it and wakes the renders. With no event, wake every render.
    """
    if not body.event:
        woken = renders().notify("")
        LOGGER.info("nudged, woke %d render(s)", len(woken))
        return {"event": "", "acted": "", "rendered": woken}

    parts = body.event.split(".")
    if len(parts) != HOOK_EVENT_PARTS or not all(parts):
        raise BadRequest(f"{body.event} is not an event: name one as "
                         f"<source>.<object>.<event>, such as luna.node.update")
    dispatch = tasks.handler(parts[0])
    if dispatch is None:
        raise BadRequest(f"nothing here publishes {parts[0]} events")
    said = dispatch(body.event, json.dumps(body.payload or {}))
    LOGGER.info("notified of %s: %s", body.event, said)
    return {"event": body.event, "acted": said, "rendered": []}


@router.post("/bulk", status_code=202,
             summary="Hand trixd a set of changes to act on, and do not wait",
             description=(
                 "For a caller that must not be held up: the changes are written down and "
                 "the answer comes at once, with the request id this daemon logs each one "
                 "under, so a caller's own log can be read against ours. Use this rather "
                 "than `/system/notify` from anything inside another service's request, such as "
                 "a hook: notify acts before it answers, and acting reads back from the "
                 "source, so a source waiting on trixd would be waiting on itself.\n\n"
                 "Each change is named as `<source>.<object>.<event>` with whatever the "
                 "source holds about it, exactly as `/system/notify` takes one. Send the "
                 "whole set in one call: a caller with a hundred changed nodes makes one "
                 "request, not a hundred.\n\nEvery change is checked before any is "
                 "queued, so a set is taken whole or refused whole."),
             responses=docs.answers({"queued": ["1791545863-2051-0f3ab9c1",
                                                "1791545863-2051-7d2e4a06"]},
                                    "One request id per change, in order",
                                    errors={400: "Not an event, or nothing publishes it"},
                                    status=202))
def bulk(body: Changes = Body(openapi_examples=docs.example(
             {"changes": [{"event": "luna.node.update",
                           "payload": {"name": "node001", "group": "compute"}},
                          {"event": "luna.node.delete", "payload": {"name": "node002"}}]})),
         caller: Caller = Depends(guard(ROLE_OPERATOR))) -> dict:
    """
    Write the set down and answer; the worker acts on it in the order it was given.
    """
    if not body.changes:
        raise BadRequest("name at least one change, each an event and its payload")

    # Checked in full first: a set half-queued is worse than one refused, because the
    # caller is told it failed while part of it is already on its way.
    for change in body.changes:
        parts = change.event.split(".")
        if len(parts) != HOOK_EVENT_PARTS or not all(parts):
            raise BadRequest(f"{change.event} is not an event: name one as "
                             f"<source>.<object>.<event>, such as luna.node.update")
        if tasks.handler(parts[0]) is None:
            raise BadRequest(f"nothing here publishes {parts[0]} events")

    queued = [tasks.submit(change.event.split(".")[0], change.event,
                           json.dumps(change.payload or {}), caller.name)
              for change in body.changes]
    LOGGER.info("%s handed us %d change(s): %s", caller.name, len(queued),
                ", ".join(change.event for change in body.changes))
    return {"queued": queued}


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


@router.get("/replication/owed", summary="What this controller still owes its peers",
            description=(
                "Every replicated call that has not reached a peer yet, oldest first, with "
                "the peer it is for, what it would do, how many times it has been offered, "
                "and the last refusal verbatim. A peer that is not answering is named "
                "first.\n\n"
                "`/system/health` carries the counts and takes no token; this carries the "
                "peers and the rows and therefore does, because naming the other "
                "controllers is not something an unauthenticated port should do.\n\n"
                "A row listed as `failed` has been given up on and stops everything behind "
                "it for that peer: later actions assume the earlier one landed, so carrying "
                "on would write over a prerequisite that never happened."),
            responses=docs.answers({"not_answering": [],
                                    "actions": [{"peer": "ctrl2", "plugin": "prometheus",
                                                 "path": "hw-rules/<node>",
                                                 "method": "DELETE",
                                                 "target": "node=node001",
                                                 "state": "queued", "attempts": 2,
                                                 "message": "",
                                                 "created": "2026-10-15T22:52:39"}]},
                                   "The peers not answering, and one entry per owed action"))
def replication_owed(caller: Caller = Depends(guard(ROLE_READER))) -> dict:
    """
    The rows behind health's counts.
    """
    return outbox.owed()


@router.post("/replication/apply", summary="Do what a peer already did",
             description=(
                 "The other controller made a call whose effect is not in a replicated "
                 "table and which no render brings back - a hardware rule file written or "
                 "retired, for instance - so it asks this controller to make the same "
                 "call. Only an endpoint whose own manifest says `replicate` can be asked "
                 "for, which is checked here as well as there, so a version-skewed pair "
                 "refuses instead of being talked into work it does not declare. A plugin "
                 "this controller does not have plugged in is answered 503 rather than "
                 "404: the call is well formed and plugging it back in makes it apply, so "
                 "the sender keeps the action and offers it again.\n\n"
                 "The named handler is called directly rather than the route being "
                 "re-entered, so nothing is written down to send onwards: there is no loop "
                 "to prevent and no hop count to get wrong. rootus only, which is the "
                 "account the pair already shares for replication.\n\n"
                 "Two conventions a replicating handler's answer carries. `skipped`, as a "
                 "non-empty string, says what the handler did not do - a node that is not "
                 "in this controller's Luna, or the entries of a batch it could not apply - "
                 "and the sender then treats the call as not done and offers it again "
                 "rather than recording it as applied; it is a reserved key, and a count or "
                 "list of left-out items must not use it. Offering the "
                 "same call again is answered with the answer given the first time, since "
                 "delivery is at-least-once and the sender's wait is bounded: brief when "
                 "a caller's own request offers the call, longer on a sweep pass."),
             responses=docs.answers({"applied": "prometheus hw-rules/<node>",
                                     "origin": "dev2-rocky9-controller1"},
                                    "What was applied",
                                    errors={400: "Not an endpoint that declares replicate",
                                            404: "No such endpoint on this controller",
                                            503: "That plugin is not loaded here"}))
def replication_apply(body: Action = Body(openapi_examples=docs.example(
                          {"plugin": "prometheus", "path": "hw-rules/<node>",
                           "method": "DELETE", "variables": {"node": "node001"},
                           "body": None, "origin": "dev2-rocky9-controller1",
                           "request_id": "1791545863-2051-0f3ab9c1"})),
                      caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    Find the endpoint the peer names, check its manifest allows this, and call its handler.
    """
    already = applied.recorded(body.origin, body.request_id)
    if already is not None:
        # Delivery is at-least-once: a sender whose wait ran out - brief on a caller's
        # request thread, longer on a sweep pass - offers the row again at every pass.
        # Answering as we answered then is what makes a slow apply harmless; running it
        # again is how one write lands repeatedly.
        LOGGER.info("%s %s %s was already applied for %s (%s), so the answer is the one "
                    "given then", body.method, body.plugin, body.path, body.origin,
                    body.request_id)
        return already
    entry = registry().plugins.get(body.plugin)
    if entry is None or entry.state != PLUGIN_STATE_LOADED:
        # Retryable, not malformed: the action names a plugin, and whether this controller
        # has that plugin plugged in is this controller's state. Terminal on the first
        # attempt, every write aimed at it was dropped with no replicated table to repair it.
        raise ServiceUnavailable(f"no plugin named {body.plugin} is loaded here")
    endpoint = next((item for item in entry.manifest.endpoints
                     if item.path == body.path
                     and body.method.upper() in [verb.upper() for verb in item.methods]),
                    None)
    if endpoint is None:
        raise NotFound(f"{body.plugin} serves no {body.method} {body.path}")
    if not endpoint.replicate:
        raise BadRequest(f"{body.plugin} {body.method} {body.path} does not declare "
                         f"replicate, so it is not ours to apply")
    handler = getattr(entry.backend, endpoint.handler, None)
    if handler is None:
        raise NotFound(f"{body.plugin} has no handler {endpoint.handler}")

    # The body the endpoint declares, checked against the model the binder bound rather than
    # a second one written beside it. A peer a release ahead sends a field this build does
    # not know, or omits one it now requires, and a refusal naming it beats the 500 a handler
    # reaching for a missing key produces - which the sender can only read as "retry for ever".
    model = (entry.models or {}).get(endpoint.body_schema) if endpoint.body_schema else None
    checked = None
    if model is not None:
        try:
            # Kept, not discarded. The ordinary route hands the handler what the model
            # produced, so a declared integer arrives as an integer and an optional field
            # the sender omitted arrives as None. Validating and then passing the raw body
            # on left the handler a string where it expected a number, and a missing key
            # where the route it was written against guarantees one - which is the 500 this
            # check exists to stop.
            checked = model(**(body.body or {})).model_dump()
        except ValidationError as error:
            raise BadRequest(f"{body.plugin} {body.method} {body.path} does not accept the "
                             f"body {body.origin} sent: {error}") from error

    # No user token reaches a node endpoint, not even rootus; this route does, because
    # applying for the node is the whole job. The peer and a person share the account, so
    # the two cannot be told apart here - the trail is what is left. A role-gated endpoint
    # needs the row for the same reason: the gate's own row names this route, not the node.
    variables = dict(body.variables or {})
    audit.granted(caller.name, "replication.apply",
                  next((str(variables[key]) for key in NODE_PATH_PARAMS if key in variables),
                       body.path),
                  f"{endpoint.token} endpoint {body.method} {body.path} reached through "
                  f"apply for {body.origin}")

    supplied = dict(variables)
    if checked is not None:
        supplied["body"] = checked
    elif body.body is not None:
        supplied["body"] = body.body
    accepted = inspect.signature(handler).parameters
    answer = handler(**{key: value for key, value in supplied.items() if key in accepted})
    LOGGER.info("applied %s %s %s for %s (%s)", body.method, body.plugin, body.path,
                body.origin, body.request_id)
    given = {"applied": f"{body.plugin} {body.path}", "origin": body.origin,
             "result": answer if isinstance(answer, (dict, list)) else str(answer)}
    turned_down = applied.declined(answer)
    if turned_down:
        # Not remembered. The sender is meant to offer this again - a decline here is often
        # a moment of Luna lag - and answering every retry from the record would hold the
        # decline for as long as the record lives.
        LOGGER.warning("did not apply %s %s %s for %s: %s", body.method, body.plugin,
                       body.path, body.origin, turned_down)
    else:
        applied.record(body.origin, body.request_id, given)
    return given


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
