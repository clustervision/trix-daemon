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
The daemon's long-lived threads, under names that can be seen from outside it.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import ctypes
import threading

from trixd.common import logging as trixlog

LOGGER = trixlog.get("threads")

PR_SET_NAME: int = 15


class Thread(threading.Thread):
    """
    A thread the kernel knows by the name Python gives it, so `ps -T` and `top -H` say
    which one is busy instead of listing every one as the interpreter. The kernel keeps
    15 bytes of a name and silently drops the rest, so a name is told apart within those.
    """

    def run(self) -> None:
        """
        Name the thread from inside it, which is the only place the kernel allows, then run.
        """
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(PR_SET_NAME, self.name.encode(), 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "prctl refused the name")
        except (OSError, AttributeError) as error:
            # Only what a listing shows is lost, so the thread runs all the same.
            LOGGER.debug("thread %s keeps the name of the process: %s", self.name, error)
        super().run()
