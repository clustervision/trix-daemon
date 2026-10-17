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

# What answers node(name) and enforcing_security(), and which plugin offered it. None until
# a plugin registers one.
_source = None
_owner: str = ""


def register(source, owner: str = "") -> None:
    """
    Offer the node source. The last plugin to register wins, and says so. The owner is the
    plugin, so unplugging it takes the source with it, as it does for a trigger.
    """
    global _source, _owner
    if _source is not None and _source is not source:
        LOGGER.info("the node source was replaced")
    _source = source
    _owner = owner


def forget(owner: str) -> bool:
    """
    Drop the node source one plugin offered, for when it is unplugged.

    Without this the source outlived its plugin: a node sign-in went on asking an unplugged
    plugin's client whether Luna knows the node, and answering a token on what it said. A
    refusal naming the missing source is the honest answer - a node cannot be verified by a
    daemon that no longer has any way to know it.
    """
    global _source, _owner
    if not owner or owner != _owner:
        return False
    LOGGER.warning("the node source %s offered is gone, so a node sign-in refuses until "
                   "something offers one again", owner)
    _source = None
    _owner = ""
    return True


def current():
    """
    The node source, or a refusal naming why there is none: a node token cannot be
    issued by a daemon that has no way to know the node.
    """
    if _source is None:
        raise ServiceUnavailable("no node source is loaded, so no node can be verified")
    return _source
