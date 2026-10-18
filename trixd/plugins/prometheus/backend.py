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

import copy
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
from trixd.common.errors import (BackendFailure, BadRequest, NotFound, ServiceUnavailable,
                                 TrixdError)
from trixd.core.render.engine import current as renders
from trixd.plugins.luna.client import current as luna
from trixd.plugins.prometheus import exporters
from trixd.utils import declarations, files
from trixd.utils import shell

LOGGER = trixlog.get("prometheus")

urllib3.disable_warnings()

PLUGIN: str = "prometheus"

# What Prometheus accepts as a label name; anything else makes it refuse the whole document.
# The literal words in this plugin's own route paths under /exporters, which an exporter may
# therefore not be called: the one-segment route catches them when a scope is left off.
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
# How many copies of one node's rule file are kept under backup/. One is taken per write and
# none was ever discarded, so the directory grows with every boot of every node until it fills
# the filesystem; what a rollback reaches for is the last few.
KEEP_COPIES: int = 10

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
# A selector is read matcher by matcher rather than matched as a whole, because a brace and a
# comma are ordinary characters inside a quoted value: {3,} in a matcher's regex is a bounded
# repetition, not the end of the selector.
# The separators before one matcher and its name and operator, in one step: the scan pays
# for this per matcher, over every rule of every node's file.
MATCHER_HEAD = re.compile(r"[\s,]*(\w+)\s*(?:=~|!~|!=|=)\s*")
# What stands between two matchers, and the space around them.
SEPARATORS = re.compile(r"[\s,]*")
# A quoted value, by its opening quote. PromQL takes all three; a backtick string is raw, so
# a backslash in one is a character rather than an escape.
STRINGS: dict = {'"': re.compile(r'"(?:[^"\\]|\\.)*"'),
                 "'": re.compile(r"'(?:[^'\\]|\\.)*'"),
                 "`": re.compile(r"`[^`]*`")}
# Where the next selector or string can begin, so the scan steps to it rather than through
# every character before it.
OPENINGS = re.compile("[{" + re.escape("".join(STRINGS)) + "]")
# Prometheus's own checker, which ships beside it. Where it lands depends on how Prometheus
# was installed - the package puts it in /usr/bin, a tarball in /usr/local/bin - so both are
# looked at before PATH. A cluster without it loses the check, not the write.
PROMTOOL: tuple = ("/usr/bin/promtool", "/usr/local/bin/promtool")
# Said once when promtool is not installed, rather than on every write.
_said_no_promtool: bool = False


def _promtool_refuses(paths: list) -> str:
    """
    What promtool says is wrong with a set of rule files, or empty when it takes them all.

    One call for the whole set: it is a fork of a Go binary, around 35 ms before it reads
    anything, so one per file is a cost multiplied by the number of nodes.

    Where promtool is not installed nothing is checked and it is said once. It does not ship
    everywhere, and a cluster without it should lose the check rather than the reload - the
    in-process check at write time is what holds in that case, and it covers the values that
    come off a node, which is the half that varies.
    """
    global _said_no_promtool
    if not paths:
        return ""
    tool = next((candidate for candidate in PROMTOOL if os.path.isfile(candidate)),
                shutil.which("promtool"))
    if not tool:
        if not _said_no_promtool:
            _said_no_promtool = True
            LOGGER.warning("promtool is not installed, so the rule files are not checked "
                           "before a reload; what is written is still checked in process")
        return ""
    result = shell.run([tool, "check", "rules", *paths], check=False)
    if result.ok:
        return ""
    return (result.err or result.out or "promtool refused the rules").strip()[:500]


def _past_string(text: str, start: int) -> int:
    """
    Where the quoted value that opens here ends, or -1 when nothing closes it.
    """
    found = STRINGS[text[start]].match(text, start)
    return found.end() if found else -1


def _past_matcher(text: str, start: int) -> int:
    """
    Where the label matcher that starts here ends, or -1 when this is not one.

    Its value is read as a string rather than up to the next comma or brace, because both
    are ordinary inside one and a matcher's regex is where they turn up.
    """
    head = MATCHER_HEAD.match(text, start)
    if head is None or head.end() >= len(text) or text[head.end()] not in STRINGS:
        return -1
    return _past_string(text, head.end())


