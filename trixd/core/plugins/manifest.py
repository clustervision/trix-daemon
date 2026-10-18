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
What a plugin declares, and the request models built from it.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import hashlib
import json
import re
from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model

from trixd.common.constants import (HOOK_EVENT_PARTS, HTTP_VERBS, MINIMUMS,
                                   NODE_PATH_PARAMS, TOKEN_KIND_NODE, TOKEN_KIND_USER)
from trixd.common.errors import PluginRefused

# <name> is one path segment; <path:name> is the rest of the path, slashes and all.
PATH_VARIABLE = re.compile(r"<(?:(path):)?([a-zA-Z_][a-zA-Z0-9_]*)>")

# What a declared query parameter may be; the document shows it and FastAPI checks it.
QUERY_TYPES: dict = {"string": str, "integer": int, "boolean": bool}
# Names the binder hands a handler itself, so a query parameter cannot take them.
RESERVED_ARGUMENTS: tuple = ("body", "caller", "request")
# The verbs that carry a body. The binder reads this to decide what to ask FastAPI for,
# and the parse refuses a schema on anything else, so the two cannot drift apart.
BODY_VERBS: tuple = ("POST", "PATCH", "PUT")

TYPES: dict = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "date": date,
    "object": dict,
    "array": list,
}


class Render(BaseModel):
    """
    A derived output: what it reads, how long a burst is gathered, and whether it is served.
    """

    model_config = ConfigDict(extra="forbid")

    inputs: list = Field(default_factory=list)
    coalesce: str = "2s"
    serve: str = ""


class Doc(BaseModel):
    """
    A path variable or query parameter, as the published document explains it.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    example: Any = None
    type: str = "string"


class Answer(BaseModel):
    """
    What a successful call returns, as the published document shows it.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    example: Any = None
    media_type: str = "application/json"


