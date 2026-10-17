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
Writing rendered output without ever leaving a half-written file behind.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import tempfile

import yaml

from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure

LOGGER = trixlog.get("files")

BLOCK_START: str = "# --- {manager} managed block, do not edit by hand ---"
BLOCK_END: str = "# --- end {manager} managed block ---"


def write(path: str, content: str, mode: int = 0o644) -> str:
    """
    Write through a temporary file and rename, so a reader never sees a partial one.

    The contents reach the disk before the rename is asked for. Without that, the rename
    can survive a power loss while the data does not, and what is left under the real name
    is a file of zeroes - which is the one outcome the rename was supposed to rule out.

    The directory is deliberately not synced afterwards. That would make the rename itself
    durable, which is not what this protects against: a rename lost in a crash leaves the
    previous contents, and every file written through here is derived from Luna or from the
    database, so the next pass puts it back. Measured on ext4, per file: 0.07 ms with no
    sync, 1.65 ms syncing the file, 2.58 ms syncing the directory as well - and this runs
    once per node, so the difference is seconds across a cluster.
    """
    directory = os.path.dirname(path) or "."
    temporary = ""
    try:
        os.makedirs(directory, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".trixd-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except OSError as error:
        # The name is unpredictable, so a failed write leaves one nothing will ever find
        # again - and a disk that is full fails every pass, leaving another each time.
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise BackendFailure(f"cannot write {path}: {error}") from None
    LOGGER.debug("wrote %s", path)
    return path


def write_yaml(path: str, document, mode: int = 0o644) -> str:
    """
    The same, for a document.
    """
    return write(path, yaml.safe_dump(document, default_flow_style=False, sort_keys=False,
                                      explicit_start=True), mode)


def read_text(path: str, fallback: str = "") -> str:
    """
    The file, or the fallback when it is not there.
    """
    if not os.path.isfile(path):
        return fallback
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def set_managed_block(path: str, manager: str, content: str, mode: int = 0o644) -> str:
    """
    Replace our block in a file somebody else also writes to. Everything outside
    the two markers is left as found, so a site keeps its own lines.
    """
    start = BLOCK_START.format(manager=manager)
    end = BLOCK_END.format(manager=manager)
    existing = read_text(path)
    block = f"{start}\n{content.rstrip()}\n{end}\n"

    if start in existing and end in existing:
        head = existing.split(start)[0]
        tail = existing.split(end, 1)[1].lstrip("\n")
        updated = f"{head}{block}{tail}"
    elif start in existing or end in existing:
        # One marker without the other, which only an edit from outside can produce: the
        # write is atomic. Where our block ended cannot be known from here, and both guesses
        # are worse than stopping - appending a second block leaves two start markers, so a
        # consumer reading from the first start to the first end gets the stale content and
        # the new marker with it, while cutting to the end of the file would take whatever a
        # site put after the damage. Said loudly instead, with the file left as it is.
        missing = end if start in existing else start
        raise BackendFailure(f"{path} holds one {manager} marker without the other: "
                             f"{missing.strip()!r} is missing, so where the block ends "
                             f"cannot be told. Nothing was written; repair the file")
    else:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        updated = f"{existing}{separator}{block}"
    return write(path, updated, mode)


def get_managed_block(path: str, manager: str) -> str:
    """
    What our block currently holds, or an empty string.
    """
    start = BLOCK_START.format(manager=manager)
    end = BLOCK_END.format(manager=manager)
    existing = read_text(path)
    if start not in existing or end not in existing:
        return ""
    return existing.split(start, 1)[1].split(end, 1)[0].strip("\n")
