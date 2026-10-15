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
Write docs/openapi.json and docs/manifest.schema.json from the code: the API with every
shipped plugin loaded, and the manifest format. A unit test fails when the committed
files and the code disagree; run this to bring them back in step:

    python tools/build_schemas.py
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def openapi_document() -> dict:
    """
    The published API of a daemon with all shipped plugins, built in a scratch directory.
    """
    from trixd.app import create_app
    from trixd.common import settings as configuration
    from trixd.common.constants import DAEMON_RELEASE
    from trixd.core.lifespan import start, stop
    from trixd.plugins.luna.config import LUNA_INI_ENV

    with tempfile.TemporaryDirectory() as scratch:
        luna_ini = os.path.join(scratch, "luna.ini")
        with open(luna_ini, "w", encoding="utf-8") as handle:
            handle.write("[API]" + chr(10) + "ENDPOINT = 127.0.0.1:9" + chr(10)
                         + "PROTOCOL = http" + chr(10) + "SECRET_KEY = " + "0" * 32 + chr(10))
        os.environ[LUNA_INI_ENV] = luna_ini
        ini = os.path.join(scratch, "trixd.ini")
        lines = ["[LOGGER]", "LEVEL = error", "LOGFILE =", "[API]", "BIND =",
                 "INSECURE_BIND = 127.0.0.1:0", "USERNAME = schema", "PASSWORD = schema",
                 "[TOKENS]", "SECRET_KEY = " + "0" * 32,
                 "[DATABASE]", "URL = sqlite:///" + os.path.join(scratch, "trixd.db").replace(os.sep, "/"),
                 "[TRIGGERS]", "LUNA_HASHES = 0s",
                 "[PLUGINS]", "DIRECTORY = " + os.path.join(ROOT, "trixd", "plugins"),
                 "SITE_DIRECTORY = " + os.path.join(scratch, "site"), "ENABLED ="]
        with open(ini, "w", encoding="utf-8") as handle:
            handle.write(chr(10).join(lines) + chr(10))
        configuration.set_current(configuration.load(ini))
        app = create_app(with_lifespan=False)
        start(app)
        try:
            document = app.openapi()
        finally:
            stop(app)
            from trixd.db import engine as database
            database.engine().dispose()
    # The release line, not the build: the document describes the API, not one wheel.
    document["info"]["version"] = DAEMON_RELEASE
    # A served copy names a service on the reader's host; the published one cannot know
    # it, so each plugin's static stand-in goes in instead.
    from trixd.api.v1 import docs
    document["info"]["description"] = docs.substitute(document["info"]["description"])
    return document


def build(out_dir: str) -> list:
    """
    Write both files into out_dir and answer their paths.
    """
    from trixd.core.plugins.schema import manifest_schema

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for name, document in (("openapi.json", openapi_document()),
                           ("manifest.schema.json", manifest_schema())):
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8", newline=chr(10)) as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True) + chr(10))
        written.append(path)
    return written


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs")
    for path in build(target):
        print("wrote", path)
