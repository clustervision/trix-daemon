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
What is loaded right now, in what state, and which routes belong to which plugin.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from trixd.common.constants import (PLUGIN_STATE_DISABLED, PLUGIN_STATE_LOADED,
                                   PLUGIN_STATE_REFUSED)


class LoadedPlugin:
    """
    One plugin the daemon knows about, loaded or not.
    """

    def __init__(self, name: str, state: str = PLUGIN_STATE_LOADED) -> None:
        """
        One plugin and the state it is in.
        """
        self.name = name
        self.state = state
        self.reason: str = ""
        self.digest: str = ""
        self.origin: str = ""
        self.manifest = None
        self.backend = None
        self.models: dict = {}
        self.routes: list = []
        self.hooks: dict = {}

    def summary(self) -> dict:
        """
        What the control endpoint reports.
        """
        return {
            "name": self.name,
            "state": self.state,
            "reason": self.reason,
            "digest": self.digest[:16],
            "origin": self.origin,
            "routes": len(self.routes),
        }


class Registry:
    """
    The plugins this process is serving.
    """

    def __init__(self) -> None:
        """
        No plugins yet.
        """
        self.plugins: dict = {}

    def put(self, entry: LoadedPlugin) -> LoadedPlugin:
        """
        Record a plugin as loaded.
        """
        self.plugins[entry.name] = entry
        return entry

    def hooks_for(self, event: str) -> list:
        """
        Every loaded plugin's handler for one change, as plugin and callable.
        """
        return [(entry.name, entry.hooks[event]) for entry in self.plugins.values()
                if entry.state == PLUGIN_STATE_LOADED and event in (entry.hooks or {})]

    def get(self, name: str) -> LoadedPlugin:
        """
        One plugin by name, or None.
        """
        return self.plugins.get(name)

    def refuse(self, name: str, reason: str, origin: str = "") -> LoadedPlugin:
        """
        Record a refusal without disturbing whatever is already serving.
        """
        existing = self.plugins.get(name)
        if existing is not None and existing.state == PLUGIN_STATE_LOADED:
            existing.reason = reason
            return existing
        entry = LoadedPlugin(name, PLUGIN_STATE_REFUSED)
        entry.reason = reason
        entry.origin = origin
        return self.put(entry)

    def disable(self, name: str) -> LoadedPlugin:
        """
        Mark a plugin unplugged and keep the entry.
        """
        entry = self.plugins.get(name) or LoadedPlugin(name)
        entry.state = PLUGIN_STATE_DISABLED
        entry.routes = []
        entry.backend = None
        return self.put(entry)

    def owned_routes(self, name: str) -> set:
        """
        The identity of every route this plugin contributed.
        """
        entry = self.plugins.get(name)
        return {id(route) for route in entry.routes} if entry else set()

    def owner(self, route) -> str:
        """
        Which plugin a route belongs to, or an empty string.
        """
        for name, entry in self.plugins.items():
            if any(route is item for item in entry.routes):
                return name
        return ""

    def paths(self, skip: str = "") -> dict:
        """
        Every path a loaded plugin serves, so a collision can be named.
        """
        taken = {}
        for name, entry in self.plugins.items():
            if name == skip or entry.state != PLUGIN_STATE_LOADED:
                continue
            for route in entry.routes:
                taken[getattr(route, "path", "")] = name
        return taken

    def listing(self) -> list:
        """
        Every plugin as the control endpoint shows it.
        """
        return [entry.summary() for entry in
                sorted(self.plugins.values(), key=lambda item: item.name)]


_registry: Registry = Registry()


def current() -> Registry:
    """
    The registry for this process.
    """
    return _registry
