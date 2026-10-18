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
import inspect
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
from trixd.core.render import triggers
from trixd.core.render.engine import current as renders
from trixd.core.security import nodes
from trixd.core import hooks as dispatch
from trixd.core import tasks
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
        elif os.path.isdir(path) and not name.startswith("_"):
            # Silence here reads as an empty directory: a plugin whose manifest is missing
            # or misnamed is never discovered, so it is never refused either and the author
            # is told nothing at all.
            LOGGER.warning("%s has no manifest.yaml, so it is not a plugin", path)
        elif name.endswith(".py") and not name.startswith("_"):
            found[name[:-3]] = path
    return found


def _install(name: str) -> None:
    """
    Let a plugin plug itself in, once it has loaded.

    Anything a plugin offers the daemon - a node source, a trigger, a documentation link -
    belongs here and not in its package's import. Another plugin reaching for this one's
    client imports its package on the way, so an import that registers plugs in a plugin
    nobody enabled: it polls, it answers, and the control surface neither shows it nor can
    stop it. A plugin with nothing to offer needs no install.
    """
    # Looked up rather than passed down: the import just above put it here, under the same
    # prefix for a package and for a single-file plugin, so the shape of what loaded does
    # not have to travel through every return on the way.
    package = sys.modules.get(f"{MODULE_PREFIX}{name}")
    install = getattr(package, "install", None)
    if install is None:
        return
    try:
        install()
    except Exception as error:
        # A sequence of registrations, so one failing part way through leaves the ones
        # before it: a trigger polling, or a node source answering, for a refused plugin.
        triggers.forget(name)
        nodes.forget(name)
        raise PluginRefused(f"{name} could not plug itself in: {error}") from None


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
        "hooks": getattr(plugin_class, "hooks", {}) or {},
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


def _render_producer(endpoint, backend):
    """
    The producer behind one declared render, refusing what the engine could not drive.

    The command-line check calls this too, so what it passes is what the load path accepts.
    """
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
    return producer


def _collect_renders(manifest, backend) -> list:
    """
    Every declared render with its producer, checked but not yet handed to the engine.
    """
    found = []
    for endpoint in manifest.endpoints:
        if endpoint.render is None:
            continue
        found.append((endpoint.path, endpoint.render,
                      _render_producer(endpoint, backend)))
    return found


def _register_renders(plugin: str, found: list) -> None:
    """
    Replace a plugin's renders with the new ones, once the plugin is certain to load.
    """
    renders().replace(plugin, found)


def _hook_signature(event: str, handler: str, found) -> None:
    """
    Refuse a hook handler the dispatcher could not call, as the binder does for an endpoint.

    A change is handed over as `handler(**payload)`, so every value arrives by keyword and
    a parameter that can only be filled positionally never is. Nothing else about the shape
    is checkable here: no plugin declares what an event's payload carries, so whether a
    handler takes the fields it will be sent is settled by the derived test over the
    publishers rather than guessed at from the event's name.
    """
    try:
        accepted = inspect.signature(found).parameters
    except (TypeError, ValueError):
        return
    unfillable = [name for name, item in accepted.items()
                  if item.kind == inspect.Parameter.POSITIONAL_ONLY
                  and item.default is inspect.Parameter.empty]
    if unfillable:
        raise PluginRefused(
            f"{event} names handler {handler}, which needs {', '.join(unfillable)} "
            f"positionally; a hook is handed its payload as keyword arguments")


def _hook_handler(manifest, backend, event: str, handler: str):
    """
    The backend method behind one declared change, refusing what the dispatcher could not
    call.

    The command-line check calls this too, so what it passes is what the load path accepts.
    A refusal added here is reached from both; one added beside it, in either caller, is
    reached from one.
    """
    found = getattr(backend, handler, None)
    if found is None or not callable(found):
        raise PluginRefused(
            f"{event} names handler {handler}, which {manifest.backend} does not have")
    _hook_signature(event, handler, found)
    return found


def _bind_hooks(manifest, backend) -> dict:
    """
    The backend method behind each change the manifest declares.
    """
    return {event: _hook_handler(manifest, backend, event, handler)
            for event, handler in manifest.hooks.items()}


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


