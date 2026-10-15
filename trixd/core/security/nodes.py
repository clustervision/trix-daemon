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
Where the daemon learns what a node is. A node is not this daemon's own object, so
whatever holds them registers itself here and the auth route asks only this.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from trixd.common import logging as trixlog
from trixd.common.errors import ServiceUnavailable

LOGGER = trixlog.get("nodes")

# What answers node(name) and enforcing_security(). None until a plugin registers one.
_source = None


def register(source) -> None:
    """
    Offer the node source. The last plugin to register wins, and says so.
    """
    global _source
    if _source is not None and _source is not source:
        LOGGER.info("the node source was replaced")
    _source = source


def current():
    """
    The node source, or a refusal naming why there is none: a node token cannot be
    issued by a daemon that has no way to know the node.
    """
    if _source is None:
        raise ServiceUnavailable("no node source is loaded, so no node can be verified")
    return _source
