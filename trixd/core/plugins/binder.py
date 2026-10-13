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

import inspect
from typing import Optional

from fastapi import APIRouter, Body, Depends, Path, Query, Request
from pydantic import BaseModel

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX
from trixd.common.errors import PluginRefused
from trixd.core.plugins.manifest import QUERY_TYPES, Endpoint, Manifest
from trixd.core.security.gate import Caller, guard, stamp

LOGGER = trixlog.get("binder")

BODY_VERBS: tuple = ("POST", "PATCH", "PUT")
# What the binder hands a handler besides the path variables.
SUPPLIED: tuple = ("body", "caller", "request")


def signature_problems(handler, endpoint: Endpoint) -> list:
    """
    What the binder could not give this handler: a path variable it does not take, or an
    argument with no default that nothing supplies. Each would be a 500 at the first call.
    """
    try:
        accepted = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return []
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in accepted.values()):
        return []
    problems = [f"{endpoint.handler} does not take the path variable {name}"
                for name in endpoint.variables if name not in accepted]
    known = set(endpoint.variables) | set(SUPPLIED) | set(endpoint.query)
    for name, item in accepted.items():
        if item.kind == inspect.Parameter.VAR_POSITIONAL or name in known:
            continue
        if item.default is inspect.Parameter.empty:
            problems.append(f"{endpoint.handler} needs {name}, which nothing supplies")
    return problems


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
    """
    caller = kwargs.pop("caller", None)
    body = kwargs.pop("body", None)
    supplied = dict(kwargs)
    if body is not None:
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


def _endpoint_function(handler, endpoint: Endpoint, body_model):
    """
    Build a path operation whose signature FastAPI can introspect.
    """
    if inspect.iscoroutinefunction(handler):
        async def operation(**kwargs):
            """
            The path operation FastAPI awaits, for a handler that is itself async.
            """
            return await _call(handler, _arguments(kwargs))
    else:
        def operation(**kwargs):
            """
            The path operation FastAPI calls, which hands the backend its arguments.
            """
            return _call(handler, _arguments(kwargs))

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


def build(manifest: Manifest, backend, models: dict, renderer=None) -> APIRouter:
    """
    One router for the whole plugin. Refuses rather than serving half of it.
    """
    router = APIRouter(prefix=f"{API_PREFIX}/{manifest.plugin}", tags=[manifest.plugin])

    for endpoint in manifest.endpoints:
        if endpoint.render is not None:
            # A render that only writes files has no path of its own to serve.
            if not endpoint.render.serve:
                continue
            if renderer is None:
                raise PluginRefused(f"{endpoint.path} declares a render with no engine")
            handler = renderer.reader(manifest.plugin, endpoint.path)
        else:
            handler = getattr(backend, endpoint.handler, None)
            if handler is None or not callable(handler):
                raise PluginRefused(
                    f'{endpoint.path or "/"} names handler {endpoint.handler}, '
                    f"which {manifest.backend} does not have")
            problems = signature_problems(handler, endpoint)
            if problems:
                raise PluginRefused(f'{endpoint.path or "/"}: {problems[0]}')

        body_model = models.get(endpoint.body_schema) if endpoint.body_schema else None
        for verb in endpoint.methods:
            verb = verb.upper()
            wants_body = body_model if verb in BODY_VERBS else None
            router.add_api_route(
                "/" + endpoint.route_path if endpoint.route_path else "",
                _endpoint_function(handler, endpoint, wants_body),
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
