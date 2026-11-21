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
The node, partition and gres files: our managed block built here, from Luna's nodes and the
presets the `Defaults` block of each file carries.

Luna's own config hooks and the OOD slurm app write these same files, so what is written
here has to be what they write, down to the byte: their markers, their blank lines, their
fold, and the administrator's own text around the block returned as it was found.
tests/unit/golden/ holds what they produce for the cases in tests/unit/golden_cases.py and
the suite holds this against it.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import os
import re

from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure
from trixd.plugins.slurm.hostnames import fold_nodeset

LOGGER = trixlog.get("slurm")

# The markers every writer of these files uses. Three spaces before the end marker's hashes,
# and a block is found by them alone - trixd/utils/files.py writes a pair of its own, which
# these files do not carry and which would land a second block beside this one.
BLOCK_START: str = "#### {manager} Managed block start ####"
BLOCK_END: str = "#### {manager} Managed block end   ####"
_START = re.compile(r"^#### (?P<manager>.+) Managed block start ####")
_END = re.compile(r"^#### (?P<manager>.+) Managed block end   ####")

# The block each file keeps its presets in, written by the administrator or by the Slurm app.
PRESETS: str = "Defaults"
# A preset line's settings: the first is the key the node or group is looked up by, the rest
# are what the node's line carries. Anything after a `#` is a note about the preset.
_SETTING = re.compile(r"(\S+)=(\S+)")


def _blocks(text: str) -> list:
    """
    The file as its writers see it: a managed block as [manager, lines], everything else as
    [None, lines]. An end marker closes whatever block is open, whoever's name it carries.
    """
    blocks: list = []
    current: list = None
    for line in text.splitlines():
        start, end = _START.match(line), _END.match(line)
        if start:
            if current:
                blocks.append(current)
            current = [start.group("manager"), []]
        elif end:
            if current:
                blocks.append(current)
                current = None
        elif current:
            current[1].append(line)
        else:
            current = [None, [line]]
    if current:
        blocks.append(current)
    return blocks


def _dump(blocks: list) -> str:
    """
    The whole file back, each managed block between its own markers, and the one newline at
    the end that a file without one gains.
    """
    lines: list = []
    for manager, held in blocks:
        if manager is None:
            lines += held
            continue
        lines += [BLOCK_START.format(manager=manager), *held,
                  BLOCK_END.format(manager=manager)]
    return "\n".join(lines + [""]) if lines else ""


def block(text: str, manager: str) -> str:
    """
    What the manager's block holds, or None where the file carries no such block at all -
    which is the administrator having taken it out, not a file to put one back into.
    """
    for name, held in _blocks(text):
        if name == manager:
            return "\n".join(held)
    return None


def replace(text: str, manager: str, content: str) -> str:
    """
    The file with the manager's block holding `content`, or None where it has no such block.
    """
    blocks = _blocks(text)
    for found in blocks:
        if found[0] == manager:
            found[1] = content.splitlines()
            return _dump(blocks)
    return None


def presets(text: str, multiple: bool = False) -> dict:
    """
    The file's own presets, keyed by their first setting: `NodeName=node001` or
    `GroupGres=gpu` to the settings that follow it. Commented out is how they are written,
    so one leading `#` comes off; `multiple` keeps every line a key carries, as a node may
    take more than one gres line and only one of everything else.
    """
    found: dict = {}
    for line in (block(text, PRESETS) or "").splitlines():
        line = line[1:].lstrip() if line.startswith("#") else line
        settings = [f"{name}={value}" for name, value in _SETTING.findall(line.split("#")[0])]
        if not settings:
            continue
        key, carries = settings[0], " ".join(settings[1:])
        if multiple:
            found.setdefault(key, []).append(carries)
        else:
            found[key] = carries
    return found


def _groups(nodes: list) -> dict:
    """
    The nodes by group, the groups in the order they first appear: that order is the order
    of the NodeSet and PartitionName lines.
    """
    groups: dict = {}
    for one in nodes:
        groups.setdefault(one["group"], []).append(one["name"])
    return groups


