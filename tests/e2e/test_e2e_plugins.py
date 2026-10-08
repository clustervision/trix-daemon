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
Plugging and unplugging with the daemon running: every shipped plugin, again and again,
at the same time as other reloads, and a site plugin dropped in live.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import threading

import pytest

from tests.e2e.stack import operations

SHIPPED = ("luna", "directory", "prometheus", "services", "slurm", "genders")
CYCLES = 3


def _probe(stack, document, plugin: str) -> str:
    """
    A GET route of the plugin with no path variable, to tell served from gone.
    """
    for verb, path, _ in operations(document):
        if verb == "GET" and path.startswith(f"/{plugin}/") and "{" not in path:
            return path
        if plugin == "luna" and path.startswith("/luna/"):
            return "/luna/config/node"
    return ""


@pytest.mark.parametrize("plugin", SHIPPED)
@pytest.mark.parametrize("cycle", range(CYCLES))
def test_a_plugin_unplugs_and_plugs_back_while_the_daemon_runs(stack, document, plugin, cycle):
    """
    Disabled: its routes answer 404. Reloaded: they are back. The daemon never restarts.
    """
    probe = _probe(stack, document, plugin)
    assert probe, f"{plugin} publishes no GET route"
    before = stack.call("GET", "/system/health").json()
    try:
        assert stack.call("POST", f"/system/plugins/{plugin}/disable",
                          stack.rootus()).status_code == 200
        assert stack.call("GET", probe, stack.rootus()).status_code == 404
    finally:
        assert stack.call("POST", f"/system/plugins/{plugin}/reload",
                          stack.rootus()).status_code == 200
    assert stack.call("GET", probe, stack.rootus()).status_code not in (404, 500)
    after = stack.call("GET", "/system/health").json()
    assert after["plugins"] == before["plugins"]


def test_reloads_at_the_same_moment_leave_every_plugin_serving(stack, document):
    """
    Twenty reloads of different plugins at once: each is serialised, none is lost.
    """
    answers = []

    def reload(name: str) -> None:
        """
        One reload, its status kept.
        """
        answers.append(stack.call("POST", f"/system/plugins/{name}/reload",
                                  stack.rootus()).status_code)

    threads = [threading.Thread(target=reload, args=(SHIPPED[n % len(SHIPPED)],))
               for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert answers == [200] * 20
    listed = stack.call("GET", "/system/plugins", stack.rootus()).json()
    states = {item["name"]: item["state"] for item in listed["plugins"]}
    assert all(states.get(name) == "loaded" for name in SHIPPED), states


@pytest.mark.parametrize("name", ["auth", "identity", "system", "docs"])
def test_a_reserved_name_is_refused_with_409(stack, name):
    """
    The daemon's own prefixes cannot be plugged.
    """
    answer = stack.call("POST", f"/system/plugins/{name}/reload", stack.rootus())
    assert answer.status_code == 409 and "reserved" in answer.text


def test_an_unknown_plugin_is_a_404(stack):
    """
    Nothing on disk by that name.
    """
    assert stack.call("POST", "/system/plugins/e2e-nosuch/reload",
                      stack.rootus()).status_code == 404


def test_a_site_plugin_dropped_in_is_served_without_a_restart(stack):
    """
    Written into SITE_DIRECTORY, reloaded by name, served, then unplugged. Local only: it
    writes on the daemon's own disk.
    """
    if not stack.local:
        pytest.skip("writes into the daemon's site directory")
    root = os.path.join(stack.scratch, "site", "e2e-hello")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "__init__.py"), "w", encoding="utf-8") as handle:
        handle.write("")
    with open(os.path.join(root, "backend.py"), "w", encoding="utf-8") as handle:
        handle.write("class Backend:\n    def greet(self, who):\n        return {'hello': who}\n")
    with open(os.path.join(root, "manifest.yaml"), "w", encoding="utf-8") as handle:
        handle.write("plugin: e2e-hello\nbackend: backend\nendpoints:\n"
                     "  - path: greet/<who>\n    methods: [GET]\n    minimum: reader\n"
                     "    handler: greet\n")
    assert stack.call("POST", "/system/plugins/e2e-hello/reload",
                      stack.rootus()).status_code == 200
    answer = stack.call("GET", "/e2e-hello/greet/world", stack.as_role("reader"))
    assert answer.status_code == 200 and answer.json() == {"hello": "world"}
    assert stack.call("GET", "/e2e-hello/greet/world", stack.as_role("none")).status_code == 403
    assert stack.call("POST", "/system/plugins/e2e-hello/disable",
                      stack.rootus()).status_code == 200
    assert stack.call("GET", "/e2e-hello/greet/world", stack.rootus()).status_code == 404
