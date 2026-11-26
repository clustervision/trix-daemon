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
presets the `Defaults` block of each file carries. The block itself - the markers, what is
outside it, the write in place - is trixd/utils/blocks.py, which the genders file is written
through as well.

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
import stat

from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure, Conflict, ServiceUnavailable
from trixd.plugins.slurm.hostnames import fold_nodeset
from trixd.utils import blocks

LOGGER = trixlog.get("slurm")

# The block each file keeps its presets in, written by the administrator or by the Slurm app.
PRESETS: str = "Defaults"
# What the installer's roles wrote above the presets block they gave a file.
_PRESETS_NOTES: tuple = (
    "# Defaults blocks are for trinityx-config-slurm to read and append generated config.",
    "# The settings and values can be altered and will not be overwritten.")
# The three files as the installer's slurm role first places them; its templates are the
# source. Only a forced reset writes these: an init keeps the file the installer placed.
INSTALLED: dict = {
    "nodes": ("NodeName=DEFAULT Boards=1 SocketsPerBoard=1 CoresPerSocket=1 ThreadsPerCore=1 "
              "RealMemory=100 State=UNKNOWN\n"
              "\n"
              "# Below line is an example/placeholder and can be altered\n"
              "# NodeName=dummynode\n"
              "\n"
              "\n"),
    "partitions": ("PartitionName=defq   Nodes=ALL Shared=YES MaxTime=INFINITE State=UP "
                   "Default=YES AllowAccounts=ALL\n"
                   "\n"
                   "\n"),
    "gres": "# File to configure slurm gres for nodes\n",
}
# What gives a file its first block; the render's refusal names it.
INIT: str = "POST /api/v1/slurm/files/actions/init"
# A preset line's settings: the first is the key the node or group is looked up by, the rest
# are what the node's line carries. Anything after a `#` is a note about the preset.
_SETTING = re.compile(r"(\S+)=(\S+)")


def presets(text: str, multiple: bool = False) -> dict:
    """
    The file's own presets, keyed by their first setting: `NodeName=node001` or
    `GroupGres=gpu` to the settings that follow it. Commented out is how they are written,
    so one leading `#` comes off; `multiple` keeps every line a key carries, as a node may
    take more than one gres line and only one of everything else.
    """
    found: dict = {}
    for line in (blocks.block(text, PRESETS) or "").splitlines():
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


def _held(configs: dict) -> dict:
    """
    What each of the files holds now, read as a backup is: it goes into one, and slurm owns
    the directory and can put a link there.
    """
    held = {}
    for kind, path in configs.items():
        try:
            held[kind] = blocks.kept(path)
        except OSError as error:
            raise BackendFailure(f"cannot read {path}: {error}") from None
    return held


def _given(text: str, manager: str, emptied: tuple = ()) -> str:
    """
    The text with each block named in `emptied` holding nothing, and with the presets block
    and ours at its end where ours is missing, as the installer's roles gave them.
    """
    for name in emptied:
        text = blocks.replace(text, name, "") or text
    if blocks.block(text, manager) is None:
        if blocks.block(text, PRESETS) is None:
            text = blocks.append(text, PRESETS, _PRESETS_NOTES)
        text = blocks.append(text, manager)
    return text


def initial(configs: dict, manager: str, reset: bool, force: bool = False) -> tuple:
    """
    (the texts an init writes, what each file holds now). A file with no block of ours
    gains the presets block and ours at its end, as the installer's roles gave them. A reset
    empties the presets as well and keeps every line outside the blocks; forced, it takes
    the installer's own file under both instead, whatever the file holds. Without a reset,
    all three carrying our block already is a Conflict, since there is nothing to give. A
    reset's texts have our block empty here, to be judged, and are written `carried`.
    """
    held = _held(configs)
    try:
        texts = (dict(INSTALLED) if reset and force
                 else {k: v.decode("utf-8") for k, v in held.items()})
    except UnicodeDecodeError as error:
        raise BackendFailure(f"the slurm files are not text: {error}") from None
    if not reset and all(blocks.block(text, manager) is not None for text in texts.values()):
        raise Conflict(f"{', '.join(sorted(configs.values()))} carry the {manager} managed "
                       f"block already, so there is nothing to initialise; `reset` empties "
                       f"the presets and renders the block afresh, and the files as they "
                       f"are go to .bkp")
    emptied = (PRESETS, manager) if reset else ()
    return {kind: _given(text, manager, emptied) for kind, text in texts.items()}, held


