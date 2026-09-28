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
What happens before the listeners open, and what happens when they close.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from trix.common import logging as trixlog
from trix.common.settings import current as settings
from trix.core.plugins import loader
from trix.core.render.engine import current as renders
from trix.core.render.triggers import current as luna_hashes
from trix.core.replication.sweep import current as sweep
from trix.core.security import keys, principals
from trix.db import engine as database
from trix.db import models

LOGGER = trixlog.get("lifespan")


def seed(handle) -> None:
    """
    The administrator the ini names. Until somebody is put in a usergroup the gate
    permits everything, which is what a cluster does today.
    """
    config = settings().api
    if config.username:
        row = handle.scalar(select(models.Principal)
                            .where(models.Principal.name == config.username))
        if row is None:
            handle.add(models.Principal(
                name=config.username, kind="user", source="local", admin=True,
                password_digest=principals.hash_password(config.password)))
            LOGGER.info("seeded the local principal %s", config.username)
    handle.commit()


def start(app: FastAPI) -> None:
    """
    Everything that must be true before the first request is accepted.
    """
    config = settings()
    database.start(config.database.url)

    handle = database.session()
    try:
        seed(handle)
    finally:
        handle.close()

    keys.set_current(keys.KeyRing(config.tokens).load())

    states = loader.load_all(app)
    LOGGER.info("plugins: %s", ", ".join(f"{k}={v}" for k, v in states.items()) or "none")

    done = renders().run_all()
    LOGGER.info("reconciled %d render(s) before opening the listeners", done)

    # Both are background timers, so they start after the reconcile: the first
    # answer a caller gets should come from a render, not from an empty cache.
    luna_hashes().start()
    sweep().start()


def stop(app: FastAPI) -> None:
    """
    Stop the timers. Nothing here holds a queue, so there is nothing to drain.
    """
    luna_hashes().shutdown()
    sweep().shutdown()
    LOGGER.info("shutting down")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Run the startup sequence, serve, then shut down.
    """
    start(app)
    try:
        yield
    finally:
        stop(app)
