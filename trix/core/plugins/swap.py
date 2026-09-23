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
The only code that may assign to the route table.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from fastapi import APIRouter, FastAPI

from trix.common import logging as trixlog
from trix.core.plugins.registry import current as registry

LOGGER = trixlog.get('swap')


def exchange(app: FastAPI, name: str, router: APIRouter = None) -> list:
    """Replace one plugin's routes with a new set, or with none.

    The list is rebuilt and assigned, never mutated in place: a request already
    inside a handler keeps the table it resolved against and finishes, while the
    next request sees the new one. There is no half-built state in between.
    """
    drop = registry().owned_routes(name)
    keep = [route for route in app.router.routes if id(route) not in drop]
    # A router built with a prefix already carries the full path on each route,
    # so these are ready to serve without being re-registered.
    added = list(router.routes) if router is not None else []

    app.router.routes = keep + added
    app.openapi_schema = None
    LOGGER.info('route table is now %d routes; %s contributes %d',
                len(app.router.routes), name, len(added))
    return added
