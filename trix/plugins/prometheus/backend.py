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
Service discovery targets, alert rules and the per-node hardware rules.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import glob
import os
import time
import re

import requests
import urllib3
import yaml

from trix.common import logging as trixlog
from trix.common.errors import BackendFailure, BadRequest, NotFound, TrixError
from trix.core.luna.client import current as luna
from trix.utils import declarations, files
from trix.utils import shell

LOGGER = trixlog.get("prometheus")

urllib3.disable_warnings()

PLUGIN: str = "prometheus"

DEFAULT_CONFIG: dict = {
    "rules_directory": "/trinity/local/etc/prometheus_server/rules/",
    "prometheus_url": "http://localhost:9090",
    "reload_command": "systemctl reload prometheus-server",
}

# Exporter name to the port it answers on. A node is a target once per exporter.
EXPORTERS: dict = {
    "node": 14200,
    "ipmi": 14202,
    "nvidia": 14203,
    "slurm_job": 14207,
    "lshw": 14208,
}

GENERIC_RULES: str = "trix.generic.rules"
SERVICE_RULES: str = "trix.service.rules"
HW_RULES: str = "trix.hw.{host}.rules"
# libyaml's safe loader where it is installed: the same documents, an order of magnitude
# faster, which is what a reconcile over every node's file needs.
SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# A node or host name as Luna holds it; it ends up in a file name, so nothing else passes.
NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SETTINGS_FILE: str = "rules_settings.yaml"

# The hardware rules take the shape Luna's generator gave them, so a file written by
# either daemon reads the same: this metric, these labels, this alert and group.
HW_METRIC: str = "lshw_device"
HW_GROUP: str = "trinityx_hw"
HW_ALERT: str = "MissingHardwareComponent"
HW_CLASSES: str = "processor|memory|disk|network|display"
HW_DESCRIPTION: str = ("Hardware component {{ $labels.class }} with {{ $labels.product }} "
                       "{{ $labels.vendor }} {{ $labels.serial }} is missing.")
COMPONENT_LABELS: tuple = ("hostname", "luna_group", "class", "description", "product",
                           "vendor", "serial")