def restored(configs: dict, manager: str) -> tuple:
    """
    (the texts a forced restore is judged on, what each file holds now, what each backup
    holds). A backup's own lines and presets come back, never its nodes, which are not
    Luna's nodes now: our block is empty here, added where the backup has none, and the
    texts are written `carried`.
    """
    held = _held(configs)
    try:
        kept = {kind: blocks.kept(f"{path}.bkp") for kind, path in configs.items()}
        texts = {kind: _given(data.decode("utf-8"), manager, (manager,))
                 for kind, data in kept.items()}
    except (OSError, UnicodeDecodeError) as error:
        raise ServiceUnavailable(f"the backups cannot be read: {error}") from None
    return texts, held, kept


def carried(texts: dict, held: dict, manager: str) -> dict:
    """
    The texts with our block holding what each file's block holds now. The render writes
    that block next, and one that cannot run then leaves slurm the nodes it had.
    """
    return {kind: blocks.replace(text, manager, blocks.block(
        held[kind].decode("utf-8", "replace"), manager) or "")
            for kind, text in texts.items()}


def _keep(path: str, data: bytes) -> None:
    """
    What the file held, in the `.bkp` a restore reads. One that is not there yet is made
    with the file's owner, group and mode, which is what lets the helper write it later.
    """
    backup = f"{path}.bkp"
    made = not os.path.lexists(backup)
    handle = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(handle, "wb") as target:
        if made:
            found = os.stat(path)
            os.fchown(handle, found.st_uid, found.st_gid)
            os.fchmod(handle, stat.S_IMODE(found.st_mode))
        target.write(data)
        target.flush()
        os.fsync(handle)


def write_initial(configs: dict, texts: dict, held: dict, files_first: bool = False) -> list:
    """
    An init's texts into the three files, what each held into its `.bkp` first: every
    backup is written before the first file, so a write that fails leaves what was there.
    A restore's go `files_first`, since the backups are what it reads: cut short, it leaves
    them whole to be asked again.
    """
    steps = ((_keep, held), (blocks.put, texts))
    for write, source in (steps[::-1] if files_first else steps):
        for kind, path in configs.items():
            try:
                write(path, source[kind])
            except OSError as error:
                raise BackendFailure(f"cannot write {path}"
                                     f"{'.bkp' if write is _keep else ''}: {error}") from None
    return sorted(configs.values())


def keep_back(configs: dict, kept: dict) -> list:
    """
    Each `.bkp` that no longer holds what it held written back, after a restore that failed
    part-way, and the ones that would not go back.
    """
    lost = []
    for kind, path in configs.items():
        try:
            if blocks.kept(f"{path}.bkp") != kept[kind]:
                _keep(path, kept[kind])
        except OSError as error:
            lost.append(f"{path}.bkp")
            LOGGER.error("the backup %s.bkp could not be put back: %s", path, error)
    return lost


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
    missing = sorted(configs[kind] for kind in configs
                     if blocks.block(held[kind], manager) is None)
    if missing:
        raise BackendFailure(f"{', '.join(missing)} has no {manager} managed block, so the "
                             f"slurm files were left exactly as they are. Taking the block "
                             f"out is how a site keeps a file of its own; {INIT} puts it "
                             f"back for this daemon to write that file again")
    for kind, text in held.items():
        build, multiple = BLOCKS[kind]
        wanted[kind] = blocks.replace(text, manager,
                                      build(nodes, presets(text, multiple)))
    for kind, path in configs.items():
        try:
            blocks.put(path, wanted[kind])
        except OSError as error:
            raise BackendFailure(f"cannot write {path}: {error}") from None
    LOGGER.debug("wrote the %s managed block of %s", manager,
                 ", ".join(sorted(configs.values())))
    return sorted(configs.values())