def _selector(text: str, start: int) -> tuple:
    """
    What no matcher accounts for inside the selector opened just before start, and where
    that selector ended.
    """
    leftover, position = [], start
    while position < len(text):
        ended = _past_matcher(text, position)
        if ended >= 0:
            position = ended
            continue
        position = SEPARATORS.match(text, position).end()
        if position >= len(text):
            break
        if text[position] == "}":
            return ", ".join(leftover), position + 1
        # Not a matcher, so this reads to the next separator as text nothing accounts for:
        # a value that closed its quote early leaves the rest of itself here.
        run = position
        while position < len(text) and text[position] not in ",}":
            position += 1
        leftover.append(text[run:position].strip())
    return ", ".join(leftover), len(text)


def unaccounted(expr: str) -> str:
    """
    What is left inside a selector once its matchers have been read, which is nothing when
    every value is escaped.

    A value that closes its quote early leaves the rest of itself behind as text no matcher
    accounts for, and that is exactly what Prometheus refuses. Counting the matchers does
    not catch it: the pattern re-synchronises, so a broken selector still parses as the
    right number of them, each truncated.

    The braces are found by scanning, because a brace is ordinary inside a quoted value -
    a bounded repetition in a matcher's regex, a vendor's product string, or a string
    argument to label_replace outside any selector at all.
    """
    text = expr or ""
    position = 0
    while True:
        opening = OPENINGS.search(text, position)
        if opening is None:
            return ""
        position = opening.start()
        if text[position] != "{":
            # A string outside any selector, whose braces and commas are no more structural
            # than those inside a value. Unterminated, nothing after it can be read.
            position = _past_string(text, position)
            if position < 0:
                return ""
            continue
        left, position = _selector(text, position + 1)
        if left:
            return left[:120]


def _refuse_unparsable(document, what: str) -> None:
    """
    Refuse a document whose selectors do not hold together, before anything is written.
    """
    for group in (document or {}).get("groups") or []:
        for rule in group.get("rules") or []:
            left = unaccounted(str(rule.get("expr") or ""))
            if left:
                raise BackendFailure(
                    f"{what} will not be written: a selector carries {left!r} outside any "
                    f"label matcher, which is what a value that closes its quote early "
                    f"leaves behind")


def quoted(value) -> str:
    """
    One label value as PromQL takes it, inside the double quotes the caller writes.

    Hardware strings come off the node and carry whatever the vendor wrote: an inch mark in
    a disk's product string is ordinary. Interpolated raw it closes the quote early, and
    Prometheus then refuses the whole rule file, aborts the reload and goes on serving its
    previous configuration - so every later rule change is silently not applied while this
    daemon reports success. MATCHER above already reads escapes back, so the reader was
    always expecting this; only the writers were not doing it.
    """
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