# InfiniBand reports no serial, so those rules select on one label fewer.
INFINIBAND_LABELS: tuple = COMPONENT_LABELS[:-1]
# Matchers on these labels say which node a rule is about; the others name the part.
NODE_LABELS: tuple = ("hostname", "luna_group")
# One label matcher inside a selector: name, operator, quoted value with escapes.
MATCHER = re.compile(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')


class Backend:
    """
    One writer of the Prometheus configuration.
    """

    def __init__(self) -> None:
        """
        No hardware file seen yet.
        """
        # path -> (mtime, size, hostname, group) of a file as last read or written here, so a
        # reconcile with nothing to change reads no file: at a thousand nodes, the parse is
        # the whole cost.
        self.seen: dict = {}

    # ---------------------------------------------------------------- config

    def config(self, body: dict = None) -> dict:
        """
        Where this plugin writes and what it reloads.
        """
        if body:
            return declarations.merge(PLUGIN, "config", "", body, DEFAULT_CONFIG)
        return declarations.read(PLUGIN, "config", "", DEFAULT_CONFIG)

    def _config(self) -> dict:
        """
        The stored configuration, with the shipped defaults under it.
        """
        return declarations.read(PLUGIN, "config", "", DEFAULT_CONFIG)

    def _rules_dir(self) -> str:
        """
        Where the rule files are written on this controller.
        """
        return self._config().get("rules_directory", DEFAULT_CONFIG["rules_directory"])

    # ---------------------------------------------------------------- rules

    def rules(self, body: dict = None) -> dict:
        """
        The generic and service alert rules, as documents rather than files.
        """
        if body:
            stored = declarations.write(PLUGIN, "rules", "", body)
            self._write_rule_documents(stored)
            self.reload()
            return stored
        return declarations.read(PLUGIN, "rules", "", {"groups": []})

    def settings(self, body: dict = None) -> dict:
        """
        The settings the hardware rules are generated from.
        """
        if body:
            stored = declarations.write(PLUGIN, "settings", "", body)
            self._write_settings(stored)
            return stored
        held = declarations.read(PLUGIN, "settings", "", None)
        if held is not None:
            return held
        return {"settings": self._read_settings_file()}

    # ------------------------------------------------------------- hardware

    def get_hw_rules(self, node: str) -> dict:
        """
        What is currently held for one node.
        """
        paths = self._hw_paths(node)
        if not paths:
            raise NotFound(f"no hardware rules held for {node}")
        return _read_yaml(paths[0])

    def import_hw_rules(self, node: str, body: dict = None) -> dict:
        """
        Generate this node's rule file from what Prometheus knows about it. The
        node asks for this itself after boot, with a token scoped to itself.
        """
        written = self._write_hw_rules(node, body or {})
        self.reload()
        return written

    def set_hw_rules(self, body: dict = None) -> dict:
        """
        Several nodes at once, as alertx's save-nodes asks: regenerate (force), or set
        whether a node's alerts count for health (nhc) and fire at all (disabled).
        """
        results = []
        for entry in (body or {}).get("nodes") or []:
            node = str((entry or {}).get("node") or "")
            if not NODE_NAME.match(node):
                raise BadRequest(f"{node or 'an entry'} does not name a node")
            if luna().node(node.split(".")[0]) is None:
                results.append({"node": node, "ok": False, "message": "not a node Luna knows"})
                continue
            try:
                results.append({"node": node, "ok": True, **self._write_hw_rules(node, entry)})
            except TrixError as error:
                results.append({"node": node, "ok": False, "message": error.message})
        # One reload for the lot, not one per node.
        if any(item["ok"] for item in results):
            self.reload()
        return {"nodes": results}

    def _write_hw_rules(self, node: str, body: dict) -> dict:
        """
        Write one node's rule file, generated afresh when asked or when there is none.
        """
        paths = self._hw_paths(node)
        if paths and not body.get("force"):
            path, document = paths[0], _read_yaml(paths[0])
        else:
            host = self._host_of(node)
            path = os.path.join(self._rules_dir(), HW_RULES.format(host=host))
            document = self._generate_hw_rules(host)

        for group in document.get("groups", []):
            for rule in group.get("rules", []):
                labels = rule.setdefault("labels", {})
                if body.get("nhc") is not None:
                    labels["nhc"] = bool(body["nhc"])
                if body.get("disabled") is not None:
                    labels["disabled"] = bool(body["disabled"])

        files.write_yaml(path, document)
        LOGGER.info("hardware rules written for %s at %s", node, path)
        return {"hostname": _hw_host(path), "rules": sum(len(group.get("rules", []))
                                                         for group in document.get("groups", []))}

    def repair_hw_rules(self, node: str, was: str = "") -> dict:
        """
        Make the files that name a node carry the host and group Luna holds for it now;
        was is the name they were written under, when the node has just been renamed.
        """
        record = luna().node(node.split(".")[0])
        if record is None:
            return {"node": node, "skipped": "not in Luna"}
        host = record.get("hostname") or node
        want = {"hostname": host, "luna_group": record.get("group") or ""}
        target = os.path.join(self._rules_dir(), HW_RULES.format(host=host))
        paths = (self._hw_paths(was) + self._hw_paths(node)) if was else self._hw_paths(node)
        moved, rewritten = [], []
        for path in dict.fromkeys(paths):
            if not os.path.isfile(path):
                continue
            try:
                document = _read_yaml(path)
            except FileNotFoundError:
                continue
            changed = _relabel_document(document, want)
            if path == target and not changed:
                continue
            files.write_yaml(target, document)
            if path != target:
                _discard(path)
                moved.append(os.path.basename(path))
            if changed:
                rewritten.append(os.path.basename(target))
        if moved or rewritten:
            self.reload()
            LOGGER.info("hardware rules for %s: moved %s, rewritten %s", node, moved, rewritten)
        return {"node": node, "hostname": host, "moved": moved, "rewritten": rewritten}

    def remove_hw_rules(self, node: str) -> dict:
        """
        Retire the files that name a node that is gone: out of the glob Prometheus loads.
        """
        retired = [name for name in (self._retire(path) for path in self._hw_paths(node)) if name]
        if retired:
            self.reload()
        return {"node": node, "retired": retired}

    def node_updated(self, name: str, newnodename: str = "", group: str = "") -> str:
        """
        Luna accepted a rename or a move between groups: the rule files follow.
        """
        outcome = self.repair_hw_rules(newnodename or name, was=name if newnodename else "")
        return f"{name}: moved {outcome.get('moved', [])}, rewritten {outcome.get('rewritten', [])}"

    def node_deleted(self, name: str) -> str:
        """
        Luna removed the node: whatever still names it is retired.
        """
        return f"{name}: retired {self.remove_hw_rules(name)['retired']}"

    def _retire(self, path: str) -> str:
        """
        Move one rule file under backup/ with a timestamp, so nothing is lost and nothing loads.
        """
        backup = os.path.join(self._rules_dir(), "backup")
        os.makedirs(backup, exist_ok=True)
        kept = os.path.join(backup, f"{os.path.basename(path)}.{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(path, kept)
        except FileNotFoundError:
            return ""
        LOGGER.info("retired %s to %s", path, kept)
        return os.path.basename(path)

    def _host_of(self, node: str) -> str:
        """
        The hostname Prometheus labels this node with: Luna's, as the targets render uses it.
        """
        record = luna().node(node.split(".")[0]) or {}
        return record.get("hostname") or node

    def _hw_paths(self, node: str) -> list:
        """
        The rule files that name this node, by full hostname or by short name.
        """
        pattern = os.path.join(self._rules_dir(), HW_RULES.format(host="*"))
        return sorted(path for path in glob.glob(pattern) if _names_node(_hw_host(path), node))

    def _generate_hw_rules(self, host: str) -> dict:
        """
        Ask Prometheus which components this host has, and build one rule each, as Luna does.
        """
        components = self._components(host)
        if not components:
            raise BackendFailure(
                f"no data found for {host}; the hardware exporter has not been scraped yet")
        hardware = (self.settings().get("settings") or {}).get("hw") or {}
        rules = []
        for component in components:
            labels = COMPONENT_LABELS
            if component.get("class") == "network" and component.get("description") == "interface":
                labels = INFINIBAND_LABELS
            selector = ",".join(f'{key}="{component[key]}"' for key in labels if component.get(key))
            rules.append({
                "alert": HW_ALERT,
                "expr": f"absent({HW_METRIC}{{{selector}}})",
                "labels": {"severity": "warning", "category": "hardware",
                           "nhc": bool(hardware.get("nhc", True)),
                           "disabled": bool(hardware.get("disabled", False))},
                "annotations": {"description": HW_DESCRIPTION},
            })
        return {"groups": [{"name": HW_GROUP, "rules": rules}]}

    def _components(self, host: str) -> list:
        """
        The hardware exporter's series for this host, one label set per component.
        """
        url = self._config().get("prometheus_url", DEFAULT_CONFIG["prometheus_url"])
        query = (f'count({HW_METRIC}{{class=~"{HW_CLASSES}", hostname="{host}", '
                 f'description!~"Virt|virt|USB", product!~"Virt.*"}}) '
                 f'by ({",".join(COMPONENT_LABELS)})')
        try:
            answer = requests.get(f"{url}/api/v1/query", params={"query": query},
                                  timeout=20, verify=False)
        except requests.RequestException as error:
            raise BackendFailure(f"Prometheus is not answering: {error}") from None
        if answer.status_code >= 400:
            raise BackendFailure(f"Prometheus answered {answer.status_code}")
        result = ((answer.json() or {}).get("data") or {}).get("result") or []
        return [dict(item.get("metric") or {}) for item in result]

    # --------------------------------------------------------------- renders

    def render_targets(self) -> list:
        """
        The service discovery document, built once per change, not per poll.
        """
        nodes = luna().nodes()
        targets = []
        for exporter, port in EXPORTERS.items():
            for name, node in nodes.items():
                address = _provision_address(node) or node.get("hostname") or name
                entry = {
                    "targets": [f"{address}:{port}"],
                    "labels": {"exporter": exporter,
                               "hostname": node.get("hostname") or name},
                }
                if node.get("group"):
                    entry["labels"]["luna_group"] = node["group"]
                targets.append(entry)
        LOGGER.info("rendered %d target(s) for %d node(s)", len(targets), len(nodes))
        return targets

    def render_rule_files(self) -> dict:
        """
        Write the generic and service documents, then make the hardware files agree with Luna.
        """
        stored = declarations.read(PLUGIN, "rules", "", None)
        written = self._write_rule_documents(stored) if stored is not None else []
        reconciled = self._reconcile_hw_rules(luna().nodes())
        if written or reconciled["repaired"] or reconciled["retired"]:
            self.reload()
        return {"written": written, **reconciled}

    def _reconcile_hw_rules(self, nodes: dict) -> dict:
        """
        Every hardware file names a node Luna has, under its current host and group, or it
        is retired. An empty node list is left alone, so nothing goes on Luna saying nothing.
        """
        repaired, retired = [], []
        if not nodes:
            return {"repaired": repaired, "retired": retired}
        by_host = {}
        for name, record in nodes.items():
            by_host.setdefault(name.split(".")[0], name)
            by_host[record.get("hostname") or name] = name
        # Read once: the setting lives in the database, and this loop runs per node.
        directory = self._rules_dir()
        for path in sorted(glob.glob(os.path.join(directory, HW_RULES.format(host="*")))):
            host = _hw_host(path)
            name = by_host.get(host) or by_host.get(host.split(".")[0])
            if name is None:
                gone = self._retire(path)
                if gone:
                    retired.append(gone)
                continue
            record = nodes[name]
            want = {"hostname": record.get("hostname") or name,
                    "luna_group": record.get("group") or ""}
            target = os.path.join(directory, HW_RULES.format(host=want["hostname"]))
            if path == target and self.seen.get(path) == _stamp(path, want):
                continue
            try:
                document = _read_yaml(path)
            except FileNotFoundError:
                continue
            changed = _relabel_document(document, want)
            if not changed and path == target:
                self.seen[path] = _stamp(path, want)
                continue
            files.write_yaml(target, document)
            self.seen[target] = _stamp(target, want)
            if path != target:
                _discard(path)
                self.seen.pop(path, None)
            repaired.append(os.path.basename(target))
        if repaired or retired:
            LOGGER.info("hardware rules reconciled: repaired %s, retired %s", repaired, retired)
        return {"repaired": repaired, "retired": retired}

    def _write_rule_documents(self, stored: dict) -> list:
        """
        Generic rules and service rules go to the two files Luna used.
        """
        directory = self._rules_dir()
        written = []
        groups = (stored or {}).get("groups") or []
        generic = [item for item in groups if not str(item.get("name", "")).startswith("service")]
        service = [item for item in groups if str(item.get("name", "")).startswith("service")]
        for filename, subset in ((GENERIC_RULES, generic), (SERVICE_RULES, service)):
            path = os.path.join(directory, filename)
            # An emptied subset is written empty, so a group taken out stops firing; a
            # file that already says the same is left alone, so nothing reloads for it.
            try:
                if _read_yaml(path) == {"groups": subset}:
                    continue
            except (OSError, yaml.YAMLError):
                pass
            files.write_yaml(path, {"groups": subset})
            written.append(path)
        return written

    def _write_settings(self, stored: dict) -> str:
        """
        Put the settings document on disk for Prometheus to read.
        """
        path = os.path.join(os.path.dirname(self._rules_dir().rstrip("/")), SETTINGS_FILE)
        files.write_yaml(path, (stored or {}).get("settings") or {})
        return path

    def _read_settings_file(self) -> dict:
        """
        What is on disk now, so a render can tell whether it changed.
        """
        path = os.path.join(os.path.dirname(self._rules_dir().rstrip("/")), SETTINGS_FILE)
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def reload(self) -> bool:
        """
        Tell Prometheus to re-read what we wrote.
        """
        command = self._config().get("reload_command", DEFAULT_CONFIG["reload_command"])
        result = shell.run(command.split(), timeout=30)
        if not result.ok:
            LOGGER.error("reload failed: %s", result.err or result.out)
        return result.ok


def _hw_host(path: str) -> str:
    """
    The host a hardware rule file names, read from its filename alone.
    """
    name = os.path.basename(path)
    prefix, suffix = HW_RULES.split("{host}")
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix):-len(suffix)]
    return ""


