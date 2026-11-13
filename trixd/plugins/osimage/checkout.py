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
Where the books come from: the checkout of the TrinityX repository on the controller, and
the archive that is fetched into its place when it is not there. The cluster's own answer
file is never taken from an archive.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import contextlib
import fcntl
import glob
import http.client
import os
import posixpath
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

from trixd.common import logging as trixlog
from trixd.common.errors import ServiceUnavailable

LOGGER = trixlog.get("osimage.checkout")

SITE: str = "site"
ANSWERS: str = posixpath.join("group_vars", "all.yml")
# What the build needs of the books besides the playbooks, relative to the site directory.
BOOK_FILES: tuple = ("ansible.cfg", posixpath.join("plugins", "connection", "lchroot.py"))
# How long one read of the download may stall, how long the whole download may take, how
# large the archive may be and how much it may unpack to: the answer of a mirror that never
# ends, or an archive that expands without end, must not fill the disk.
SOCKET_TIMEOUT: int = 30
FETCH_TIMEOUT: int = 120
ARCHIVE_MOST: int = 512 * 1024 * 1024
EXTRACTED_MOST: int = 2 * 1024 * 1024 * 1024
CHUNK: int = 64 * 1024
# The books are code that runs as root, so they are not taken over a channel anybody can alter.
SCHEME: str = "https://"
SCRATCH: str = ".checkout-"


def site(repo: str) -> str:
    """
    The books of a checkout.
    """
    return os.path.join(repo, SITE)


def has_site_answers(repo: str) -> bool:
    """
    Whether the checkout carries the cluster's answer file, which ansible then finds
    beside the playbooks by itself.
    """
    return os.path.isfile(os.path.join(site(repo), ANSWERS))


def missing(repo: str) -> list:
    """
    The book files the build needs that the checkout does not have.
    """
    return [name for name in BOOK_FILES if not os.path.isfile(os.path.join(site(repo), name))]


def check_url(repo: str, url: str) -> None:
    """
    Refuse an address the books cannot be fetched from: none, or not https.
    """
    if not url:
        raise ServiceUnavailable(f"the books are not at {repo} and repo_url is empty: put the "
                                 f"checkout there, or set repo_url in the osimage settings to "
                                 f"the https archive of a TrinityX tag to fetch them from")
    if not url.startswith(SCHEME):
        raise ServiceUnavailable(f"repo_url must be an https address, not {url}")


def ensure(repo: str, url: str, say) -> bool:
    """
    Fetch the archive into repo when repo is not there, telling say each step. Answers
    whether it did. The archive is unpacked beside repo and renamed into place, so a
    download that fails leaves nothing at repo for the next start to mistake for the
    books.
    """
    if os.path.isdir(repo):
        return False
    check_url(repo, url)
    parent = os.path.dirname(repo.rstrip("/")) or "/"
    os.makedirs(parent, exist_ok=True)
    with _locked(parent):
        if os.path.isdir(repo):
            return False
        # The lock is held, so a scratch directory still here is a fetch that was killed.
        for stale in glob.glob(os.path.join(glob.escape(parent), SCRATCH + "*")):
            shutil.rmtree(stale, ignore_errors=True)
        say(f"fetching the books from {url}")
        LOGGER.info("fetching the books from %s into %s", url, repo)
        scratch = tempfile.mkdtemp(prefix=SCRATCH, dir=parent)
        try:
            archive = os.path.join(scratch, "archive.tar")
            _download(url, archive)
            unpacked = os.path.join(scratch, "tree")
            _unpack(archive, unpacked)
            if not os.path.isdir(site(unpacked)):
                raise ServiceUnavailable(f"the archive from {url} has no {SITE} directory")
            os.rename(unpacked, repo)
        except OSError as error:
            raise ServiceUnavailable(f"the books from {url} could not be put at {repo}: "
                                     f"{error}") from None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    say("unpacked")
    LOGGER.info("the books are at %s", repo)
    return True


