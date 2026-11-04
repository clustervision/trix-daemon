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
Turning a manifest into a router whose handlers are methods on the plugin backend.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import asyncio
import inspect
from typing import Optional

from fastapi import APIRouter, Body, Depends, Path, Query, Request, WebSocket
from pydantic import BaseModel
from starlette.status import WS_1008_POLICY_VIOLATION, WS_1013_TRY_AGAIN_LATER
from starlette.concurrency import run_in_threadpool

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX
from trixd.common.errors import Forbidden, PluginRefused, Unauthenticated
from trixd.core import tasks
from trixd.core.plugins.manifest import BODY_VERBS, QUERY_TYPES, Endpoint, Manifest
from trixd.core.replication import outbox
from trixd.core.security.gate import Caller, admit, guard, stamp

LOGGER = trixlog.get("binder")

# What the binder hands a handler besides the path variables.
SUPPLIED: tuple = ("body", "caller", "request")
# And what it hands a WebSocket handler, which has no request and no body.
SOCKET_SUPPLIED: tuple = ("websocket", "caller")
# A guarded socket that has sent no token by then is closed, so it cannot be held unjudged.
FIRST_MESSAGE_WAIT: float = 5.0
# Open sockets one plugin may hold, judged or not; past it a connect is closed with 1013.
SOCKETS_PER_PLUGIN: int = 64
# A close reason is at most this many bytes.
CLOSE_REASON_BYTES: int = 123


def signature_problems(handler, endpoint: Endpoint, supplied: tuple = SUPPLIED) -> list:
    """
    What the binder could not give this handler: a path variable it does not take, an
    argument it will only take positionally, or an argument with no default that nothing
    supplies. Each would be a 500 at the first call. `supplied` is what the binder hands
    over besides the path variables and the query.
    """
    try:
        accepted = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return []
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in accepted.values()):
        return []
    problems = [f"{endpoint.handler} does not take the path variable {name}"
                for name in endpoint.variables if name not in accepted]
    known = set(endpoint.variables) | set(supplied) | set(endpoint.query)
    for name, item in accepted.items():
        if item.kind == inspect.Parameter.VAR_POSITIONAL:
            continue
        if item.kind == inspect.Parameter.POSITIONAL_ONLY and name in known:
            # Everything is handed over by keyword, so this one is never filled: the call
            # raises before the handler's first line, on every request.
            problems.append(f"{endpoint.handler} takes {name} positionally only, and a "
                            f"handler is given its arguments by keyword")
            continue
        if name in known:
            continue
        if item.default is inspect.Parameter.empty:
            problems.append(f"{endpoint.handler} needs {name}, which nothing supplies")
    return problems


def endpoint_problems(manifest: Manifest, backend, endpoint: Endpoint) -> list:
    """
    What one endpoint could not be bound to: a handler the backend does not have, or a
    signature the route could not call.

    The command-line check calls this too, so what it passes is what the load path accepts.
    These were two copies of one pair of refusals, and the wording had already drifted.
    Collected rather than raised, because the check names every problem it finds in one run
    while the load path refuses on the first.
    """
    where = endpoint.path or "/"
    handler = getattr(backend, endpoint.handler, None)
    if handler is None or not callable(handler):
        return [f"{where} names handler {endpoint.handler}, "
                f"which {manifest.backend} does not have"]
    if endpoint.websocket:
        if not inspect.iscoroutinefunction(handler):
            # A plain function would block the one event loop for as long as the socket is open.
            return [f"{where}: {endpoint.handler} serves a websocket, so it must be async"]
        if "websocket" not in inspect.signature(handler).parameters:
            return [f"{where}: {endpoint.handler} does not take the websocket"]
        return [f"{where}: {problem}"
                for problem in signature_problems(handler, endpoint, supplied=SOCKET_SUPPLIED)]
    return [f"{where}: {problem}" for problem in signature_problems(handler, endpoint)]


def _call(handler, supplied: dict):
    """
    Pass a handler only the arguments it actually declares.
    """
    accepted = inspect.signature(handler).parameters
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in accepted.values()):
        return handler(**supplied)
    return handler(**{key: value for key, value in supplied.items() if key in accepted})