@_serialised
def load(app: FastAPI, name: str, path: str = "") -> LoadedPlugin:
    """
    Load or reload one plugin. On a refusal nothing already serving is disturbed.
    """
    known = registry()
    if name in RESERVED_PLUGINS:
        # Not a plugin at all, so nothing is recorded: the plugin list stays plugins only.
        raise PluginRefused(f"{name} is reserved: the daemon serves {API_PREFIX}/{name} itself")

    path = path or discover().get(name, "")
    if not path:
        raise NotFound(f"no plugin named {name}")

    # What answers for this plugin's queue before anything here touches it: its own handler
    # from an earlier load, or the hook dispatcher where another plugin declares hooks on
    # this source. Read before the purge, because the import below replaces it.
    standing = tasks.handler(name)
    _purge(name)
    try:
        if os.path.isdir(path):
            manifest, backend = _from_directory(name, path)
        else:
            manifest, backend = _from_script(name, path)
        if manifest.plugin != name:
            raise PluginRefused(
                f"manifest calls itself {manifest.plugin} but sits at {name}")
        # Only the command-line check looked for these, and a reload does not run it.
        twice = manifests.duplicates(manifest)
        if twice:
            raise PluginRefused(twice[0])

        models_for_plugin = manifests.build_models(manifest)
        found_renders = _collect_renders(manifest, backend)
        hooks = _bind_hooks(manifest, backend)
        router = binder.build(manifest, backend, models_for_plugin, renders())

        taken = known.paths(skip=name)
        for daemon_path in _daemon_paths(app, known):
            taken.setdefault(daemon_path, "the daemon itself")
        clash = [route.path for route in router.routes if route.path in taken]
        if clash:
            raise PluginRefused(
                f"{clash[0]} is already served by {taken[clash[0]]}")

        # Inside the try, because it can fail: plugged in after the routes went live, a
        # plugin that refused was already serving them and the reconcile never ran.
        _install(name)
    except Exception as error:
        # Whatever is already serving keeps serving. The caller decides whether
        # that is fatal: at startup it is not, at the control endpoint it is.
        # What answered for its queue is put back as it was found. The import above
        # replaced that, so a refused plugin used to go on answering for its own subsystem
        # and a notify naming it was dispatched into code that had just been refused -
        # while dropping the registration instead would unplug the queue of a plugin whose
        # routes are still serving, because three refusals come before that import.
        if standing is None:
            tasks.unregister(name)
        else:
            tasks.register(name, standing)
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
    # The hooks it declared may name a source nothing answered for yet, and dispatch is the
    # core's rather than the publisher's, so it is settled from the manifests after a load.
    dispatch.reconcile()
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
    # Routes and renders were the only things let go of. Its task handler stayed, so the
    # daemon still accepted notifications for it and dispatched them into unplugged code,
    # and its triggers kept polling and kept waking renders - a disabled plugin that was
    # still running.
    tasks.unregister(name)
    triggers.forget(name)
    # The node source too, where this plugin offered it: a node sign-in asking an unplugged
    # plugin's client whether Luna knows the node, and issuing a token on the answer, is
    # worse than refusing because nothing can answer.
    nodes.forget(name)
    entry = known.disable(name)
    _purge(name)
    _remember(entry)
    # After the unregister above, which takes back the handler under this plugin's own name:
    # where that name is also a source another loaded plugin declares a hook on, dispatch
    # for it is put back. Unplugging the publisher used to stop every plugin's hooks.
    dispatch.reconcile()
    LOGGER.info("plugin %s unplugged", name)
    return entry


@_serialised
def load_all(app: FastAPI) -> dict:
    """
    Load everything discovered, honouring the enabled list and stored state.
    """
    wanted = settings().plugins.enabled
    disabled = _disabled_names()
    found = discover()
    if not wanted:
        # An empty list has always meant every plugin, which is not what "loads only
        # what ENABLED lists" reads like; say the count rather than change it.
        LOGGER.warning("[PLUGINS] ENABLED names nothing, which enables every plugin "
                       "found: %d of them; list the plugins to load to narrow it",
                       len(found))
    for name in wanted:
        if name not in found:
            # Nothing is recorded for a name with nothing behind it, so it is absent from
            # the plugin list rather than refused in it: the only place it can be said is
            # here.
            LOGGER.warning("plugin %s is in the enabled list but not on disk", name)
    result = {}
    for name, path in found.items():
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
    except Exception as error:
        # Kept deliberately: at a first start the table is not there yet, and refusing to
        # load anything over that would be worse. Said out loud because the same empty
        # answer also means a readable table whose rows were not read.
        LOGGER.warning("could not read which plugins are unplugged, so none are: %s", error)
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
