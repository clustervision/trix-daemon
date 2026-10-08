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
The plugin manifest as a JSON Schema, built from the same constants the loader checks
against, so an editor, a reviewer and the daemon agree on what a manifest may say.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from trixd.common.constants import HTTP_VERBS, LUNA_HOOKS, MINIMUMS, TOKEN_KIND_NODE, TOKEN_KIND_USER

SCHEMA_ID: str = "https://gitlab.taurusgroup.one/clustervision/trix-daemon/docs/manifest.schema.json"


def manifest_schema() -> dict:
    """
    What a manifest.yaml may hold.
    """
    render = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "inputs": {"type": "array", "items": {"type": "string"},
                       "description": "luna.<table> or a declaration of this plugin"},
            "coalesce": {"type": "string", "pattern": "^[0-9]+[smhd]?$",
                         "description": "how long a burst of changes is gathered, e.g. 2s"},
            "serve": {"type": "string", "enum": ["", "json"],
                      "description": "served from the last result; empty writes files only"},
        },
    }
    endpoint = {
        "type": "object",
        "additionalProperties": False,
        "required": ["path"],
        "properties": {
            "path": {"type": "string",
                     "description": "under /api/v1/<plugin>; <name> is a segment, "
                                    "<path:name> the rest of the path"},
            "methods": {"type": "array", "items": {"enum": list(HTTP_VERBS)},
                        "default": ["GET"]},
            "minimum": {"enum": list(MINIMUMS),
                        "description": "the lowest role this endpoint admits"},
            "handler": {"type": "string", "description": "the backend method"},
            "schema": {"type": "string", "description": "a name under schemas, for the body"},
            "token": {"enum": [TOKEN_KIND_USER, TOKEN_KIND_NODE], "default": TOKEN_KIND_USER},
            "render": render,
            "summary": {"type": "string"},
            "public": {"type": "boolean", "default": False,
                       "description": "no token at all; argue for every use"},
            "forward": {"type": "boolean", "default": False,
                        "description": "the daemon checks nothing; the service behind decides"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "trixd plugin manifest",
        "$comment": "This code is part of the TrinityX software suite. Copyright (C) 2026 "
                    "ClusterVision Solutions b.v. GPL-3.0-or-later.",
        "type": "object",
        "additionalProperties": False,
        "required": ["plugin", "endpoints"],
        "properties": {
            "plugin": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$"},
            "backend": {"type": "string", "default": "backend"},
            "description": {"type": "string"},
            "schemas": {"type": "object",
                        "additionalProperties": {"type": "object"},
                        "description": "request bodies by name: field -> {type, optional, items}"},
            "endpoints": {"type": "array", "items": endpoint},
            "luna_hooks": {"type": "object",
                           "propertyNames": {"enum": list(LUNA_HOOKS)},
                           "additionalProperties": {"type": "string"}},
        },
    }
