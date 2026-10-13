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
Writing out a plugin that loads, so an author starts from something that works.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import re

from trixd.common.constants import RESERVED_PLUGINS
from trixd.common.errors import BadRequest, Conflict

NAME_RULE = re.compile(r"^[a-z][a-z0-9-]*$")

MANIFEST: str = '''plugin: {name}
backend: backend
description: >
  What this plugin is for. One or two lines; it reaches the published document.
schemas:
  setting:
    value: {{type: string}}
endpoints:
  - path: ""
    methods: [GET]
    minimum: reader
    handler: show
    summary: What this plugin currently holds
  - path: setting
    methods: [GET, PATCH]
    minimum: manager
    handler: setting
    schema: setting
    summary: Read or change the one setting this example keeps
'''

BACKEND: str = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
The {name} plugin. One method per handler the manifest names, and nothing else.
"""

from trixd.common import logging as trixlog
from trixd.utils import declarations

LOGGER = trixlog.get("plugin.{name}")

PLUGIN: str = "{name}"

DEFAULT: dict = {{"value": ""}}


class Backend:
    """
    The {name} backend. See the manifest beside this file.
    """

    def show(self, caller=None) -> dict:
        """
        What this plugin holds, which is the setting and who asked.
        """
        return {{"plugin": PLUGIN, "setting": declarations.read(PLUGIN, "setting", "",
                                                               dict(DEFAULT))}}

    def setting(self, body: dict = None, caller=None) -> dict:
        """
        Read the setting, or write it when a body is given.
        """
        if body is None:
            return declarations.read(PLUGIN, "setting", "", dict(DEFAULT))
        declarations.write(PLUGIN, "setting", "", body)
        who = getattr(caller, "name", "unknown")
        LOGGER.info("%s changed the {name} setting", who)
        return body
'''

README: str = '''{name}
{underline}

A plugin for trixd. The manifest declares the endpoints; the backend answers
them. Drop this directory into the plugins directory the ini names and reload:

    curl -X POST http://<controller>:8051/api/v1/system/plugins/{name}/reload \\
         -H "x-access-tokens: $TOKEN"

The daemon does not restart. If the manifest is wrong the call answers 409 and
says why, and whatever was already serving keeps serving.

See docs/writing-a-plugin.md in the trix-daemon repository for the contract.
'''


def new(name: str, directory: str, shipped: str = "") -> list:
    """
    Write a plugin that loads as it stands, for an author to edit into shape.
    """
    if not NAME_RULE.match(str(name or "")):
        raise BadRequest(f"{name} is not a plugin name: lower case, digits and dashes")
    if name in RESERVED_PLUGINS:
        raise BadRequest(f"{name} is reserved for the daemon's own routes")
    if shipped and os.path.isdir(os.path.join(shipped, name)):
        raise Conflict(f"{name} is a shipped plugin; choose another name")

    target = os.path.join(directory, name)
    if os.path.exists(target):
        raise Conflict(f"{target} already exists")
    os.makedirs(target)

    written = []
    for filename, content in (
            ("manifest.yaml", MANIFEST.format(name=name)),
            ("backend.py", BACKEND.format(name=name)),
            ("__init__.py", ""),
            ("README", README.format(name=name, underline="=" * len(name)))):
        path = os.path.join(target, filename)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        written.append(path)
    return written
