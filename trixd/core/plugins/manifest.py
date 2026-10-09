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

from trixd.common.constants import (HTTP_VERBS, LUNA_HOOKS, MINIMUMS, TOKEN_KIND_NODE,
                                   TOKEN_KIND_USER)
from trixd.common.errors import PluginRefused

# <name> is one path segment; <path:name> is the rest of the path, slashes and all.
PATH_VARIABLE = re.compile(r"<(?:(path):)?([a-zA-Z_][a-zA-Z0-9_]*)>")

# What a declared query parameter may be; the document shows it and FastAPI checks it.
QUERY_TYPES: dict = {"string": str, "integer": int, "boolean": bool}
# Names the binder hands a handler itself, so a query parameter cannot take them.
RESERVED_ARGUMENTS: tuple = ("body", "caller", "request")

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
    # Which changes at Luna this plugin acts on, and the backend method for each.
    luna_hooks: dict = Field(default_factory=dict)

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
        if endpoint.body_schema and endpoint.body_schema not in manifest.schemas:
            raise PluginRefused(
                f"{endpoint.path} wants schema {endpoint.body_schema}, which is not declared")
        _check_docs(endpoint)
    for event, handler in manifest.luna_hooks.items():
        if event not in LUNA_HOOKS:
            raise PluginRefused(f"{event} is not a change at Luna the daemon notices; "
                                f"one of {', '.join(LUNA_HOOKS)}")
        if not handler or not isinstance(handler, str):
            raise PluginRefused(f"{event} names no handler")
    return manifest


def _check_docs(endpoint: Endpoint) -> None:
    """
    Refuse documentation that the document could not show truthfully.
    """
    where = endpoint.path or "/"
    unknown = [name for name in endpoint.params if name not in endpoint.variables]
    if unknown:
        raise PluginRefused(f"{where} documents {', '.join(unknown)}, which its path does not have")
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

