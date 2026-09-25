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
The application object. It reads no configuration and touches no filesystem.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from fastapi import FastAPI

from trix.api.v1 import auth, system
from trix.common import errors
from trix.common.constants import API_PREFIX, DAEMON_NAME, DAEMON_VERSION
from trix.core.lifespan import lifespan
from trix.core.luna import proxy


def create_app(with_lifespan: bool = True) -> FastAPI:
    """
    Build the daemon. Plugin routes are added later, at load time.
    """
    app = FastAPI(
        title="TrinityX trix-daemon",
        description="Everything in TrinityX that is not a physical node.",
        version=DAEMON_VERSION,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        lifespan=lifespan if with_lifespan else None,
    )
    errors.register(app)
    app.include_router(auth.router)
    app.include_router(system.router)
    app.include_router(proxy.router)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"daemon": DAEMON_NAME, "version": DAEMON_VERSION, "api": API_PREFIX}

    return app
