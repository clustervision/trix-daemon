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
The Alembic environment. The URL comes from trix.ini unless one is passed in.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from alembic import context
from sqlalchemy import engine_from_config, pool

from trix.common import settings as configuration
from trix.db.models import Base

config = context.config
target_metadata = Base.metadata


def database_url() -> str:
    """
    Whatever the caller set, or the configured one.
    """
    return config.get_main_option('sqlalchemy.url') or configuration.load().database.url


def run_migrations_offline() -> None:
    """
    Emit the statements without a connection, for review.
    """
    context.configure(url=database_url(), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True,
                      dialect_opts={'paramstyle': 'named'})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Apply against the real database.
    """
    section = config.get_section(config.config_ini_section) or {}
    section['sqlalchemy.url'] = database_url()
    connectable = engine_from_config(section, prefix='sqlalchemy.', poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # SQLite cannot alter a column in place, so every migration runs in batch
        # mode: the table is rebuilt rather than altered.
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
