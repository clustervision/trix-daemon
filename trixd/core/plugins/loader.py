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
Discover, validate, refuse, build, swap. Plugging a plugin never restarts the daemon.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import functools
import importlib
import importlib.util
import os
import sys
import threading

from fastapi import FastAPI
from sqlalchemy import select

from trixd.common import logging as trixlog
from trixd.common.constants import (API_PREFIX, PLUGIN_STATE_DISABLED, PLUGIN_STATE_LOADED,
                                    RESERVED_PLUGINS)
from trixd.common.errors import NotFound, PluginRefused
from trixd.common.settings import current as settings, seconds
from trixd.core.plugins import binder, manifest as manifests
from trixd.core.plugins.registry import LoadedPlugin, current as registry
from trixd.core.plugins.swap import exchange
from trixd.core.render.engine import current as renders
from trixd.db import engine as database
from trixd.db import models

LOGGER = trixlog.get("loader")

MODULE_PREFIX: str = "trixd_plugin_"

# A reload from the control endpoint and one from SIGHUP arrive on different threads.
# One at a time keeps the route table and the registry telling the same story.
_LOCK = threading.RLock()


def _serialised(function):
    """
    Run the wrapped load, unload or full load with no other one in progress.
    """
    @functools.wraps(function)
    def run(*args, **kwargs):
        """
        Hold the loader's lock for the whole call.
        """
        with _LOCK:
            return function(*args, **kwargs)
    return run


def discover(directory: str = "") -> dict:
    """
    Every plugin on disk: the shipped ones, then the site's own. A site plugin cannot take
    a shipped plugin's name, or it would replace shipped code without anyone seeing it.
    """
    if directory:
        return _scan(directory)
    config = settings().plugins
    found = _scan(config.directory)
    if config.site_directory and os.path.isdir(config.site_directory):
        for name, path in _scan(config.site_directory).items():
            if name in found:
                LOGGER.warning("site plugin %s ignored: a shipped plugin has that name", path)
                continue
            found[name] = path
    return found


def _scan(directory: str) -> dict:
    """
    The plugins in one directory: a directory with a manifest, or a single script.
    """
    found = {}
    if not os.path.isdir(directory):
        LOGGER.warning("no plugins directory at %s", directory)
        return found
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "manifest.yaml")):
            found[name] = path
        elif name.endswith(".py") and not name.startswith("_"):
            found[name[:-3]] = path
    return found


def _purge(name: str) -> None:
    """
    Drop the plugin's modules so a re-plug genuinely runs the new code.
    """
    prefix = f"{MODULE_PREFIX}{name}"
    for module in [key for key in sys.modules if key == prefix or key.startswith(prefix + ".")]:
        sys.modules.pop(module, None)


def _import_package(name: str, path: str):
    """
    Import a plugin directory as a package, so its own imports resolve.
    """
    package = f"{MODULE_PREFIX}{name}"
    init = os.path.join(path, "__init__.py")
    if os.path.isfile(init):
        spec = importlib.util.spec_from_file_location(
            package, init, submodule_search_locations=[path])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    else:
        spec = importlib.machinery.ModuleSpec(package, None, is_package=True)
        spec.submodule_search_locations = [path]
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
    return package


def _import_script(name: str, path: str):
    """
    Import a single-file plugin, which is the shape Luna's plugins have.
    """
    module_name = f"{MODULE_PREFIX}{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _from_script(name: str, path: str):
    """
    Read a manifest off a Plugin class attribute, and return the backend.
    """
    module = _import_script(name, path)
    plugin_class = getattr(module, "Plugin", None)
    if plugin_class is None:
        raise PluginRefused(f"{path} has no Plugin class")
    endpoints = getattr(plugin_class, "endpoints", None)
    if not endpoints:
        # The shape Luna's plugins have: methods and no declaration of routes.
        raise PluginRefused("class declares no endpoints, so it serves no routes")
    document = {
        "plugin": getattr(plugin_class, "name", name),
        "backend": "inline",
        "schemas": getattr(plugin_class, "schemas", {}) or {},
        "endpoints": endpoints,
        "luna_hooks": getattr(plugin_class, "luna_hooks", {}) or {},
    }
    return manifests.parse(document), plugin_class()


def _from_directory(name: str, path: str):
    """
    Read manifest.yaml and import the backend module it names.
    """
    document = manifests.read_yaml(os.path.join(path, "manifest.yaml"))
    manifest = manifests.parse(document)
    package = _import_package(name, path)
    try:
        module = importlib.import_module(f"{package}.{manifest.backend}")
    except ImportError as error:
        raise PluginRefused(f"backend {manifest.backend} does not import: {error}") from None
    backend_class = getattr(module, "Backend", None)
    if backend_class is None:
        raise PluginRefused(f"{manifest.backend} has no Backend class")
    return manifest, backend_class()


def _collect_renders(manifest, backend) -> list:
    """
    Every declared render with its producer, checked but not yet handed to the engine.
    """
    found = []
    for endpoint in manifest.endpoints:
        if endpoint.render is None:
            continue
        slug = endpoint.path.strip("/").replace("/", "_").replace("-", "_")
        method = endpoint.handler or f"render_{slug}"
        try:
            seconds(endpoint.render.coalesce, 2)
        except ValueError as error:
            raise PluginRefused(f"{endpoint.path}: coalesce {error}") from None
        producer = getattr(backend, method, None)
        if producer is None or not callable(producer):
            raise PluginRefused(
                f"{endpoint.path} declares a render with no producer named {method} "
                "on the backend")
        found.append((endpoint.path, endpoint.render, producer))
    return found