class Endpoint(BaseModel):
    """
    One path a plugin asks the daemon to serve.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str
    methods: list = Field(default_factory=lambda: ["GET"])
    # The lowest role on the ladder this endpoint admits. A path that is read at
    # one level and changed at another is two endpoints.
    minimum: str = ""
    handler: str = ""
    body_schema: str = Field(default="", alias="schema")
    token: str = TOKEN_KIND_USER
    render: Optional[Render] = None
    summary: str = ""
    # No token at all. Only for a consumer that cannot present one, which today
    # means Prometheus service discovery. Every use has to be argued for.
    public: bool = False
    # The daemon checks nothing: the request goes on as sent and the service behind it
    # decides, as Luna does behind /api/v1/luna. Only the luna plugin needs it.
    forward: bool = False
    # This call has to happen on the other controller too, because what it writes is not
    # in a replicated table and no render brings it back. Off by default, including for a
    # plugin's own bulk route: a call that need not cross should not.
    replicate: bool = False
    # What /api/v1/docs shows beyond the summary: what the route does, what to send, what
    # comes back, and the refusals its own work can answer.
    description: str = ""
    params: dict[str, Doc] = Field(default_factory=dict)
    query: dict[str, Doc] = Field(default_factory=dict)
    request_example: Any = None
    response: Optional[Answer] = None
    errors: dict[int, str] = Field(default_factory=dict)

    @property
    def variables(self) -> list:
        """
        Every path variable this endpoint declares.
        """
        return [name for _, name in PATH_VARIABLE.findall(self.path)]

    @property
    def route_path(self) -> str:
        """
        The manifest writes <name>; Starlette wants {name}.
        """
        return PATH_VARIABLE.sub(lambda found: "{" + found.group(2) + (
            ":path}" if found.group(1) else "}"), self.path).strip("/")

    @property
    def shape(self) -> str:
        """
        The path as Starlette matches it, which a variable's name is no part of.
        """
        return PATH_VARIABLE.sub(
            lambda found: "{:path}" if found.group(1) else "{}", self.path).strip("/")


class Manifest(BaseModel):
    """
    Everything a plugin declares about itself.
    """

    model_config = ConfigDict(extra="forbid")

    plugin: str
    backend: str = "backend"
    description: str = ""
    schemas: dict = Field(default_factory=dict)
    endpoints: list = Field(default_factory=list)
    # Which changes elsewhere this plugin acts on, and the backend method for each.
    hooks: dict = Field(default_factory=dict)

    @property
    def digest(self) -> str:
        """
        A stable hash of the declaration, so a reload can say what changed.
        """
        return hashlib.sha256(
            json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def parse(document: dict) -> Manifest:
    """
    Turn a manifest document into a Manifest, refusing anything unusable.
    """
    try:
        manifest = Manifest(**document)
        manifest.endpoints = [item if isinstance(item, Endpoint) else Endpoint(**item)
                              for item in manifest.endpoints]
    except Exception as error:
        raise PluginRefused(f"manifest does not parse: {error}") from None

    if not manifest.plugin:
        raise PluginRefused("manifest names no plugin")
    for endpoint in manifest.endpoints:
        bad = [verb for verb in endpoint.methods if verb.upper() not in HTTP_VERBS]
        if bad:
            raise PluginRefused(f'{endpoint.path or "/"} declares unsupported verbs: {", ".join(bad)}')
        if endpoint.token not in (TOKEN_KIND_USER, TOKEN_KIND_NODE):
            raise PluginRefused(f'{endpoint.path or "/"} declares an unknown token kind')
        if endpoint.render is None and not endpoint.handler:
            raise PluginRefused(f'{endpoint.path or "/"} names no handler')
        if (not endpoint.public and not endpoint.forward and endpoint.token == TOKEN_KIND_USER
                and endpoint.minimum not in MINIMUMS):
            raise PluginRefused(
                f'{endpoint.path or "/"} must declare a minimum role from '
                f'{", ".join(MINIMUMS)}, not {endpoint.minimum or "nothing"}')
        if endpoint.replicate:
            # A read has nothing to carry; a render is already driven at both ends by the
            # same source; a forwarded call is replicated by the service behind it; and a
            # public one would let anyone make this controller call its peer.
            reads = [verb.upper() for verb in endpoint.methods
                     if verb.upper() in ("GET", "HEAD")]
            if len(reads) == len(endpoint.methods):
                raise PluginRefused(f"{endpoint.path or '/'} reads, so there is nothing "
                                    f"to replicate")
            for why, blocked in (("a render", endpoint.render is not None),
                                 ("forwarded", endpoint.forward),
                                 ("public", endpoint.public)):
                if blocked:
                    raise PluginRefused(f"{endpoint.path or '/'} is {why} and cannot "
                                        f"also replicate")
            if reads:
                # Replicate is applied per verb, so a read sharing the endpoint replicates
                # as well: it writes an outbox row and asks the peer on every call, and
                # answers with a block no read is documented to carry.
                raise PluginRefused(f"{endpoint.path or '/'} replicates, so it cannot also "
                                    f"serve {', '.join(reads)}: a read that replicated "
                                    f"would write an outbox row for every call")
        if endpoint.render is not None and (endpoint.variables or endpoint.query):
            # A render is served by a reader that takes no arguments, so a variable or a
            # query parameter is accepted and then ignored: one document answers for every
            # value of it, the producer is never told which was asked for, and the author's
            # only signal is wrong data at a customer.
            unusable = "a path variable" if endpoint.variables else "a query parameter"
            raise PluginRefused(f"{endpoint.path or '/'} is a render, so it cannot take "
                                f"{unusable}: the reader is given none")
        if endpoint.token == TOKEN_KIND_NODE and not any(
                key in endpoint.variables for key in NODE_PATH_PARAMS):
            # The gate binds a node token to the node its path names, and looks for it under
            # one of these names only. With none of them there the binding is skipped in
            # silence and one node's token is accepted for another node's data.
            raise PluginRefused(
                f"{endpoint.path or '/'} takes a node token, so its path has to carry one "
                f"of {', '.join(NODE_PATH_PARAMS)} for the gate to bind it to")
        if endpoint.body_schema:
            if endpoint.body_schema not in manifest.schemas:
                raise PluginRefused(
                    f"{endpoint.path} wants schema {endpoint.body_schema}, which is not declared")
            # GET is deliberate: a GET and a write on one handler tell a read from a change
            # by the body being None. Any other bodiless verb is accepted here and dropped
            # at bind, so the document agrees with the route and not with the manifest.
            bodiless = [verb.upper() for verb in endpoint.methods
                        if verb.upper() not in BODY_VERBS and verb.upper() != "GET"]
            if bodiless:
                raise PluginRefused(
                    f"{endpoint.path or '/'} wants schema {endpoint.body_schema} on "
                    f"{', '.join(bodiless)}, which takes no body")
        _check_docs(endpoint)
    for event, handler in manifest.hooks.items():
        parts = str(event).split(".")
        if len(parts) != HOOK_EVENT_PARTS or not all(parts):
            raise PluginRefused(f"{event} is not a hook event: name one as "
                                f"<source>.<object>.<event>, such as luna.node.update")
        if not handler or not isinstance(handler, str):
            raise PluginRefused(f"{event} names no handler")
    return manifest


def duplicates(manifest: Manifest) -> list:
    """
    Every path and verb this manifest declares more than once.

    Starlette matches the first of two routes with the same path and verb, so the second
    handler is dead code its author believes is serving. A variable's name is no part of
    that match, so rules/<alpha> and rules/<beta> are one route and not two. Collected
    rather than raised: the command-line check lists every problem it can find at once so
    an author fixes them in one pass, and a refusal from the parse ends that collection at
    the first one. The load path refuses on whatever comes back here.
    """
    seen, twice = {}, []
    for endpoint in manifest.endpoints:
        where = endpoint.path or "/"
        for verb in endpoint.methods:
            key = (endpoint.shape, verb.upper())
            first = seen.get(key)
            if first == where:
                twice.append(f"{verb.upper()} {where} is declared twice")
            elif first is not None:
                twice.append(f"{verb.upper()} {where} and {first} differ only in the name of "
                             f"a path variable, so only {first} is ever matched")
            seen.setdefault(key, where)
    return twice


def _check_docs(endpoint: Endpoint) -> None:
    """
    Refuse documentation that the document could not show truthfully.
    """
    where = endpoint.path or "/"
    unknown = [name for name in endpoint.params if name not in endpoint.variables]
    if unknown:
        raise PluginRefused(f"{where} documents {', '.join(unknown)}, which its path does not have")
    # A query parameter by one of these names is already refused below. A path variable by
    # one of them was not, and for a public endpoint nothing else catches it: the handler is
    # handed the path's value where it expects the caller, and answers 500 on its first call.
    taken = [name for name in endpoint.variables if name in RESERVED_ARGUMENTS]
    if taken:
        raise PluginRefused(f"{where} names path variable {', '.join(taken)}, which is how a "
                            f"handler is given its body, its caller or the request")
    for name, item in endpoint.query.items():
        if (not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) or name in RESERVED_ARGUMENTS
                or name in endpoint.variables):
            raise PluginRefused(f"{where} declares query parameter {name}, which it cannot take")
        if item.type not in QUERY_TYPES:
            raise PluginRefused(f"{where} query parameter {name} is {item.type}; "
                                f"one of {', '.join(QUERY_TYPES)}")
    bad = [str(status) for status in endpoint.errors if not 400 <= status < 600]
    if bad:
        raise PluginRefused(f"{where} documents {', '.join(bad)} as errors; 400 to 599 only")


def build_models(manifest: Manifest) -> dict:
    """
    Make one request model per declared schema.
    """
    models = {}
    for name, fields in manifest.schemas.items():
        spec = {}
        for field, rule in (fields or {}).items():
            rule = rule or {}
            kind = TYPES.get(str(rule.get("type", "string")), str)
            if kind is list:
                item = TYPES.get(str(rule.get("items", "string")), str)
                kind = list[item]
            constraints = {}
            if rule.get("pattern"):
                constraints["pattern"] = rule["pattern"]
            if rule.get("description"):
                constraints["description"] = str(rule["description"])
            if "example" in rule:
                constraints["examples"] = [rule["example"]]
            if rule.get("optional"):
                spec[field] = (Optional[kind], Field(default=None, **constraints))
            else:
                spec[field] = (kind, Field(**constraints))
        try:
            models[name] = create_model(f"{manifest.plugin.title()}{name.title()}", **spec)
        except Exception as error:
            raise PluginRefused(f"schema {name} does not build: {error}") from None
    return models


def read_yaml(path: str) -> dict:
    """
    Read a manifest.yaml from disk.
    """
    import yaml
    with open(path, "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise PluginRefused(f"{path} is not a manifest document")
    return document

