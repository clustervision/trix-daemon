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

import requests
import urllib3
import yaml

from trix.common import logging as trixlog
from trix.common.errors import BackendFailure, NotFound
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


class Backend:
    """
    One writer of the Prometheus configuration.
    """

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

    def tuning(self, body: dict = None) -> dict:
        """
        Drop lists, rule group intervals, scrape intervals and retention.
        """
        if body:
            return declarations.merge(PLUGIN, "tuning", "", body, {})
        return declarations.read(PLUGIN, "tuning", "", {})

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
        body = body or {}
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
        self.reload()
        LOGGER.info("hardware rules written for %s at %s", node, path)
        return {"hostname": _hw_host(path), "rules": sum(len(group.get("rules", []))
                                                         for group in document.get("groups", []))}

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
                 f'description!~"Virt|virt|USB", product!~"Virt.*"}}) by ({",".join(COMPONENT_LABELS)})')
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
        Write the generic and service rule documents, then reload.
        """
        stored = declarations.read(PLUGIN, "rules", "", None)
        if stored is None:
            return {"written": []}
        written = self._write_rule_documents(stored)
        if written:
            self.reload()
        return {"written": written}

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
            if not subset:
                continue
            path = os.path.join(directory, filename)
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


def _read_yaml(path: str) -> dict:
    """
    One rule file as a document, empty when the file is.
    """
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {"groups": []}


def _provision_address(node: dict) -> str:
    """
    The address on the interface the node provisions over.
    """
    wanted = node.get("provision_interface")
    for interface in node.get("interfaces") or []:
        if interface.get("interface") == wanted:
            return interface.get("ipaddress") or ""
    return ""
