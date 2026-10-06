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
Reading and writing the documents a plugin owns.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import socket

from sqlalchemy import select

from trix.common import logging as trixlog
from trix.db import engine as database
from trix.db import models

LOGGER = trixlog.get("declarations")


def _origin() -> str:
    """
    Which controller wrote this, so a document can be traced to where it was changed.
    """
    return socket.gethostname().split(".")[0]


def read(plugin: str, kind: str, name: str = "", fallback=None):
    """
    The stored document, or the fallback when nothing has been written yet.
    """
    handle = database.session()
    try:
        row = handle.scalar(
            select(models.Declaration).where(
                models.Declaration.plugin == plugin,
                models.Declaration.kind == kind,
                models.Declaration.name == name))
        return dict(row.body) if row is not None else fallback
    finally:
        handle.close()


def write(plugin: str, kind: str, name: str, body: dict) -> dict:
    """
    Replace the document, bumping its version so a peer can order the two.
    """
    handle = database.session()
    try:
        row = handle.scalar(
            select(models.Declaration).where(
                models.Declaration.plugin == plugin,
                models.Declaration.kind == kind,
                models.Declaration.name == name))
        if row is None:
            row = models.Declaration(plugin=plugin, kind=kind, name=name,
                                     body=dict(body), origin=_origin())
            handle.add(row)
        else:
            row.body = dict(body)
            row.version = (row.version or 0) + 1
            row.origin = _origin()
        handle.commit()
        LOGGER.info("declaration %s/%s/%s written at version %s",
                    plugin, kind, name or "-", row.version)
        return dict(row.body)
    except Exception:
        handle.rollback()
        raise
    finally:
        handle.close()


def merge(plugin: str, kind: str, name: str, body: dict, defaults: dict) -> dict:
    """
    Change only the keys that were sent, leaving the rest as they were.
    """
    held = read(plugin, kind, name, dict(defaults))
    held.update({key: value for key, value in (body or {}).items() if value is not None})
    return write(plugin, kind, name, held)


def listing(plugin: str, kind: str = "") -> list:
    """
    Every document a plugin owns, optionally of one kind.
    """
    handle = database.session()
    try:
        query = select(models.Declaration).where(models.Declaration.plugin == plugin)
        if kind:
            query = query.where(models.Declaration.kind == kind)
        return [{"kind": row.kind, "name": row.name, "version": row.version,
                 "body": dict(row.body)} for row in handle.scalars(query).all()]
    finally:
        handle.close()
