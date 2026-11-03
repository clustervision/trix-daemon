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
What is mounted below a path, read-only loop mounts of a disc, the mirror layout the
books expect on top of one, and unmounting with the mount table read back afterwards.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os

from trixd.common import logging as trixlog
from trixd.utils import shell

LOGGER = trixlog.get("osimage.mounts")

# The repositories a RedHat disc carries at its root, linked into the path the books
# append to the base URL: <Repo>/<arch>/os.
DISC_REPOSITORIES: tuple = ("BaseOS", "AppStream")


def targets_below(path: str) -> list:
    """
    Every mount target at or below a path, deepest first. Compared by real-path prefix
    against the whole table: asking findmnt for the path itself answers only a mount
    that is the path, and an image tree or a job directory never is one, so that form
    waves through exactly the case this exists to catch.
    """
    listed = shell.run(["findmnt", "-rn", "-o", "TARGET"], check=True)
    root = os.path.realpath(path)
    prefix = root.rstrip("/") + "/"
    targets = [target for target in listed.out.split("\n")
               if target == root or target.startswith(prefix)]
    return sorted(targets, key=lambda target: (target.count("/"), len(target)), reverse=True)


def loop_mount_ro(iso: str, target: str) -> str:
    """
    Mount a disc image read-only at a directory, made if need be.
    """
    os.makedirs(target, exist_ok=True)
    shell.run(["mount", "-o", "loop,ro", "--", iso, target], check=True)
    LOGGER.info("mounted %s read-only at %s", iso, target)
    return target


def mirror_layout(mount: str, repo: str, arch: str) -> str:
    """
    The URL the books take as a base for what a mounted disc holds. A RedHat disc keeps
    BaseOS and AppStream at its root and the books append <Repo>/<arch>/os to the base,
    so each present repository is linked under that path; any other disc is linked whole.
    """
    present = [name for name in DISC_REPOSITORIES if os.path.isdir(os.path.join(mount, name))]
    if present:
        for name in present:
            os.makedirs(os.path.join(repo, name, arch), exist_ok=True)
            _link(os.path.join(mount, name), os.path.join(repo, name, arch, "os"))
    else:
        os.makedirs(os.path.dirname(repo), exist_ok=True)
        _link(mount, repo)
    return "file://" + repo + "/"


def unmount_all(path: str) -> list:
    """
    Unmount everything below a path, deepest first, and answer what the table still
    shows below it afterwards: the read-back, not the unmount, is the evidence.
    """
    for target in targets_below(path):
        done = shell.run(["umount", "--", target])
        if done.ok:
            LOGGER.info("unmounted %s", target)
        else:
            LOGGER.error("could not unmount %s: %s", target, done.err or done.out)
    return targets_below(path)


def release(path: str) -> list:
    """
    Lazily unmount what a runner left below a path, and answer what the table still
    shows afterwards. The runner unmounts its own; this is for what it could not, so
    each one is said loudly.
    """
    left = targets_below(path)
    for target in left:
        done = shell.run(["umount", "-l", "--", target])
        if done.ok:
            LOGGER.warning("lazily unmounted %s, which the runner left behind", target)
        else:
            LOGGER.error("could not unmount %s lazily: %s", target, done.err or done.out)
    return targets_below(path) if left else []


def _link(source: str, link: str) -> None:
    """
    A symlink, replaced when a previous run left one.
    """
    if os.path.islink(link):
        os.unlink(link)
    os.symlink(source, link)