def _register_renders(plugin: str, found: list) -> None:
    """
    Replace a plugin's renders with the new ones, once the plugin is certain to load.
    """
    engine = renders()
    engine.forget(plugin)
    for path, spec, producer in found:
        engine.register(plugin, path, spec, producer)


def _bind_hooks(manifest, backend) -> dict:
    """
    The backend method behind each change at Luna the manifest declares.
    """
    hooks = {}
    for event, handler in manifest.luna_hooks.items():
        found = getattr(backend, handler, None)
        if found is None or not callable(found):
            raise PluginRefused(
                f"{event} names handler {handler}, which {manifest.backend} does not have")
        hooks[event] = found
    return hooks


@_serialised
def _daemon_paths(app: FastAPI, known) -> list:
    """
    The paths the daemon serves itself; an included router keeps its routes inside it.
    """
    paths = []
    for route in app.router.routes:
        if known.owner(route):
            continue
        inner = getattr(route, "original_router", None)
        for item in (inner.routes if inner is not None else [route]):
            if getattr(item, "path", ""):
                paths.append(item.path)
    return paths


def load(app: FastAPI, name: str, path: str = "") -> LoadedPlugin:
    """
    Load or reload one plugin. On a refusal nothing already serving is disturbed.
    """
    known = registry()
    if name in RESERVED_PLUGINS:
        reason = f"{name} is reserved: the daemon serves {API_PREFIX}/{name} itself"
        known.refuse(name, reason)
        raise PluginRefused(reason)

    path = path or discover().get(name, "")
    if not path:
        raise NotFound(f"no plugin named {name}")

    _purge(name)
    try:
        if os.path.isdir(path):
            manifest, backend = _from_directory(name, path)
        else:
            manifest, backend = _from_script(name, path)
        if manifest.plugin != name:
            raise PluginRefused(
                f"manifest calls itself {manifest.plugin} but sits at {name}")

        models_for_plugin = manifests.build_models(manifest)
        found_renders = _collect_renders(manifest, backend)
        hooks = _bind_hooks(manifest, backend)
        router = binder.build(manifest, backend, models_for_plugin, renders())

        taken = known.paths(skip=name)
        for path in _daemon_paths(app, known):
            taken.setdefault(path, "the daemon itself")
        clash = [route.path for route in router.routes if route.path in taken]
        if clash:
            raise PluginRefused(
                f"{clash[0]} is already served by {taken[clash[0]]}")
    except Exception as error:
        # Whatever is already serving keeps serving. The caller decides whether
        # that is fatal: at startup it is not, at the control endpoint it is.
        reason = error.message if isinstance(error, PluginRefused) else str(error)
        LOGGER.error("plugin %s refused: %s", name, reason)
        _remember(known.refuse(name, reason, path))
        raise PluginRefused(reason) from None

    _register_renders(name, found_renders)
    entry = LoadedPlugin(name, PLUGIN_STATE_LOADED)
    entry.manifest = manifest
    entry.backend = backend
    entry.models = models_for_plugin
    entry.hooks = hooks
    entry.digest = manifest.digest
    entry.origin = path
    entry.routes = exchange(app, name, router)
    known.put(entry)
    _remember(entry)
    LOGGER.info("plugin %s loaded from %s with %d routes", name, path, len(entry.routes))
    return entry


@_serialised
def unload(app: FastAPI, name: str) -> LoadedPlugin:
    """
    Take a plugin out of the route table. Its paths answer 404 from the next request.
    """
    known = registry()
    if known.get(name) is None:
        raise NotFound(f"no plugin named {name}")
    exchange(app, name, None)
    renders().forget(name)
    entry = known.disable(name)
    _purge(name)
    _remember(entry)
    LOGGER.info("plugin %s unplugged", name)
    return entry


@_serialised
def load_all(app: FastAPI) -> dict:
    """
    Load everything discovered, honouring the enabled list and stored state.
    """
    wanted = settings().plugins.enabled
    disabled = _disabled_names()
    result = {}
    for name, path in discover().items():
        if wanted and name not in wanted:
            LOGGER.info("plugin %s present but not in the enabled list", name)
            continue
        if name in disabled:
            LOGGER.info("plugin %s left unplugged, as recorded", name)
            registry().disable(name)
            continue
        try:
            result[name] = load(app, name, path).state
        except PluginRefused as error:
            # One bad plugin must not stop the daemon from serving the others.
            result[name] = f"refused: {error.message}"
    return result


def _disabled_names() -> set:
    """
    Plugins recorded as unplugged here, so a restart agrees.
    """
    handle = database.session()
    try:
        return set(handle.scalars(
            select(models.PluginState.name)
            .where(models.PluginState.enabled.is_(False))).all())
    except Exception:
        return set()
    finally:
        handle.close()


def _remember(entry: LoadedPlugin) -> None:
    """
    Record what this plugin is doing, so a restart agrees with the running state.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.PluginState)
                            .where(models.PluginState.name == entry.name))
        if row is None:
            row = models.PluginState(name=entry.name)
            handle.add(row)
        row.state = entry.state
        row.reason = entry.reason
        row.digest = entry.digest
        row.enabled = entry.state != PLUGIN_STATE_DISABLED
        handle.commit()
    except Exception as error:
        handle.rollback()
        LOGGER.warning("could not record plugin state for %s: %s", entry.name, error)
    finally:
        handle.close()