def _names_node(host: str, node: str) -> bool:
    """
    Whether a file's host is this node, by full hostname or by short name, never by prefix.
    """
    return bool(host) and (host == node or host.split(".")[0] == node.split(".")[0])


def _relabel(expr: str, values: dict) -> str:
    """
    Rewrite the equality matchers on the node labels inside an expression, and nothing else.
    """
    def one(match):
        """
        One matcher: replaced when it names a node label with =, kept to the character otherwise.
        """
        key, operator = match.group(1), match.group(2)
        if operator == "=" and key in NODE_LABELS and values.get(key):
            return f'{key}="{values[key]}"'
        return match.group(0)
    return MATCHER.sub(one, expr)


def _relabel_document(document: dict, values: dict) -> bool:
    """
    Apply the relabel to every rule in a document; True when anything changed.
    """
    changed = False
    for group in document.get("groups") or []:
        for rule in group.get("rules") or []:
            expr = str(rule.get("expr") or "")
            fixed = _relabel(expr, values)
            if fixed != expr:
                rule["expr"] = fixed
                changed = True
            labels = rule.get("labels") or {}
            host = values.get("hostname")
            if labels.get("hostname") and host and labels["hostname"] != host:
                labels["hostname"] = host
                changed = True
    return changed


def _discard(path: str) -> None:
    """
    Remove a file another repair may already have moved; absence is the outcome wanted.
    """
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _read_yaml(path: str) -> dict:
    """
    One rule file as a document, empty when the file is.
    """
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.load(handle, Loader=SAFE_LOADER) or {"groups": []}


def _stamp(path: str, want: dict) -> tuple:
    """
    What a file looked like and which host and group it named, or None if it is gone.
    """
    try:
        found = os.stat(path)
    except OSError:
        return None
    return (found.st_mtime_ns, found.st_size, want["hostname"], want["luna_group"])


def _provision_address(node: dict) -> str:
    """
    The address on the interface the node provisions over.
    """
    wanted = node.get("provision_interface")
    for interface in node.get("interfaces") or []:
        if interface.get("interface") == wanted:
            return interface.get("ipaddress") or ""
    return ""
