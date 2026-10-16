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
import re
import shutil
import threading
import time

import requests
import urllib3
import yaml

from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure, BadRequest, NotFound, TrixdError
from trixd.core.render.engine import current as renders
from trixd.plugins.luna.client import current as luna
from trixd.plugins.prometheus import exporters
from trixd.utils import declarations, files
from trixd.utils import shell

LOGGER = trixlog.get("prometheus")

urllib3.disable_warnings()

PLUGIN: str = "prometheus"

# What Prometheus accepts as a label name; anything else makes it refuse the whole document.
# Path words under /exporters, which an exporter may therefore not be called.
RESERVED: tuple = ("applied",)

DEFAULT_CONFIG: dict = {
    "exporters_file": "/trinity/local/trixd/daemon/config/exporters.yaml",
    "rules_directory": "/trinity/local/etc/prometheus_server/rules/",
    "prometheus_url": "https://localhost:9090",
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
        # Where Prometheus really answered, when that differs from the configured scheme.
        self.prometheus_url: str = ""

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
            self.reload_or_fail("rules written")
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
        # A node that rebooted unchanged costs no write and no reload: a boot storm of
        # thousands of nodes reloads Prometheus only for the ones that changed.
        if written["changed"]:
            self.reload_or_fail(f"rules written for {node}")
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
            except TrixdError as error:
                results.append({"node": node, "ok": False, "message": error.message})
        # One reload for the lot, not one per node, and none when nothing changed.
        changed = [item["node"] for item in results if item.get("changed")]
        if changed:
            self.reload_or_fail(f"rules written for {len(changed)} node(s)")
        return {"nodes": results}

    def list_hw_rules(self, request=None) -> dict:
        """
        Every node's hardware rules, or those of ?nodes=a,b, as Luna's export answers them;
        ?detail=no leaves the documents out. A node asked for and not held says so.
        """
        query = request.query_params if request is not None else {}
        asked = [name for name in str(query.get("nodes", "")).split(",") if name]
        detail = str(query.get("detail", "yes")).lower() not in ("0", "no", "false")
        pattern = os.path.join(self._rules_dir(), HW_RULES.format(host="*"))
        held = {_hw_host(path): path for path in sorted(glob.glob(pattern))}
        answer = []
        for host in (asked or sorted(held)):
            path = held.get(host) or next((held[h] for h in held if _names_node(h, host)), "")
            if not path:
                answer.append({"node": host, "exists": False})
                continue
            document = _read_yaml(path)
            rules = [rule for group in document.get("groups", [])
                     for rule in group.get("rules", [])]
            entry = {"node": host, "exists": True, "rules": len(rules)}
            if detail:
                entry["document"] = document
            answer.append(entry)
        return {"nodes": answer}

    def _write_hw_rules(self, node: str, body: dict) -> dict:
        """
        Write one node's rule file, generated afresh when asked or when there is none.
        """
        paths = self._hw_paths(node)
        before = _read_yaml(paths[0]) if paths else None
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

        count = sum(len(group.get("rules", [])) for group in document.get("groups", []))
        if before is not None and document == before and paths[0] == path:
            return {"hostname": _hw_host(path), "rules": count, "changed": False}
        if os.path.isfile(path):
            self._keep_copy(path)
        files.write_yaml(path, document)
        LOGGER.info("hardware rules written for %s at %s", node, path)
        return {"hostname": _hw_host(path), "rules": count, "changed": True}

    def _keep_copy(self, path: str) -> str:
        """
        A copy of a rule file under backup/ before it is overwritten, as Luna keeps one.
        """
        backup = os.path.join(self._rules_dir(), "backup")
        os.makedirs(backup, exist_ok=True)
        kept = os.path.join(backup, f"{os.path.basename(path)}.{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, kept)
        return kept

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

    def rename_hw_rules(self, node: str, body: dict = None) -> dict:
        """
        The rule files of a node that has been renamed, carried to its new name. The
        previous name has to be given: nothing else can find the files it was written
        under, because Luna no longer holds it.
        """
        was = str((body or {}).get("was") or "").strip()
        if not was:
            raise BadRequest("was names what the node was called before it was renamed")
        return self.repair_hw_rules(node, was=was)

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
        query = (f'count({HW_METRIC}{{class=~"{HW_CLASSES}", hostname="{host}", '
                 f'description!~"Virt|virt|USB", product!~"Virt.*"}}) '
                 f'by ({",".join(COMPONENT_LABELS)})')
        answer = self._query(query)
        result = ((answer.json() or {}).get("data") or {}).get("result") or []
        return [dict(item.get("metric") or {}) for item in result]

    def _query(self, query: str):
        """
        One instant query. TrinityX serves Prometheus over TLS; a site that does not is
        found on the first call and remembered, so a scheme mismatch never stops the rules.
        """
        configured = self._config().get("prometheus_url", DEFAULT_CONFIG["prometheus_url"])
        url = self.prometheus_url or configured
        for attempt in (1, 2):
            try:
                answer = requests.get(f"{url}/api/v1/query", params={"query": query},
                                      timeout=20, verify=False)
            except requests.exceptions.SSLError as error:
                if attempt == 1:
                    url = _other_scheme(url)
                    continue
                raise BackendFailure(f"Prometheus is not answering: {error}") from None
            except requests.RequestException as error:
                raise BackendFailure(f"Prometheus is not answering: {error}") from None
            if answer.status_code == 400 and "HTTPS server" in answer.text and attempt == 1:
                url = _other_scheme(url)
                continue
            break
        if answer.status_code >= 400:
            raise BackendFailure(f"Prometheus answered {answer.status_code} at {url}")
        if url != (self.prometheus_url or configured):
            LOGGER.warning("Prometheus answers on %s, not %s; using that from now on",
                           url, configured)
            self.prometheus_url = url
        return answer

    # --------------------------------------------------------------- exporters

    def list_exporters(self, caller=None) -> dict:
        """
        What is scraped and where: the baseline, what this site has defined on top of it, and
        every scope that deviates.

        The baseline's digest is here because that file is NOT replicated - only the deviations
        are - so two controllers can disagree about it while the database looks converged. One
        call per controller and the two digests settle it.
        """
        baseline, definitions, scopes = self._exporter_inputs()
        path = self._config().get("exporters_file") or DEFAULT_CONFIG["exporters_file"]
        read = exporters.NODES in exporters.factory(path)
        return {"factory": {"path": path, "digest": exporters.digest(path),
                            "read": read,
                            "reason": "" if read else
                                      "the file is absent, unreadable or names no nodes "
                                      "section; the exporters built into the daemon are in use",
                            "nodes": baseline.get(exporters.NODES) or {},
                            "controllers": baseline.get(exporters.CONTROLLERS) or {},
                            "hosts": sorted(baseline.get(exporters.HOSTS) or {})},
                "defined": definitions, "applied": scopes}

    def _state(self, kind: str, name: str, body: dict) -> dict:
        """
        Write one exporter document and wake the render that reads it.

        An empty body is how this plugin removes one. A row is never deleted: the table
        replicates, and the peer re-adds a row that is missing here at the next pass, so a
        delete would come back within the sweep interval.
        """
        # The name becomes a declaration's name, which the column holds to 255 characters: a
        # longer one is a caller's mistake and belongs in a refusal rather than a database error.
        if not name or len(name) > 255:
            raise BadRequest("a name is needed, of at most 255 characters")
        stored = declarations.write(PLUGIN, kind, name, body or {})
        # Qualified with the plugin, because a render's bare manifest input is compared that
        # way: a bare kind here wakes nothing, and the change then waits for a node to move.
        renders().notify(f"{PLUGIN}.{kind}")
        return stored

    def set_exporter(self, exporter: str, body: dict = None) -> dict:
        """
        Define an exporter the baseline does not have, or change one that it does.
        """
        if exporter in RESERVED:
            # /exporters/applied with the scope left off lands here, and would otherwise
            # define an exporter named after the path word and scrape it on every node.
            raise BadRequest(f"{exporter} is part of the path, not an exporter name")
        sent = {k: v for k, v in (body or {}).items() if v is not None}
        # Checked here as the file's reader checks it, so a shape the render cannot use is a
        # refusal with a reason rather than a row that breaks service discovery later.
        if exporters.checked(exporter, sent, "the request", strict=True) is None:
            raise BadRequest("an exporter is a mapping of fields")
        held = declarations.read(PLUGIN, exporters.DEFINITION, exporter, {})
        # Only what was actually sent: the binder fills every unset optional with None, and
        # writing those would blank the port this exporter is scraped on.
        held.update(sent)
        return {"exporter": exporter,
                "fields": self._state(exporters.DEFINITION, exporter, held)}

    def remove_exporter(self, exporter: str) -> dict:
        """
        Forget what this site said about an exporter, leaving the baseline to speak.
        """
        return {"exporter": exporter,
                "fields": self._state(exporters.DEFINITION, exporter, {})}

    def set_applied(self, where: str, body: dict = None) -> dict:
        """
        Where an exporter applies, and what it looks like there: a mapping of exporter name to
        false to switch it off, true to take the baseline, or fields to change.
        """
        sent = (body or {}).get("exporters")
        if not isinstance(sent, dict):
            raise BadRequest("exporters must be a mapping of exporter name to false, true, "
                             "or the fields to change")
        for name, deviation in sent.items():
            if isinstance(deviation, dict) and exporters.checked(
                    name, deviation, "the request", strict=True) is None:
                raise BadRequest(f"the deviation for {name} is not a mapping of fields")
        held = declarations.read(PLUGIN, exporters.SCOPE, where, {})
        held.update(sent)
        return {"applied": where, "exporters": self._state(exporters.SCOPE, where, held)}

    def remove_applied(self, where: str) -> dict:
        """
        Drop every deviation held for one scope.
        """
        return {"applied": where, "exporters": self._state(exporters.SCOPE, where, {})}

    def show_applied(self, where: str) -> dict:
        """
        What Prometheus will scrape there once everything has been resolved, so an operator can
        look before the render does it for real.
        """
        baseline, definitions, scopes = self._exporter_inputs()
        if where in (exporters.EVERY_CONTROLLER,) or where in (baseline.get(exporters.HOSTS) or {}):
            resolved = exporters.for_controller(where, baseline, definitions, scopes)
        elif where.startswith("node:"):
            name = where.split(":", 1)[1]
            record = luna().node(name) or {}
            resolved = exporters.for_node(name, record.get("group") or "",
                                          baseline, definitions, scopes)
        elif where.startswith("group:"):
            resolved = exporters.for_node("", where.split(":", 1)[1],
                                          baseline, definitions, scopes)
        else:
            resolved = exporters.for_node("", "", baseline, definitions, scopes)
        return {"applied": where, "exporters": resolved}

    def _exporter_inputs(self) -> tuple:
        """
        The baseline, whatever the site has defined on top of it, and where each one applies.

        No file means an installation that predates it, so the five this daemon shipped with are
        the baseline and the render is what it was yesterday.
        """
        path = self._config().get("exporters_file") or DEFAULT_CONFIG["exporters_file"]
        baseline = exporters.factory(path)
        if exporters.NODES not in baseline:
            baseline = dict(baseline)
            baseline[exporters.NODES] = {name: {"port": port} for name, port in EXPORTERS.items()}
        definitions = {row["name"]: row["body"]
                       for row in declarations.listing(PLUGIN, exporters.DEFINITION)}
        scopes = {row["name"]: row["body"]
                  for row in declarations.listing(PLUGIN, exporters.SCOPE)}
        return baseline, definitions, scopes

    def _target(self, address: str, exporter: str, fields: dict, labels: dict) -> dict:
        """
        One service discovery entry. The labels are the compatibility surface: dashboards and
        alert rules group by them, so a change here is a change to what a customer sees.

        A scrape interval reaches Prometheus only as __scrape_interval__ on the target; set as
        a plain field it would be stored, echoed back and silently ignored.
        """
        held = fields.get("labels")
        carried = dict(held) if isinstance(held, dict) else {}
        for field, label in (("interval", "__scrape_interval__"),
                             ("timeout", "__scrape_timeout__")):
            if fields.get(field):
                carried[label] = str(fields[field])
        # Last, so a site cannot rename the labels dashboards and alert rules group by.
        carried.update(labels)
        return {"targets": [f"{address}:{fields['port']}"], "labels": carried}

    # --------------------------------------------------------------- renders

    def render_targets(self) -> list:
        """
        The service discovery document, built once per change, not per poll.
        """
        baseline, definitions, scopes = self._exporter_inputs()
        nodes = luna().nodes()
        targets = []
        for name, node in nodes.items():
            address = _provision_address(node) or node.get("hostname") or name
            labels = {"hostname": node.get("hostname") or name}
            if node.get("group"):
                labels["luna_group"] = node["group"]
            for exporter, fields in exporters.for_node(name, node.get("group") or "",
                                                       baseline, definitions, scopes).items():
                targets.append(self._target(address, exporter, fields,
                                            {"exporter": exporter, **labels}))
        hosts = baseline.get(exporters.HOSTS) or {}
        for host, where in hosts.items():
            address = (where or {}).get("address") or host
            for exporter, fields in exporters.for_controller(host, baseline,
                                                             definitions, scopes).items():
                targets.append(self._target(address, exporter, fields,
                                            {"exporter": exporter, "luna_group": "controller",
                                             "hostname": host}))
        LOGGER.info("rendered %d target(s) for %d node(s) and %d controller(s)",
                    len(targets), len(nodes), len(hosts))
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

    def _reload_now(self) -> tuple:
        """
        Tell Prometheus to re-read what we wrote, once.
        """
        command = self._config().get("reload_command", DEFAULT_CONFIG["reload_command"])
        result = shell.run(command.split(), timeout=30)
        if not result.ok:
            LOGGER.error("reload failed: %s", result.err or result.out)
        return result.ok, result.err or result.out

    def reload(self) -> bool:
        """
        A reload that callers at the same moment share; each gets the outcome of one that
        started after its own write.
        """
        return RELOADS.run(self._reload_now)[0]

    def reload_or_fail(self, what: str) -> None:
        """
        Reload, and say so in the answer when Prometheus did not take it.
        """
        ok, message = RELOADS.run(self._reload_now)
        if not ok:
            raise BackendFailure(f"{what}, but Prometheus did not reload: {message}")


class SharedReload:
    """
    One reload serves every caller waiting for it. A caller takes a ticket after its write;
    a reload covers every ticket taken before it started, so nobody is answered by a
    reload that ran before their file was on disk.
    """

    def __init__(self) -> None:
        """
        No ticket taken, no reload run.
        """
        self.condition = threading.Condition()
        self.issued = 0
        self.covered = 0
        self.running = False
        self.outcome: tuple = (True, "")
        self.runs = 0

    def run(self, reload) -> tuple:
        """
        Wait for, or run, a reload that covers this caller, and answer its outcome.
        """
        with self.condition:
            self.issued += 1
            ticket = self.issued
            while self.covered < ticket:
                if self.running:
                    self.condition.wait()
                    continue
                self.running = True
                covers = self.issued
                self.condition.release()
                try:
                    outcome = reload()
                except Exception as error:
                    outcome = (False, str(error))
                finally:
                    self.condition.acquire()
                self.running = False
                self.covered = covers
                self.outcome = outcome
                self.runs += 1
                self.condition.notify_all()
            return self.outcome


RELOADS: SharedReload = SharedReload()


def _other_scheme(url: str) -> str:
    """
    The same address over the other scheme.
    """
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return "https://" + url.split("://", 1)[-1]


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
