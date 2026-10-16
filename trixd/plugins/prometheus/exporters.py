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
Which exporters are scraped, and where.

The baseline is a file the installer renders from the exporter roles themselves, so the ports
are stated once, where the exporters are installed. The database holds only what a site has
changed on top of it - which is why an empty table means "as shipped" and an upgrade renders
what it rendered yesterday.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, ClusterVision Solutions b.v."
__license__     = "GPL"
__version__     = "2.2"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Development"

import hashlib
import os
import threading

import yaml

from trixd.common import logging as trixlog

LOGGER = trixlog.get("prometheus")

# The file's two sections: one ladder for Luna's nodes, one for the controllers. They are kept
# apart so a rule written for every node does not quietly land on a controller as well.
NODES: str = "nodes"
CONTROLLERS: str = "controllers"
# The controllers themselves, with the address to scrape each on. Luna knows nodes, not
# controllers, and the installer already writes these addresses for the file it replaces - so it
# states them here rather than having the daemon guess an interface or depend on DNS.
HOSTS: str = "hosts"

# A definition names an exporter; a scope says where it applies and what it overrides there.
DEFINITION: str = "exporter"
SCOPE: str = "exporter-scope"

# Scope names. A controller's own scope is its hostname, so the two controllers never write the
# same row: the table replicates, and one row meaning "me" would have each overwriting the other.
CLUSTER: str = "cluster"
EVERY_CONTROLLER: str = "controllers"
GROUP: str = "group:{}"
NODE: str = "node:{}"

_held: dict = {}
_lock = threading.Lock()


def _sections(body: dict, path: str) -> dict:
    """
    The file as this understands it, or nothing where it is shaped wrongly.

    A port written where a mapping belongs is an ordinary typo, and it must not reach the
    render: an exception there leaves a restarted daemon with no document at all, which is a
    404 to every poll rather than a stale answer.
    """
    for section in (NODES, CONTROLLERS, HOSTS):
        held = body.get(section)
        if held is None:
            continue
        if not isinstance(held, dict) or not all(isinstance(v, dict) or v is None
                                                 for v in held.values()):
            LOGGER.error("%s in %s is not a mapping of name to fields, ignoring the file",
                         section, path)
            return {}
    return body


def factory(path: str) -> dict:
    """
    The file as it is now, read again only when it has changed.

    A render asks for this on every pass, and a pass can follow every node change, so the cost
    of being wrong here is paid per boot in a boot storm. The stamp is the size as well as the
    time: two writes inside one timestamp tick are not rare, and the cache beside this one in
    the backend pairs them for the same reason.
    """
    try:
        status = os.stat(path)
        stamp = (status.st_mtime, status.st_size)
    except OSError as error:
        with _lock:
            held = _held.get("body", {}) if _held.get("path") == path else {}
        if held:
            # It was there a moment ago. Saying nothing here is how every controller target
            # disappears and every node silently moves back to the ports built into the code.
            LOGGER.error("the exporter file %s has gone, keeping what was loaded: %s",
                         path, error)
        return held
    with _lock:
        if _held.get("path") == path and _held.get("stamp") == stamp:
            return _held["body"]
    try:
        with open(path, "r", encoding="utf-8") as handle:
            body = yaml.safe_load(handle) or {}
            # Stamped from the handle, so a file replaced between the stat and the open is not
            # remembered under the previous file's mtime.
            status = os.fstat(handle.fileno())
            stamp = (status.st_mtime, status.st_size)
    except (OSError, yaml.YAMLError) as error:
        LOGGER.error("the exporter file %s could not be read, keeping what was loaded: %s",
                     path, error)
        with _lock:
            return _held.get("body", {}) if _held.get("path") == path else {}
    if not isinstance(body, dict):
        LOGGER.error("the exporter file %s is not a mapping, ignoring it", path)
        body = {}
    body = _sections(body, path)
    with _lock:
        _held.update({"path": path, "stamp": stamp, "body": body})
    LOGGER.info("read %d node and %d controller exporter(s) from %s",
                len(body.get(NODES) or {}), len(body.get(CONTROLLERS) or {}), path)
    return body


def digest(path: str) -> str:
    """
    What the file says, as one short string, so two controllers can be compared in one call.

    The file is not replicated - only the deviations are - so two controllers can disagree
    about the baseline while the database looks perfectly converged.
    """
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError:
        return ""


def _applied(base: dict, deviation) -> dict:
    """
    One exporter after one scope has had its say, or None where the scope switches it off.

    False is off, True is on as it comes, and a mapping is on with those fields changed - so a
    scope can move a port or an interval without restating the rest.
    """
    if deviation is False or deviation is None:
        return None
    if deviation is True:
        return dict(base)
    if not isinstance(deviation, dict):
        LOGGER.warning("an exporter deviation is %s, which is not a value this understands",
                       type(deviation).__name__)
        return dict(base)
    merged = dict(base)
    merged.update(deviation)
    return merged


def resolve(baseline: dict, definitions: dict, scopes: list) -> dict:
    """
    What is scraped somewhere: the baseline, what the site has defined on top, then each scope
    in turn from the least specific to the most.

    A scope may name an exporter the baseline has never heard of, which defines it rather than
    failing - that is how a site adds its own.
    """
    resolved = {name: dict(fields or {}) for name, fields in (baseline or {}).items()}
    for name, fields in (definitions or {}).items():
        resolved[name] = {**resolved.get(name, {}), **(fields or {})}
    for body in scopes:
        for name, deviation in (body or {}).items():
            applied = _applied(resolved.get(name, {}), deviation)
            if applied is None:
                resolved.pop(name, None)
            else:
                resolved[name] = applied
    return {name: fields for name, fields in resolved.items() if fields.get("port")}


def for_node(name: str, group: str, baseline: dict, definitions: dict, rows: dict) -> dict:
    """
    One node: the cluster, then its group, then the node itself.
    """
    return resolve(baseline.get(NODES) or {}, definitions,
                   [rows.get(CLUSTER), rows.get(GROUP.format(group)) if group else None,
                    rows.get(NODE.format(name))])


def for_controller(hostname: str, baseline: dict, definitions: dict, rows: dict) -> dict:
    """
    One controller: every controller, then this one by its own name.
    """
    return resolve(baseline.get(CONTROLLERS) or {}, definitions,
                   [rows.get(EVERY_CONTROLLER), rows.get(hostname)])
