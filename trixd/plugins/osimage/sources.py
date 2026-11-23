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
The sources an operator names for an image before Luna's create of it arrives: held by
image name in the plugin's own declarations, taken by the create of that name, and
forgotten when nobody comes for them.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import urllib.parse

from trixd.common import logging as trixlog
from trixd.common.errors import BadRequest, NotFound
from trixd.plugins.osimage import jobs, rules
from trixd.utils import declarations

LOGGER = trixlog.get("osimage.sources")

PLUGIN: str = "osimage"
KIND: str = "sources"
# Luna's queue can hold a create behind a pack for a while, so an hour; a create later than
# that is refused rather than built from something else, because the operator asked for these.
HELD_SECONDS: int = 3600
# Sources no create came for are forgotten after this long.
KEPT_SECONDS: int = 86400


def check_here(media: list) -> None:
    """
    Refuse a directory or an ISO that is not on this controller, naming its position: the
    build reads it here, and a typo is cheaper before the record exists than after.
    """
    for position, item in enumerate(media, 1):
        path = urllib.parse.urlsplit(item).path if item.startswith("file://") else item
        if path.startswith("/") and not os.path.exists(path):
            raise BadRequest(f"media item {position} is not on this controller: {item}")


def hold(name: str, media: list, by: str) -> dict:
    """
    Hold the sources of the next create of an image, checked as a create's media is and
    for being here. A second hold for the name replaces the first.
    """
    rules.check_name(name)
    rules.check_media(media)
    check_here(media)
    held = {"media": list(media), "held_by": by, "held_at": jobs.stamp()}
    declarations.write(PLUGIN, KIND, name, held)
    LOGGER.info("%s held %d sources for the next create of %s", by, len(media), name)
    return {"name": name, **held}


def drop(name: str) -> None:
    """
    Forget the sources held for an image, or 404 when none are.
    """
    rules.check_name(name)
    if not declarations.forget(PLUGIN, KIND, name):
        raise NotFound(f"no sources are held for {name}")


def take(name: str):
    """
    The sources held for an image, forgotten as they are taken, or none. Ones held longer
    than an hour are refused in a sentence that says what to do.
    """
    held = declarations.read(PLUGIN, KIND, name)
    if held is None:
        return None
    declarations.forget(PLUGIN, KIND, name)
    age = jobs.seconds_since(held["held_at"])
    if age > HELD_SECONDS:
        raise BadRequest(f"the sources held for {name} by {held['held_by']} at {held['held_at']} "
                         f"are {int(age // 60)} minutes old, more than {HELD_SECONDS // 60}: run "
                         f"luna osimage create again with --mirrors")
    return held


def prune() -> list:
    """
    Forget the sources no create came for in a day; the names forgotten.
    """
    forgotten = []
    for entry in declarations.listing(PLUGIN, KIND):
        if jobs.seconds_since(entry["body"]["held_at"]) > KEPT_SECONDS:
            declarations.forget(PLUGIN, KIND, entry["name"])
            forgotten.append(entry["name"])
    return forgotten