def _arguments(kwargs: dict) -> dict:
    """
    What the backend is handed: path variables, the body as a dict, the caller, and the
    request itself for a handler that asks for it.

    A verb that carries no body hands one anyway, as None: that is how a handler serving
    both a read and a write tells them apart, and leaving it out answered 500 on the read
    for a handler that declares `body` without a default.
    """
    caller = kwargs.pop("caller", None)
    body = kwargs.pop("body", None)
    supplied = dict(kwargs)
    supplied["body"] = body.model_dump() if isinstance(body, BaseModel) else body
    supplied["caller"] = caller
    return supplied


def _examples(value) -> dict:
    """
    One example, in the shape Swagger UI lists.
    """
    return {"example": {"value": value}} if value is not None else None


def _documented(endpoint: Endpoint) -> list:
    """
    The path variables, described, and the query parameters the manifest declares, so the
    document shows what to send. A handler that reads the query itself still can.
    """
    parameters = []
    for name in endpoint.variables:
        doc = endpoint.params.get(name)
        parameters.append(inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY, annotation=str,
            default=Path(description=doc.description if doc else None,
                         openapi_examples=_examples(doc.example) if doc else None)))
    for name, doc in endpoint.query.items():
        parameters.append(inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY, annotation=Optional[QUERY_TYPES[doc.type]],
            default=Query(None, description=doc.description or None,
                          openapi_examples=_examples(doc.example))))
    return parameters


def _responses(endpoint: Endpoint) -> dict:
    """
    What a call answers, as the manifest documents it: the success, and its own refusals.
    """
    answers = {}
    if endpoint.response is not None and endpoint.response.example is not None:
        answers[200] = {"content": {endpoint.response.media_type:
                                    {"example": endpoint.response.example}}}
    for status, meaning in endpoint.errors.items():
        answers[status] = {"description": meaning, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Error"}}}}
    return answers


def _replicated(endpoint: Endpoint, plugin: str, verb: str, supplied: dict, result):
    """
    Tell the peers about a call that has already happened here, and put what happened in
    the answer. Returns the result, with a `replicated` block when there is one to add.

    Takes what the handler was handed, not the raw kwargs: `_arguments` pops `body` and
    `caller` out of those, so reading them there gives a null body and no origin on every
    call - which the peer accepts and acts on wrongly.

    Only reached for an endpoint whose manifest says so, and only after its handler
    returned: a call that raised changed nothing to pass on.
    """
    body = supplied.get("body")
    caller = supplied.get("caller")
    variables = {key: value for key, value in supplied.items()
                 if key not in ("body", "caller", "request")}
    said = outbox.replicate(plugin, endpoint.path, verb, variables, body, result,
                            tasks.request_id(), getattr(caller, "name", "local"))
    if isinstance(result, dict):
        return {**result, "replicated": said}
    # Nothing to attach it to, so the log is where it is said. A manifest cannot reach
    # here with a render, which is the only shipped handler that answers a bare list.
    LOGGER.info("replicated %s %s: %s", verb, endpoint.path, said)
    return result


def _endpoint_function(handler, endpoint: Endpoint, body_model, plugin: str = "",
                       verb: str = ""):
    """
    Build a path operation whose signature FastAPI can introspect.
    """
    if inspect.iscoroutinefunction(handler):
        async def operation(**kwargs):
            """
            The path operation FastAPI awaits, for a handler that is itself async.
            """
            supplied = _arguments(kwargs)
            answer = await _call(handler, supplied)
            if endpoint.replicate:
                return _replicated(endpoint, plugin, verb, supplied, answer)
            return answer
    else:
        def operation(**kwargs):
            """
            The path operation FastAPI calls, which hands the backend its arguments.
            """
            supplied = _arguments(kwargs)
            answer = _call(handler, supplied)
            if endpoint.replicate:
                return _replicated(endpoint, plugin, verb, supplied, answer)
            return answer

    parameters = _documented(endpoint)
    if "request" in inspect.signature(handler).parameters:
        parameters.append(inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY,
                                            annotation=Request))
    if body_model is not None:
        example = _examples(endpoint.request_example)
        parameters.append(inspect.Parameter(
            "body", inspect.Parameter.KEYWORD_ONLY, annotation=body_model,
            **({"default": Body(..., openapi_examples=example)} if example else {})))
    if not endpoint.public and not endpoint.forward:
        parameters.append(inspect.Parameter(
            "caller", inspect.Parameter.KEYWORD_ONLY, annotation=Caller,
            default=Depends(guard(endpoint.minimum, endpoint.token))))

    operation.__signature__ = inspect.Signature(parameters)
    operation.__name__ = (endpoint.handler or "render").replace("-", "_")
    operation.__doc__ = endpoint.summary or None
    return operation


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """
    Close the socket with a code and a reason short enough to be sent.
    """
    await websocket.close(code, reason.encode()[:CLOSE_REASON_BYTES].decode(errors="ignore"))


