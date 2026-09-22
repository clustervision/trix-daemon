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
The engine and the session factory, with the SQLite pragmas several workers need.
"""

__author__      = 'Sumit Sharma'
__copyright__   = 'Copyright 2026, TrinityX Project'
__license__     = 'GPL'
__version__     = '1.0'
__maintainer__  = 'Sumit Sharma'
__email__       = 'sumit.sharma@clustervision.com'
__status__      = 'Development'

import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from trix.common import logging as trixlog
from trix.db.models import Base

LOGGER = trixlog.get('db')

BUSY_TIMEOUT_MS: int = 10000

_engine: Engine = None
_factory: sessionmaker = None


def _sqlite_path(url: str) -> str:
    if url.startswith('sqlite:////'):
        return '/' + url[len('sqlite:////'):]
    if url.startswith('sqlite:///'):
        return url[len('sqlite:///'):]
    return ''


@event.listens_for(Engine, 'connect')
def _pragmas(dbapi_connection, record) -> None:
    """Write-ahead logging and a busy timeout: several threads share one file."""
    if dbapi_connection.__class__.__module__.startswith('sqlite3'):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.close()


def start(url: str) -> Engine:
    """Open the database and make sure the schema is present."""
    global _engine, _factory
    path = _sqlite_path(url)
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    _engine = create_engine(url, future=True, connect_args={'check_same_thread': False}
                            if url.startswith('sqlite') else {})
    Base.metadata.create_all(_engine)
    _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    LOGGER.info('database ready at %s', path or url)
    return _engine


def engine() -> Engine:
    """The engine this daemon opened."""
    return _engine


def session() -> Session:
    """A new session. The caller closes it."""
    return _factory()


def scoped():
    """Session dependency for a path operation."""
    handle = _factory()
    try:
        yield handle
        handle.commit()
    except Exception:
        handle.rollback()
        raise
    finally:
        handle.close()
