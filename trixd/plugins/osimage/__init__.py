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
The osimage plugin: an OS image tree built by the TrinityX books in a child process
of the daemon, which starts and supervises it. Luna asks, follows and packs; nothing here
knows Luna.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

# By the canonical name: the loader also imports this directory under a module name of
# its own, and a relative import here would give that copy a second supervisor.
from trixd.core.render import triggers
from trixd.plugins.osimage import supervisor


def install() -> None:
    """
    Plug this plugin in. The loader calls it once the plugin has loaded; the trigger is
    owned, so unplugging the plugin stops the supervisor with it.
    """
    triggers.register("osimage_jobs", supervisor.current(), owner="osimage")


__all__ = ["install"]