class Backend:
    """
    One writer of the Prometheus configuration.
    """

    def __init__(self) -> None:
        """
        No hardware file seen yet, and nothing written waiting to be checked.
        """
        # Rule files written since the last reload, which is what promtool is given. Held
        # under a lock: the writers are request threads and the reload runs on whichever of
        # them took the ticket.
        self.pending_check: set = set()
        self.pending_lock = threading.Lock()
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
            # Refused before it is stored, not when the files are written: the declaration
            # table replicates, so a document the writer refuses would reach the peer as
            # well and leave every later render raising here instead of reaching the
            # hardware reconcile below it - on both controllers, until someone sends this
            # again.
            _refuse_unparsable(body, "the rules sent")
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
        if written["changed"] or self._check_outstanding(written["hostname"]):
            self.reload_or_fail(f"rules written for {node}")
        return written

    def set_hw_rules(self, body: dict = None) -> dict:
        """
        Several nodes at once, as alertx's save-nodes asks: regenerate (force), or set
        whether a node's alerts count for health (nhc) and fire at all (disabled).
        """
        entries = [(entry or {}, str((entry or {}).get("node") or ""))
                   for entry in (body or {}).get("nodes") or []]
        # Every name is read before the first write. Refused from inside the loop below, the
        # entries ahead of the bad one are already written and the reload at the end never
        # runs: files Prometheus has not been told about, under an answer saying the batch
        # was refused.
        for _, node in entries:
            if not NODE_NAME.match(node):
                raise BadRequest(f"{node or 'an entry'} does not name a node")
        results = []
        for entry, node in entries:
            if luna().node(node.split(".")[0]) is None:
                results.append({"node": node, "ok": False, "message": "not a node Luna knows"})
                continue
            try:
                results.append({"node": node, "ok": True, **self._write_hw_rules(node, entry)})
            except TrixdError as error:
                results.append({"node": node, "ok": False, "message": error.message})
        # One reload for the lot, not one per node, and none when nothing changed.
        changed = [item["node"] for item in results if item.get("changed")
                   or self._check_outstanding(item.get("hostname") or "")]
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
            # A file under the name asked for is the whole answer. Where there is none, the
            # node is resolved as every other route resolves it - by the names Luna holds
            # for it - so a node whose name and hostname differ in their short form is no
            # longer listed as holding nothing while its rules sit on disk and the route
            # for one node serves them. Only a name asked for and not on disk costs that
            # lookup: the estate pass reads the file names and asks Luna nothing.
            path = held.get(host)
            if path is None:
                found = self._hw_paths(host)
                path = found[0] if found else ""
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
        stale = []
        if paths and not body.get("force"):
            # Copied rather than parsed a second time. Two independent documents are needed
            # - the one below is mutated in place and compared against `before` - but a
            # parse of one node's file costs an order of magnitude more than the copy, and
            # this is paid once per node on every boot.
            path, document = paths[0], copy.deepcopy(before)
        else:
            host = self._host_of(node)
            path = os.path.join(self._rules_dir(), HW_RULES.format(host=host))
            document = self._generate_hw_rules(host)
            # Written under the name Luna resolves now, which is not the name the files it
            # matched carry. Left where they are, Prometheus loads the whole glob and both
            # fire - the one left behind still selecting the group the node has left.
            stale = [found for found in paths if found != path]

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
        self._write_rules(path, document)
        # After the write, never before it: a generate that raises must not leave the node
        # with no rule file at all.
        for found in stale:
            self._retire(found)
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
        _prune_copies(backup, os.path.basename(path))
        return kept

    def _write_rules(self, path: str, document) -> str:
        """
        Write a rule file, refusing one whose selectors do not hold together.

        Every rule file this plugin writes goes through here, and the check is in-process on
        purpose. This runs once per node: a fork of promtool and a second durable write
        here would be ~35 ms and an fsync each, which is nothing at four nodes and is a
        thousand of each in a boot storm, on the request threads and inside the reconcile
        pass that has to return to idle. What promtool adds beyond this is the same for
        every node, so it is paid once per reload instead - see _reload_now.
        """
        _refuse_unparsable(document, f"the rules for {os.path.basename(path)}")
        written = files.write_yaml(path, document)
        with self.pending_lock:
            self.pending_check.add(path)
        return written

    def _check_outstanding(self, host: str) -> bool:
        """
        Whether this host's rule file is still waiting for promtool to take it.

        A file promtool refused is on disk and unchanged, so the writes that follow it find
        nothing to do and ask for no reload: without this the node's own retry answers that
        its rules are in place while Prometheus is refusing them.
        """
        path = os.path.join(self._rules_dir(), HW_RULES.format(host=host))
        with self.pending_lock:
            return path in self.pending_check

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
        # The file under the name this node answers to now. _hw_paths does not return it
        # when a file under the name asked for exists, because an exact match is the whole
        # answer there - and here the name asked for is the node's while the file is written
        # under its hostname. Without this, a repair discards the older file as superseded
        # and never looks at the live one, so a relabel that was due silently does not
        # happen and the node's alerts go on matching a group it has left.
        if os.path.isfile(target) and target not in paths:
            paths.append(target)
        moved, rewritten, superseded = [], [], []
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
            if path != target and os.path.isfile(target):
                # The node already has a file under the name it answers to now, so this one
                # is what it was called before. Writing it to the target promoted the old
                # name's contents over whatever has been written since - which is what the
                # reconcile was taught not to do, and this path, which the rename hook takes
                # automatically, was still doing.
                _discard(path)
                superseded.append(os.path.basename(path))
                continue
            self._write_rules(target, document)
            if path != target:
                _discard(path)
                moved.append(os.path.basename(path))
            if changed:
                rewritten.append(os.path.basename(target))
        answer = {"node": node, "hostname": host, "moved": moved, "rewritten": rewritten,
                  **({"superseded": superseded} if superseded else {})}
        if moved or rewritten or superseded:
            answer["reloaded"] = self.reload_reported()
            LOGGER.info("hardware rules for %s: moved %s, rewritten %s, superseded %s",
                        node, moved, rewritten, superseded)
        return answer

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
        answer = {"node": node, "retired": retired}
        if retired:
            answer["reloaded"] = self.reload_reported()
        return answer

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
        _prune_copies(backup, os.path.basename(path))
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
        The rule files that name this node: the names Luna holds for it, or failing Luna,
        what the filenames say.

        A node is unique in Luna, so its name and its hostname address its files directly
        and the domain does not matter. A filename cannot say that much: a node whose name
        and hostname differ in their short form answers to neither by file, so its rules
        were a 404 while they sat on disk, its retire found nothing and left a node that
        was gone still alerting, and the import it asks for itself after boot regenerated,
        backed up and reloaded on every boot.

        The search by filename is what is left when Luna cannot answer - a node that has
        been deleted, which is exactly when the retire and the delete hook have to find its
        files. There a short name may name two files, because two nodes whose hostnames
        differ only past the first dot share one, and serving, rewriting or retiring either
        of them on the other's behalf is the mistake the reconcile was taught not to make.
        Two is refused loudly and answers nothing, so a caller gets "no rules held for this
        node" rather than another node's, and whoever reads the log sees which two files.
        """
        directory = self._rules_dir()
        try:
            record = luna().node(node.split(".")[0])
        except ServiceUnavailable as error:
            # A retire and the delete hook have to find the files with Luna unreachable, and
            # that is the same answer as for a node Luna no longer holds. Said, not swallowed.
            LOGGER.warning("Luna could not say which files are %s's (%s), so they are taken "
                           "from the file names instead", node, error.message)
            record = None
        if record is not None:
            # The hostname first: that is the name a file is written under, and the node's
            # own name is what a file written before a rename still carries.
            names = dict.fromkeys([record.get("hostname") or node, node.split(".")[0]])
            return [path for path in (os.path.join(directory, HW_RULES.format(host=name))
                                      for name in names) if os.path.isfile(path)]
        pattern = os.path.join(directory, HW_RULES.format(host="*"))
        found = sorted(path for path in glob.glob(pattern)
                       if _names_node(_hw_host(path), node))
        if len(found) > 1:
            LOGGER.error("%s could be any of %s, which share its short name, so none of "
                         "them is taken for it: name the node as its file does", node,
                         [os.path.basename(path) for path in found])
            return []
        return found

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
            selector = ",".join(f'{key}="{quoted(component[key])}"'
                                for key in labels if component.get(key))
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
        baseline, definitions, scopes, read = self._exporter_inputs(say=True)
        path = self._config().get("exporters_file") or DEFAULT_CONFIG["exporters_file"]
        return {"factory": {"path": path, "digest": exporters.digest(path),
                            "read": read,
                            "reason": "" if read else
                                      "no nodes section was read from this file - it is "
                                      "absent, unreadable, or names none; the exporters built "
                                      "into the daemon are in use",
                            "ignored": list(exporters.IGNORED),
                            "nodes": baseline.get(exporters.NODES) or {},
                            "controllers": baseline.get(exporters.CONTROLLERS) or {},
                            "hosts": sorted(baseline.get(exporters.HOSTS) or {})},
                "defined": definitions, "applied": scopes}

    def _state(self, kind: str, name: str, body: dict) -> dict:
        """
        Write one exporter document and wake the render that reads it.

        Removal is `forget`, which marks the row rather than deleting it: the peer re-adds a
        row that is missing here, so a real delete comes back within the sweep interval.
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

    def _forget(self, kind: str, name: str) -> bool:
        """
        Mark one document deleted and wake the render that read it.
        """
        # The same bound the write path applies: a name this long is a caller's mistake either
        # way, and answering 200 to it only because this is the delete would hide that.
        if not name or len(name) > 255:
            raise BadRequest("a name is needed, of at most 255 characters")
        gone = declarations.forget(PLUGIN, kind, name)
        if gone:
            renders().notify(f"{PLUGIN}.{kind}")
        return gone

    def remove_exporter(self, exporter: str) -> dict:
        """
        Forget what this site said about an exporter, leaving the baseline to speak.
        """
        if exporter in RESERVED:
            # DELETE /exporters/applied with the scope left off lands here, and would answer
            # 200 while the scope's deviations stay exactly where they were.
            raise BadRequest(f"{exporter} is part of the path, not an exporter name")
        return {"exporter": exporter, "forgotten": self._forget(exporters.DEFINITION, exporter),
                "fields": {}}

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
        return {"applied": where, "forgotten": self._forget(exporters.SCOPE, where),
                "exporters": {}}

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

    def _exporter_inputs(self, say: bool = False) -> tuple:
        """
        The baseline, whatever the site has defined on top of it, and where each one applies.

        No file means an installation that predates it, so the five this daemon shipped with are
        the baseline and the render is what it was yesterday.
        """
        path = self._config().get("exporters_file") or DEFAULT_CONFIG["exporters_file"]
        baseline = exporters.factory(path)
        read = exporters.NODES in baseline
        if not read:
            baseline = dict(baseline)
            baseline[exporters.NODES] = {name: {"port": port} for name, port in EXPORTERS.items()}
        definitions = {row["name"]: row["body"]
                       for row in declarations.listing(PLUGIN, exporters.DEFINITION)}
        scopes = {row["name"]: row["body"]
                  for row in declarations.listing(PLUGIN, exporters.SCOPE)}
        return (baseline, definitions, scopes, read) if say else (baseline, definitions, scopes)

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
        if not nodes:
            # As the other renders do: an empty answer is not a cluster with no nodes, and a
            # document without them empties service discovery for every node at once. Failing
            # here keeps the last document being served.
            raise ServiceUnavailable("no nodes yet, so the targets already served are kept")
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
        answer = {"written": written, **reconciled}
        if written or any(reconciled[kind] for kind in ("repaired", "retired", "superseded")):
            answer["reloaded"] = self.reload_reported()
        return answer

    def _reconcile_hw_rules(self, nodes: dict) -> dict:
        """
        Every hardware file names a node Luna has, under its current host and group, or it
        is retired. An empty node list is left alone, so nothing goes on Luna saying nothing.
        """
        repaired, retired, superseded = [], [], []
        if not nodes:
            return {"repaired": repaired, "retired": retired, "superseded": superseded}
        # Which node a file's host name belongs to, and the names that cannot say. Two
        # nodes whose names differ only past the first dot share a short name, and taking
        # the first of them relabelled one node's file to the other and discarded the
        # loser's - two genuinely different nodes merged onto one file.
        by_host, ambiguous = {}, set()
        for name, record in nodes.items():
            for key in (name.split(".")[0], record.get("hostname") or name):
                if by_host.get(key, name) != name:
                    ambiguous.add(key)
                by_host.setdefault(key, name)
        # A name more than one node answers to is dropped rather than left pointing at
        # whichever node was seen first: the lookup below then finds nothing, which is the
        # honest answer, instead of a guess that reads like a fact.
        for key in ambiguous:
            by_host.pop(key, None)
        # Read once: the setting lives in the database, and this loop runs per node.
        directory = self._rules_dir()
        for path in sorted(glob.glob(os.path.join(directory, HW_RULES.format(host="*")))):
            host = _hw_host(path)
            name = by_host.get(host) or by_host.get(host.split(".")[0])
            if name is None and {host, host.split(".")[0]} & ambiguous:
                # Whose file this is cannot be told from its name, and relabelling it to
                # either node would move one node's rules onto another. Left exactly as it
                # is: a wrong answer here is worse than an old file.
                LOGGER.error("%s names %s, which more than one node answers to, so it is "
                             "left alone rather than relabelled to one of them",
                             os.path.basename(path), host)
                continue
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
            if path != target and os.path.isfile(target):
                # The node already has a file under the host name it answers to now, so this
                # one is what it was called before. Reading it and writing it to the target
                # promoted the name it had over whatever has been written since the rename,
                # and the stamp that write set then made the loop skip the real file.
                _discard(path)
                self.seen.pop(path, None)
                superseded.append(os.path.basename(path))
                continue
            try:
                document = _read_yaml(path)
            except FileNotFoundError:
                continue
            changed = _relabel_document(document, want)
            if not changed and path == target:
                self.seen[path] = _stamp(path, want)
                continue
            self._write_rules(target, document)
            self.seen[target] = _stamp(target, want)
            if path != target:
                _discard(path)
                self.seen.pop(path, None)
            repaired.append(os.path.basename(target))
        if repaired or retired or superseded:
            LOGGER.info("hardware rules reconciled: repaired %s, retired %s, superseded %s",
                        repaired, retired, superseded)
        return {"repaired": repaired, "retired": retired, "superseded": superseded}

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
            self._write_rules(path, {"groups": subset})
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

        Prometheus's own checker runs first, in one call, over the files written since the
        last reload rather than over the whole directory. Measured on a thousand-node set:
        one call over every file is 3.2 s and 69 MB, which is not a thing to put on a
        caller's path, and it is not needed - Prometheus re-reads them all itself, and only
        what we just changed can have become bad. In steady state that is a file or two.

        Here rather than per write because a fork is ~35 ms before promtool reads anything,
        so one per node is a cost multiplied by the number of nodes; reloads are already
        shared between callers, so a boot storm pays for a handful of these.

        Refused, nothing is sent: Prometheus would read the files itself, refuse them, abort
        and go on serving its previous configuration - the same outcome, with nobody told.
        """
        with self.pending_lock:
            changed, self.pending_check = sorted(self.pending_check), set()
        on_disk = [path for path in changed if os.path.isfile(path)]
        refused = _promtool_refuses(on_disk)
        if refused:
            # Still on disk and still refused, so the record of them outlives the refusal.
            # Drained here, the next reload for any other node checked only its own file and
            # succeeded, Prometheus went on refusing the directory, and nothing named the bad
            # file again - while the write that caused it reported success on its next try.
            with self.pending_lock:
                self.pending_check.update(on_disk)
            LOGGER.error("not reloading: promtool refuses what is on disk: %s", refused)
            return False, f"promtool refuses the rules on disk: {refused}"
        command = self._config().get("reload_command", DEFAULT_CONFIG["reload_command"])
        result = shell.run(command.split(), timeout=30)
        if not result.ok:
            LOGGER.error("reload failed: %s", result.err or result.out)
        return result.ok, result.err or result.out

    def reload_reported(self) -> dict:
        """
        Reload, and say in the answer whether Prometheus took it.

        These paths have no caller waiting on the reload, and the files are written either
        way - a refusal must not turn a completed write into a failure. But dropping the
        outcome left the one case that matters invisible: Prometheus refused the files, kept
        its previous configuration, and the answer said the rules were in place.
        """
        ok, message = RELOADS.run(self._reload_now)
        if ok:
            return {"ok": True}
        return {"ok": False, "message": str(message)[:500]}

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
            return f'{key}="{quoted(values[key])}"'
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
            # Every node label, not the hostname alone: the pass that fixes the expression
            # is the only one that will look, so a label left behind is left for good.
            labels = rule.get("labels") or {}
            for key in NODE_LABELS:
                want = values.get(key)
                if labels.get(key) and want and labels[key] != want:
                    labels[key] = want
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


def _prune_copies(backup: str, basename: str) -> list:
    """
    Discard all but the newest KEEP_COPIES copies of one rule file, and say what went.

    Keyed on the whole file name and the dot that follows it, so one node's copies are never
    counted as another's where a name is a prefix of a longer one. Ordered by the stamp in the
    name rather than by mtime: a copy carries the mtime of the file it was taken from, which
    says when the rules last changed and not when the copy was made.
    """
    kept = sorted(name for name in os.listdir(backup) if name.startswith(f"{basename}."))
    older = kept[:-KEEP_COPIES]
    for name in older:
        _discard(os.path.join(backup, name))
    return older


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
