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
A file somebody else also writes: our block between the markers every writer of these files
uses, and everything outside it handed back as it was found.

The slurm files and the genders file have three writers - Luna's own config hooks, the OOD
slurm app and this daemon - and what makes that safe is that they all leave the same bytes.
This is the layer they share, so the markers, the blank lines and the administrator's own
text around the block are one definition read by every plugin that writes such a file
rather than one per plugin.

trixd/utils/files.py writes a managed block of its own in a different pair of markers, and
creates one where a file has none. Both are deliberate and neither belongs here: a file
these markers reach carries this pair, and an administrator who takes the block out has
turned the file into their own, which is a refusal rather than a block to put back.
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

# Three spaces before the end marker's hashes, and a block is found by them alone.
BLOCK_START: str = "#### {manager} Managed block start ####"
BLOCK_END: str = "#### {manager} Managed block end   ####"
_START = re.compile(r"^#### (?P<manager>.+) Managed block start ####")
_END = re.compile(r"^#### (?P<manager>.+) Managed block end   ####")


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


def put(path: str, text: str, follow: bool = False) -> None:
    """
    Write the file where it is, so its owner, group, mode and inode stay: the slurm files are
    root's of the cluster's admin group with group write, and a temporary file and a rename
    would hand them this process's own.

    `follow` is for a path the installer makes a symlink - /etc/genders points into the
    shared tree, where both controllers reach one file - because refusing to follow one
    there refuses the only path there is.
    """
    flags = os.O_WRONLY | os.O_TRUNC | (0 if follow else os.O_NOFOLLOW)
    handle = os.open(path, flags)
    with os.fdopen(handle, "w", encoding="utf-8") as target:
        target.write(text)
        target.flush()
        os.fsync(target.fileno())