def _lines(lines: list) -> str:
    """
    A block's lines, opening with the blank line these files carry under the start marker;
    a block with nothing in it leaves the two markers together.
    """
    return "\n" + "\n".join(lines) if lines else ""


def nodes_block(nodes: list, found: dict) -> str:
    """
    A NodeName line per node, carrying the first preset that names the node itself, a node
    of its group's name, its group's partition or its group; then the nodesets.
    """
    lines = []
    for one in nodes:
        for key in (f"NodeName={one['name']}", f"NodeName={one['group']}",
                    f"PartitionName={one['group']}", f"GroupName={one['group']}"):
            if key in found:
                lines.append(f"NodeName={one['name']} {found[key]} # GroupName={one['group']}")
                break
        else:
            lines.append(f"NodeName={one['name']} # GroupName={one['group']}")
    lines += ["", "# Nodesets"]
    lines += [f"NodeSet={group} Nodes={fold_nodeset(names)}"
              for group, names in _groups(nodes).items()]
    return _lines(lines)


def partitions_block(nodes: list, found: dict) -> str:
    """
    A PartitionName line per group, over the nodeset of that group's own name.
    """
    lines = []
    for group in _groups(nodes):
        key = f"PartitionName={group}"
        lines.append(f"PartitionName={group} Nodes={group} {found[key]}" if key in found
                     else f"PartitionName={group} Nodes={group}")
    return _lines(lines)


def gres_block(nodes: list, found: dict) -> str:
    """
    A line per node per gres preset, so a group whose preset is two lines names each of its
    nodes twice; a node no preset reaches is in no line at all.
    """
    lines = []
    for one in nodes:
        for key in (f"NodeName={one['name']}", f"NodeName={one['group']}",
                    f"GroupName={one['group']}", f"NodeGres={one['name']}",
                    f"GroupGres={one['group']}"):
            if key in found:
                lines += [f"NodeName={one['name']} {entry} # GroupName={one['group']}"
                          for entry in found[key]]
                break
    return _lines(lines)


# What each file holds, and whether a key there carries more than one line.
BLOCKS: dict = {"nodes": (nodes_block, False), "partitions": (partitions_block, False),
                "gres": (gres_block, True)}


def _put(path: str, text: str) -> None:
    """
    Write the file where it is, so its owner, group and mode stay: the six slurm files are
    root's of the cluster's admin group with group write, and the helper writes them by that
    group. A temporary file and a rename would hand them this process's own.
    """
    handle = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(handle, "w", encoding="utf-8") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())


def write_all(nodes: list, configs: dict, manager: str) -> list:
    """
    The managed block of each file in `configs`, built from `nodes` and that file's own
    presets. Every file is read and built before any is written, and a file carrying no
    block of ours stops the lot: the three are loaded together by one `scontrol
    reconfigure`, so two of them moving and one being the administrator's is a cluster
    configured half from each.
    """
    held, wanted = {}, {}
    for kind, path in configs.items():
        try:
            with open(path, "r", encoding="utf-8") as handle:
                held[kind] = handle.read()
        except OSError as error:
            raise BackendFailure(f"cannot read {path}: {error}") from None
    missing = sorted(configs[kind] for kind in configs if block(held[kind], manager) is None)
    if missing:
        raise BackendFailure(f"{', '.join(missing)} has no {manager} managed block, so the "
                             f"slurm files were left exactly as they are. Taking the block "
                             f"out is how a site keeps a file of its own; put it back for "
                             f"this daemon to write that file again")
    for kind, text in held.items():
        build, multiple = BLOCKS[kind]
        wanted[kind] = replace(text, manager, build(nodes, presets(text, multiple)))
    for kind, path in configs.items():
        try:
            _put(path, wanted[kind])
        except OSError as error:
            raise BackendFailure(f"cannot write {path}: {error}") from None
    LOGGER.debug("wrote the %s managed block of %s", manager,
                 ", ".join(sorted(configs.values())))
    return sorted(configs.values())
