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

import contextlib
import os
import secrets
import stat
import tempfile

import yaml

from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure

LOGGER = trixlog.get("files")

# libyaml's safe dumper where it is installed, as the per-node read path beside it already
# takes libyaml's loader: the same bytes, around seven times faster, and this writes one file
# per node. It ships inside PyYAML, which is already declared, so there is nothing further to
# install and nothing further to mirror for an airgapped site - and where PyYAML was built
# without libyaml the attribute is absent and the pure-Python dumper is used, which is why
# this is read rather than assumed.
SAFE_DUMPER = getattr(yaml, "CSafeDumper", yaml.SafeDumper)

BLOCK_START: str = "# --- {manager} managed block, do not edit by hand ---"
BLOCK_END: str = "# --- end {manager} managed block ---"
# Absent where the platform has no such flag; a person's files are only ever written on Linux.
NO_FOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)


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
    return write(path, yaml.dump(document, Dumper=SAFE_DUMPER, default_flow_style=False,
                                 sort_keys=False, explicit_start=True), mode)


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


def _owned(found: os.stat_result, uid: int, path: str, kind) -> None:
    """
    Refuse what is not the expected kind of thing, or not the person's own.
    """
    if not kind(found.st_mode) or found.st_uid != uid:
        raise BackendFailure(f"{path} is not a plain file or folder owned by uid {uid}; "
                             f"it is left alone")


@contextlib.contextmanager
def _folder(home: str, folder: str, uid: int, gid: int = None):
    """
    A folder in a person's home, the home and the folder each opened without following a
    link and checked to be theirs. Created 0700 and handed to them, through its descriptor,
    when gid is given and it is missing, as the Luna command line creates ~/.luna.
    """
    top = os.open(home, os.O_RDONLY | DIRECTORY | NO_FOLLOW)
    try:
        _owned(os.fstat(top), uid, home, stat.S_ISDIR)
        created = False
        if gid is not None:
            try:
                os.mkdir(folder, 0o700, dir_fd=top)
                created = True
            except FileExistsError:
                pass
        inner = os.open(folder, os.O_RDONLY | DIRECTORY | NO_FOLLOW, dir_fd=top)
        try:
            if created and os.fstat(inner).st_uid != uid:
                os.fchown(inner, uid, gid)
                os.fchmod(inner, 0o700)
            _owned(os.fstat(inner), uid, os.path.join(home, folder), stat.S_ISDIR)
            yield inner
        finally:
            os.close(inner)
    finally:
        os.close(top)


def read_private(home: str, folder: str, name: str, uid: int) -> tuple:
    """
    A person's own file, home/folder/name, as (text, mtime_ns), or ("", 0) when there is none.

    Read as root on the person's behalf, so only a plain file of theirs with one link is
    read: through a link, or a hard link to somebody else's file, the person would be
    handed what is not theirs.
    """
    path = os.path.join(home, folder, name)
    try:
        with _folder(home, folder, uid) as directory:
            handle = os.open(name, os.O_RDONLY | NO_FOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                found = os.fstat(handle)
                _owned(found, uid, path, stat.S_ISREG)
                if found.st_nlink != 1:
                    raise BackendFailure(f"{path} has more than one link; it is left alone")
                chunks = []
                while True:
                    chunk = os.read(handle, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
            finally:
                os.close(handle)
    except FileNotFoundError:
        return "", 0
    except OSError as error:
        raise BackendFailure(f"cannot read {path}: {type(error).__name__}") from None
    return b"".join(chunks).decode("utf-8", errors="replace"), found.st_mtime_ns


def remove_private(home: str, folder: str, name: str, content: str, uid: int) -> None:
    """
    Remove a person's own file, home/folder/name, as root on their behalf, only while it
    still holds `content`: one written there meanwhile is left in place.
    """
    path = os.path.join(home, folder, name)
    try:
        with _folder(home, folder, uid) as directory:
            handle = os.open(name, os.O_RDONLY | NO_FOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                found = os.fstat(handle)
                _owned(found, uid, path, stat.S_ISREG)
                held = os.read(handle, found.st_size + 1).decode("utf-8", errors="replace")
            finally:
                os.close(handle)
            # A writer replaces the file by a rename, so the inode just read still being at
            # the name means nothing was written meanwhile.
            if (held.strip() != content or os.stat(name, dir_fd=directory,
                                                   follow_symlinks=False).st_ino != found.st_ino):
                return
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        return
    except OSError as error:
        raise BackendFailure(f"cannot remove {path}: {type(error).__name__}") from None
    LOGGER.debug("removed %s", path)


def write_private(home: str, folder: str, name: str, content: str, uid: int,
                  gid: int) -> str:
    """
    Write a person's own file, home/folder/name, as root on their behalf: 0600 and theirs
    before the first byte, in a folder of theirs, through a temporary file and a rename
    that replaces whatever is at the name rather than writing through it. A file there that
    is not theirs is not replaced.
    """
    path = os.path.join(home, folder, name)
    temporary = f".{name}.{secrets.token_hex(8)}"
    try:
        with _folder(home, folder, uid, gid) as directory:
            try:
                _owned(os.stat(name, dir_fd=directory, follow_symlinks=False), uid, path,
                       stat.S_ISREG)
            except FileNotFoundError:
                pass
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NO_FOLLOW,
                             0o600, dir_fd=directory)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as target:
                    os.fchown(target.fileno(), uid, gid)
                    os.fchmod(target.fileno(), 0o600)
                    target.write(content)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(temporary, dir_fd=directory)
                raise
    except OSError as error:
        raise BackendFailure(f"cannot write {path}: {type(error).__name__}") from None
    LOGGER.debug("wrote %s", path)
    return path
