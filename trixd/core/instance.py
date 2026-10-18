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
One serving daemon per configuration, decided before start-up touches anything.

A second daemon is refused rather than warned about, unlike a misconfiguration: by the
time it reaches a port it cannot have, it has migrated the database, started a second
audit writer, taken the tasks the first daemon is running and rewritten every render.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import fcntl
import os

from trixd.common import logging as trixlog

LOGGER = trixlog.get("instance")


def lock_path(config) -> str:
    """
    Beside the ini this daemon was started with, so the two controllers of a pair each
    hold their own and a second daemon on one controller meets the first one's.

    The path as given, not where it points: an ini symlinked onto shared storage must
    still leave each controller a lock of its own.
    """
    return f"{os.path.abspath(config.config_file)}.lock"


class SingleInstance:
    """
    An exclusive advisory lock, held by an open descriptor for the life of the process.
    """

    def __init__(self, config):
        self.path = lock_path(config)
        self._handle = None

    def take(self) -> bool:
        """
        True when nothing stops this process serving: either it holds the lock now, or
        the lock file cannot be made at all - a directory problem rather than a second
        daemon, which says so rather than refusing a start that would have worked.
        """
        try:
            # Not inherited, so a child outliving its parent cannot hold the lock open
            # against the process that replaces it.
            handle = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        except OSError as error:
            LOGGER.warning("cannot keep the single-instance lock at %s: %s; a second "
                           "trixd started here will not be refused", self.path, error)
            return True
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(handle)
            return False
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        self._handle = handle
        return True

    def holder(self) -> str:
        """
        What the daemon holding the lock recorded, for the refusal to name. The lock is
        the kernel's and this is only a line of text, so an unreadable one is no error.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def release(self) -> None:
        """
        Closing the descriptor drops the lock. The kernel does that anyway when the
        process ends, however it ends; this is the orderly way out.
        """
        if self._handle is not None:
            os.close(self._handle)
            self._handle = None
