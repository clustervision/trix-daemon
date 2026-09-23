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
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, create_model

from trix.common.constants import HTTP_VERBS, TOKEN_KIND_NODE, TOKEN_KIND_USER
from trix.common.errors import PluginRefused

PATH_VARIABLE = re.compile(r'<([a-zA-Z_][a-zA-Z0-9_]*)>')

TYPES: dict = {
    'string': str,
    'integer': int,
    'number': float,
    'boolean': bool,
    'date': date,
    'object': dict,
    'array': list,
}


class Render(BaseModel):
    """A derived output: what it reads, what wakes it, and where it goes."""

    model_config = ConfigDict(extra='forbid')

    inputs: list = Field(default_factory=list)
    triggers: list = Field(default_factory=list)
    coalesce: str = '2s'
    serve: str = ''
    files: str = ''
    blocks: str = ''
    after: list = Field(default_factory=list)


class Endpoint(BaseModel):
    """One path a plugin asks the daemon to serve."""

    model_config = ConfigDict(extra='forbid', populate_by_name=True)

    path: str
    methods: list = Field(default_factory=lambda: ['GET'])
    capability: str = ''
    handler: str = ''
    body_schema: str = Field(default='', alias='schema')
    token: str = TOKEN_KIND_USER
    render: Optional[Render] = None
    summary: str = ''
    # No token at all. Only for a consumer that cannot present one, which today
    # means Prometheus service discovery. Every use has to be argued for.
    public: bool = False

    @property
    def variables(self) -> list:
        return PATH_VARIABLE.findall(self.path)

    @property
    def route_path(self) -> str:
        """
        The manifest writes <name>; Starlette wants {name}.
        """
        return PATH_VARIABLE.sub(r'{\1}', self.path).strip('/')


class Manifest(BaseModel):
    """Everything a plugin declares about itself."""

    model_config = ConfigDict(extra='forbid')

    plugin: str
    backend: str = 'backend'
    description: str = ''
    schemas: dict = Field(default_factory=dict)
    endpoints: list = Field(default_factory=list)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.model_dump(mode='json'), sort_keys=True).encode()).hexdigest()


def parse(document: dict) -> Manifest:
    """
    Turn a manifest document into a Manifest, refusing anything unusable.
    """
    try:
        manifest = Manifest(**document)
        manifest.endpoints = [item if isinstance(item, Endpoint) else Endpoint(**item)
                              for item in manifest.endpoints]
    except Exception as error:
        raise PluginRefused(f'manifest does not parse: {error}') from None

    if not manifest.plugin:
        raise PluginRefused('manifest names no plugin')
    for endpoint in manifest.endpoints:
        bad = [verb for verb in endpoint.methods if verb.upper() not in HTTP_VERBS]
        if bad:
            raise PluginRefused(f'{endpoint.path or "/"} declares unsupported verbs: {", ".join(bad)}')
        if endpoint.token not in (TOKEN_KIND_USER, TOKEN_KIND_NODE):
            raise PluginRefused(f'{endpoint.path or "/"} declares an unknown token kind')
        if endpoint.render is None:
            if not endpoint.capability:
                raise PluginRefused(f'{endpoint.path or "/"} declares no capability')
            if not endpoint.handler:
                raise PluginRefused(f'{endpoint.path or "/"} names no handler')
        if endpoint.body_schema and endpoint.body_schema not in manifest.schemas:
            raise PluginRefused(
                f'{endpoint.path} wants schema {endpoint.body_schema}, which is not declared')
    return manifest


def build_models(manifest: Manifest) -> dict:
    """
    Make one request model per declared schema.
    """
    models = {}
    for name, fields in manifest.schemas.items():
        spec = {}
        for field, rule in (fields or {}).items():
            rule = rule or {}
            kind = TYPES.get(str(rule.get('type', 'string')), str)
            if kind is list:
                item = TYPES.get(str(rule.get('items', 'string')), str)
                kind = list[item]
            constraints = {}
            if rule.get('pattern'):
                constraints['pattern'] = rule['pattern']
            if rule.get('optional'):
                spec[field] = (Optional[kind], Field(default=None, **constraints))
            else:
                spec[field] = (kind, Field(**constraints))
        try:
            models[name] = create_model(f'{manifest.plugin.title()}{name.title()}', **spec)
        except Exception as error:
            raise PluginRefused(f'schema {name} does not build: {error}') from None
    return models


def read_yaml(path: str) -> dict:
    """
    Read a manifest.yaml from disk.
    """
    import yaml
    with open(path, 'r', encoding='utf-8') as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise PluginRefused(f'{path} is not a manifest document')
    return document