async def _admitted(websocket: WebSocket, endpoint: Endpoint) -> Optional[Caller]:
    """
    The caller whose token is the socket's first message, judged as a route judges one;
    None once the socket is closed instead.
    """
    try:
        first = await asyncio.wait_for(websocket.receive(), FIRST_MESSAGE_WAIT)
    except asyncio.TimeoutError:
        await _refuse(websocket, WS_1008_POLICY_VIOLATION, "no token in time")
        return None
    if first["type"] == "websocket.disconnect":
        return None
    try:
        # Off the loop, as FastAPI runs a route's guard: resolving reads the database.
        return await run_in_threadpool(
            admit, first.get("text") or "", endpoint.minimum, endpoint.token, "WEBSOCKET",
            websocket.url.path, websocket.path_params)
    except (Unauthenticated, Forbidden) as refusal:
        await _refuse(websocket, WS_1008_POLICY_VIOLATION, refusal.message)
        return None


def _socket_function(handler, endpoint: Endpoint, held: set):
    """
    Build a WebSocket operation: the socket is accepted and its caller judged here, and
    the handler is handed both.
    """

    async def operation(websocket: WebSocket, **kwargs):
        """
        The WebSocket operation FastAPI awaits.
        """
        await websocket.accept()
        if len(held) >= SOCKETS_PER_PLUGIN:
            await _refuse(websocket, WS_1013_TRY_AGAIN_LATER, "too many open sockets")
            return
        held.add(websocket)
        try:
            caller = None if endpoint.public else await _admitted(websocket, endpoint)
            if endpoint.public or caller is not None:
                await _call(handler, {**kwargs, "websocket": websocket, "caller": caller})
        finally:
            held.discard(websocket)

    operation.__signature__ = inspect.Signature(
        [inspect.Parameter("websocket", inspect.Parameter.KEYWORD_ONLY, annotation=WebSocket)]
        + _documented(endpoint))
    operation.__name__ = endpoint.handler.replace("-", "_")
    operation.__doc__ = endpoint.summary or None
    return operation


def build(manifest: Manifest, backend, models: dict, renderer=None) -> APIRouter:
    """
    One router for the whole plugin. Refuses rather than serving half of it.
    """
    router = APIRouter(prefix=f"{API_PREFIX}/{manifest.plugin}", tags=[manifest.plugin])
    sockets: set = set()

    for endpoint in manifest.endpoints:
        if endpoint.render is not None:
            # A render that only writes files has no path of its own to serve.
            if not endpoint.render.serve:
                continue
            if renderer is None:
                raise PluginRefused(f"{endpoint.path} declares a render with no engine")
            handler = renderer.reader(manifest.plugin, endpoint.path)
        else:
            problems = endpoint_problems(manifest, backend, endpoint)
            if problems:
                raise PluginRefused(problems[0])
            handler = getattr(backend, endpoint.handler)

        path = "/" + endpoint.route_path if endpoint.route_path else ""
        if endpoint.websocket:
            router.add_api_websocket_route(
                path, _socket_function(handler, endpoint, sockets),
                name=f"{manifest.plugin}.{endpoint.handler}.websocket")
            LOGGER.debug("bound websocket %s%s", router.prefix, path)
            continue
        body_model = models.get(endpoint.body_schema) if endpoint.body_schema else None
        for verb in endpoint.methods:
            verb = verb.upper()
            wants_body = body_model if verb in BODY_VERBS else None
            router.add_api_route(
                path,
                _endpoint_function(handler, endpoint, wants_body, manifest.plugin, verb),
                methods=[verb],
                name=f"{manifest.plugin}.{endpoint.handler or endpoint.path}.{verb.lower()}",
                summary=endpoint.summary or f"{verb} {endpoint.path}",
                description=endpoint.description or endpoint.summary or None,
                response_description=(endpoint.response.description
                                      if endpoint.response and endpoint.response.description
                                      else "Successful Response"),
                responses=_responses(endpoint) or None,
                openapi_extra=({"x-trixd-access": "forward", "x-trixd-minimum": ""}
                               if endpoint.forward else None),
            )
            LOGGER.debug("bound %s %s/%s", verb, router.prefix, endpoint.route_path)
    stamp(router)
    return router
