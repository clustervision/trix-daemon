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
Load a custom plugin again when its files change, where [PLUGINS] AUTO_RELOAD asks for it.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import os
import threading

from fastapi import FastAPI

from trixd.common import logging as trixlog
from trixd.common.constants import PLUGIN_STATE_DISABLED, PLUGIN_STATE_LOADED
from trixd.common.errors import PluginRefused
from trixd.common.settings import current as settings
from trixd.core.plugins import loader
from trixd.core.plugins.registry import current as registry
from trixd.utils import threads

LOGGER = trixlog.get("watch")

# Seconds between two looks at the custom plugins. A change is acted on at the look after
# the one that first saw it, so a plugin still being written is not loaded half-saved.
INTERVAL: int = 5


def fingerprint(path: str) -> tuple:
    """
    The source files of one plugin, a directory or a single script, as they are on disk.
    """
    files = [path] if os.path.isfile(path) else [
        os.path.join(root, name) for root, _, names in os.walk(path)
        if "__pycache__" not in root for name in names]
    seen = []
    for item in files:
        if item.endswith((".py", ".yaml")):
            stat = os.stat(item)
            seen.append((os.path.relpath(item, path), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(seen))


class Watch:
    """
    One thread, one look per interval at the custom plugins. Only that directory: the
    shipped one is rewritten by an upgrade under a running daemon.
    """

    def __init__(self) -> None:
        """
        The watch, not yet running.
        """
        self.stop = threading.Event()
        self.thread = None
        self.app = None
        # Per plugin, as path and fingerprint: the last look, and the last one acted on.
        self.seen: dict = {}
        self.acted: dict = {}
        self.failed: str = ""

    def _look(self) -> dict:
        """
        Everything in the custom directory by plugin name, as its path and fingerprint.
        """
        directory = settings().plugins.custom_directory
        found = {}
        for item in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
            path = os.path.join(directory, item)
            try:
                found[item[:-3] if item.endswith(".py") else item] = (path, fingerprint(path))
            except OSError:
                # A file went between the listing and the read: it is being written, so
                # this look says nothing about it and the next one reads it again.
                continue
        return found

    def _due(self, name: str, path: str) -> bool:
        """
        Whether a changed plugin is the watch's to load: one that is serving from this
        path, or one a restart would load. Never one that was unplugged.
        """
        entry = registry().get(name)
        if entry is not None and entry.state == PLUGIN_STATE_LOADED:
            return entry.origin == path
        if entry is not None and entry.state == PLUGIN_STATE_DISABLED:
            return False
        wanted = settings().plugins.enabled
        return ((not wanted or name in wanted) and name not in loader._disabled_names()
                and loader.discover().get(name) == path)

    def once(self) -> list:
        """
        One look: load each plugin whose files changed and have since stood still.
        """
        loaded = []
        before, self.seen = self.seen, self._look()
        for name, found in self.seen.items():
            if not found[1] or found != before.get(name) or found == self.acted.get(name):
                continue
            # Recorded before the load, so a refused plugin is tried once rather than at
            # every look; the loader has said why.
            self.acted[name] = found
            try:
                if self._due(name, found[0]):
                    loader.load(self.app, name, found[0])
                    loaded.append(name)
            except PluginRefused:
                continue
        return loaded

    def baseline(self, app: FastAPI) -> None:
        """
        Take what is on disk now as already acted on: the startup load has just read it.
        """
        self.app = app
        self.seen = self._look()
        self.acted = dict(self.seen)

    def _loop(self) -> None:
        """
        One look per interval until the daemon stops.
        """
        while not self.stop.wait(INTERVAL):
            try:
                self.once()
                self.failed = ""
            except Exception as error:
                # Nothing restarts this thread: a look that fails is said, once while it
                # keeps failing the same way, and the next one still runs.
                if str(error) != self.failed:
                    LOGGER.error("could not look for changed plugins: %s", error)
                self.failed = str(error)

    def start(self, app: FastAPI) -> bool:
        """
        Only where the ini asks for it: a controller in production loads on request.
        """
        if not settings().plugins.auto_reload:
            return False
        self.baseline(app)
        self.stop.clear()
        self.thread = threads.Thread(target=self._loop, name="trixd-watch", daemon=True)
        self.thread.start()
        LOGGER.info("[PLUGINS] AUTO_RELOAD is on: a changed plugin in %s is loaded again "
                    "within %ss", settings().plugins.custom_directory, 2 * INTERVAL)
        return True

    def shutdown(self) -> None:
        """
        Ask the watch to stop; it ends at its next wake-up.
        """
        self.stop.set()


_watch: Watch = Watch()


def current() -> Watch:
    """
    The watch for this process.
    """
    return _watch
