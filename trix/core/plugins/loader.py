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

import importlib
import importlib.util
import os
import sys

from fastapi import FastAPI
from sqlalchemy import select

from trix.common import logging as trixlog
from trix.common.constants import (PLUGIN_STATE_DISABLED, PLUGIN_STATE_LOADED,
                                   RESERVED_PLUGIN)
from trix.common.errors import NotFound, PluginRefused
from trix.common.settings import current as settings
from trix.core.plugins import binder, manifest as manifests
from trix.core.plugins.registry import LoadedPlugin, current as registry
from trix.core.plugins.swap import exchange
from trix.core.render.engine import current as renders
from trix.db import engine as database
from trix.db import models

LOGGER = trixlog.get("loader")

MODULE_PREFIX: str = "trix_plugin_"


def discover(directory: str = "") -> dict:
    """
    Every plugin on disk: a directory with a manifest, or a single script.
    """
    directory = directory or settings().plugins.directory
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
        # Today's shape: a class with methods and no declaration. It loads as a
        # library other plugins can import, and serves nothing.
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


def _register_renders(manifest, backend) -> None:
    """
    Hand every declared render to the engine, with its producer.
    """
    engine = renders()
    engine.forget(manifest.plugin)
    for endpoint in manifest.endpoints:
        if endpoint.render is None:
            continue
        slug = endpoint.path.strip("/").replace("/", "_").replace("-", "_")
        producer = getattr(backend, endpoint.handler or f"render_{slug}", None)
        if producer is None or not callable(producer):
            raise PluginRefused(
                f"{endpoint.path} declares a render with no producer "
                f"named render_{slug} on the backend")
        engine.register(manifest.plugin, endpoint.path, endpoint.render, producer)


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


def load(app: FastAPI, name: str, path: str = "") -> LoadedPlugin:
    """
    Load or reload one plugin. On a refusal nothing already serving is disturbed.
    """
    known = registry()
    if name == RESERVED_PLUGIN:
        reason = f"{RESERVED_PLUGIN} is reserved for the pass-through to Luna"
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
        _register_renders(manifest, backend)
        hooks = _bind_hooks(manifest, backend)
        router = binder.build(manifest, backend, models_for_plugin, renders())

        taken = known.paths(skip=name)
        clash = [route.path for route in router.routes if route.path in taken]
        if clash:
            raise PluginRefused(
                f"{clash[0]} is already served by {taken[clash[0]]}")
    except Exception as error:
        # Whatever is already serving keeps serving. The caller decides whether
        # that is fatal: at startup it is not, at the control endpoint it is.
        reason = error.message if isinstance(error, PluginRefused) else str(error)
        LOGGER.error("plugin %s refused: %s", name, reason)
        renders().forget(name)
        _remember(known.refuse(name, reason, path))
        raise PluginRefused(reason) from None

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