@contextlib.contextmanager
def _locked(directory: str):
    """
    Exclusive over a directory for as long as the block runs, across processes: builds
    are processes of their own, and two that start together must not both fetch.
    """
    handle = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def _download(url: str, target: str) -> None:
    """
    The url into a file, within the time and the size allowed.
    """
    ends = time.monotonic() + FETCH_TIMEOUT
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=SOCKET_TIMEOUT) as answer, \
                open(target, "wb") as handle:
            while True:
                chunk = answer.read(CHUNK)
                if not chunk:
                    return
                size += len(chunk)
                if size > ARCHIVE_MOST:
                    raise ServiceUnavailable(f"the archive from {url} is larger than "
                                             f"{ARCHIVE_MOST} bytes")
                if time.monotonic() > ends:
                    raise ServiceUnavailable(f"the archive from {url} did not arrive "
                                             f"within {FETCH_TIMEOUT} s")
                handle.write(chunk)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        raise ServiceUnavailable(f"cannot fetch {url}: {error}") from None


def _unpack(archive: str, destination: str) -> None:
    """
    The archive under destination with its top directory stripped. A member that would
    land outside destination, directly or through a link, refuses the whole archive; so
    does anything that is not a file, a directory or a link that stays inside, and an
    archive that unpacks to more than EXTRACTED_MOST bytes. The answer file is skipped,
    whatever the archive says.
    """
    root = os.path.realpath(destination)
    os.makedirs(root)
    left = EXTRACTED_MOST
    try:
        with tarfile.open(archive, "r:*") as bundle:
            for member in bundle:
                names = [part for part in member.name.split("/") if part not in ("", ".")]
                if member.name.startswith("/") or ".." in names:
                    raise ServiceUnavailable(f"the archive names {member.name}, which leads "
                                             f"out of its directory")
                parts = names[1:]
                if not parts:
                    if not member.isdir():
                        raise ServiceUnavailable(f"the archive has a file {member.name} "
                                                 f"outside a top directory")
                    continue
                if posixpath.join(*parts) == posixpath.join(SITE, ANSWERS):
                    LOGGER.warning("the archive carries %s, which is the cluster's own and "
                                   "is not taken from a download", member.name)
                    continue
                left -= _place(bundle, member, os.path.join(root, *parts), root, left)
    except tarfile.TarError as error:
        raise ServiceUnavailable(f"the archive cannot be read: {error}") from None
    _refuse_escaping_links(root)


def _place(bundle, member, where: str, root: str, left: int) -> int:
    """
    One member at its place: a directory, a file, or a link that stays inside the tree.
    Answers how many bytes it wrote, and refuses a file that would pass what is left.
    """
    # Links made earlier in the same archive are followed here, so a file cannot be written
    # through one that leads out.
    if not _inside(root, os.path.realpath(os.path.dirname(where))):
        raise ServiceUnavailable(f"the archive writes {member.name} through a link that "
                                 f"leaves its directory")
    written = 0
    if member.isdir():
        os.makedirs(where, exist_ok=True)
        os.chmod(where, (member.mode & 0o777) | 0o700)
    elif member.isreg():
        if os.path.islink(where):
            raise ServiceUnavailable(f"the archive writes {member.name} over a link")
        os.makedirs(os.path.dirname(where), exist_ok=True)
        with bundle.extractfile(member) as source, open(where, "wb") as handle:
            for chunk in iter(lambda: source.read(CHUNK), b""):
                written += len(chunk)
                if written > left:
                    raise ServiceUnavailable(f"the archive unpacks to more than "
                                             f"{EXTRACTED_MOST} bytes")
                handle.write(chunk)
        os.chmod(where, member.mode & 0o777)
    elif member.issym():
        target = os.path.realpath(os.path.join(os.path.dirname(where), member.linkname))
        if os.path.isabs(member.linkname) or not _inside(root, target):
            raise ServiceUnavailable(f"the archive links {member.name} to {member.linkname}, "
                                     f"which leaves its directory")
        os.makedirs(os.path.dirname(where), exist_ok=True)
        os.symlink(member.linkname, where)
    else:
        raise ServiceUnavailable(f"the archive holds {member.name}, which is neither a file, "
                                 f"a directory nor a link")
    return written


def _refuse_escaping_links(root: str) -> None:
    """
    Every link of the unpacked tree, resolved as it now stands: a link that was inside when
    it was made can lead out once the links after it exist.
    """
    for here, folders, entries in os.walk(root):
        for entry in folders + entries:
            where = os.path.join(here, entry)
            if os.path.islink(where) and not _inside(root, os.path.realpath(where)):
                raise ServiceUnavailable(f"the archive links {os.path.relpath(where, root)} "
                                         f"to {os.readlink(where)}, which leaves its directory")


def _inside(root: str, path: str) -> bool:
    """
    Whether a path, as written, is the root or below it.
    """
    return path == root or path.startswith(root + os.sep)
