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
A plugin checked without a running daemon: everything the loader would refuse, said at
once, so an author or a pipeline finds out before a controller does.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os

from trixd.common.constants import RESERVED_PLUGINS
from trixd.common.errors import PluginRefused
from trixd.core.plugins import binder, manifest as manifests


def check(path: str) -> list:
    """
    The problems with the plugin in this directory; an empty list means it would load.
    """
    from trixd.core.plugins import loader

    name = os.path.basename(os.path.normpath(path))
    problems = []
    if name in RESERVED_PLUGINS:
        problems.append(f"{name} is reserved for the daemon's own routes")
    manifest_path = os.path.join(path, "manifest.yaml")
    if not os.path.isfile(manifest_path):
        return problems + [f"no manifest.yaml in {path}"]
    try:
        manifest = manifests.parse(manifests.read_yaml(manifest_path))
    except PluginRefused as error:
        return problems + [error.message]
    if manifest.plugin != name:
        problems.append(f"manifest calls itself {manifest.plugin} but sits at {name}")
    try:
        manifests.build_models(manifest)
    except PluginRefused as error:
        problems.append(error.message)
    # A module left from an earlier import of the same name would answer for this one.
    loader._purge(name)
    try:
        _, backend = loader._from_directory(name, path)
    except PluginRefused as error:
        return problems + [error.message]
    except Exception as error:
        return problems + [f"the backend does not import: {error}"]

    # Through the shared collector, which the load path refuses on. Collected here rather
    # than raised, so one run still names every problem it can find.
    problems += manifests.duplicates(manifest)
    for endpoint in manifest.endpoints:
        if endpoint.render is not None:
            # The loader's own check rather than a second one beside it: this branch looked
            # for the producer only, so a coalesce the load path refuses passed here and
            # the empty list promised above was wrong.
            try:
                loader._render_producer(endpoint, backend)
            except PluginRefused as error:
                problems.append(error.message)
            continue
        # The binder's own check rather than a second one beside it: this was a second
        # copy of its two refusals, and the wording had already drifted apart from it.
        problems += binder.endpoint_problems(manifest, backend, endpoint)
    for event, handler in manifest.hooks.items():
        # The loader's own check rather than a second one beside it: this loop looked for
        # the handler only, so a signature the load path refuses passed here. That is the
        # render branch's defect again, and the way it comes back is a refusal added to the
        # loader alone - so nothing here knows what a hook refusal is any more.
        try:
            loader._hook_handler(manifest, backend, event, handler)
        except PluginRefused as error:
            problems.append(error.message)
    return problems
