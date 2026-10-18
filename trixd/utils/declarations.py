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

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from trixd.common import logging as trixlog
from trixd.common.errors import ServiceUnavailable
from trixd.db import engine as database
from trixd.db import models

LOGGER = trixlog.get("declarations")

# How many times an amend reads the row again after losing the version it wrote against. A
# round is only lost to a write that landed, so the bound is reached by a key being written
# faster than it can be read, not by a race. Past it the caller is refused: answering 200
# with a document that was never stored is the one outcome this must not produce.
ROUNDS: int = 5


def _origin() -> str:
    """
    Which controller wrote this, so a document can be traced to where it was changed.
    """
    return socket.gethostname().split(".")[0]


def forget(plugin: str, kind: str, name: str) -> bool:
    """
    Mark one document deleted, so the deletion reaches the peer instead of being undone by it.

    The row stays: the sweep re-adds a row that is missing here and present there, so a real
    delete comes back within the interval. Marked, it counts as no row on both.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.Declaration).where(
            models.Declaration.plugin == plugin, models.Declaration.kind == kind,
            models.Declaration.name == name))
        if row is None:
            return False
        if row.deleted:
            # Marked here already, but the peer may still hold a live row at this version.
            # Bumping is what lets the delete, issued again, settle that; a silent no-op
            # leaves the operator no way to break the tie.
            row.version = (row.version or 0) + 1
            row.origin = _origin()
            handle.commit()
            LOGGER.info("declaration %s/%s/%s was already forgotten, re-marked at version %s",
                        plugin, kind, name or "-", row.version)
            return False
        row.deleted = True
        row.body = {}
        row.version = (row.version or 0) + 1
        row.origin = _origin()
        handle.commit()
        LOGGER.info("declaration %s/%s/%s forgotten at version %s",
                    plugin, kind, name or "-", row.version)
        return True
    except Exception:
        handle.rollback()
        raise
    finally:
        handle.close()


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
        # A marked row is the record of a deletion, not a document.
        return dict(row.body) if row is not None and not row.deleted else fallback
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
            # Writing a key again revives it: the marker is how a deletion travels, and it has
            # travelled by the time anyone writes here.
            row.deleted = False
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


def amend(plugin: str, kind: str, name: str, changes: dict, defaults: dict) -> dict:
    """
    Put `changes` over the stored document and write the result, as one step.

    Reading and writing in two sessions let a second caller land in between: both read one
    version, both wrote the whole document, the later one won, and the earlier caller was
    answered 200 with the value it sent rather than the value that was stored - after which
    the truncated document replicates like any other, so the peer agrees with it and nothing
    reports a conflict. The write is therefore conditional on the version that was read, and
    a row that moved underneath is read again and the changes put over what is there now.
    """
    handle = database.session()
    try:
        for _ in range(ROUNDS):
            # The round that lost has a transaction open on the snapshot it read, so without
            # ending it here every later round reads the same stale row and none can settle.
            handle.rollback()
            row = handle.scalar(
                select(models.Declaration).where(
                    models.Declaration.plugin == plugin,
                    models.Declaration.kind == kind,
                    models.Declaration.name == name))
            # A marked row is the record of a deletion, not a document; writing a key again
            # revives it, as the marker has travelled by the time anyone writes here.
            held = dict(row.body) if row is not None and not row.deleted else dict(defaults)
            held.update(changes or {})
            if row is None:
                version = 1
                try:
                    handle.add(models.Declaration(plugin=plugin, kind=kind, name=name,
                                                  body=held, origin=_origin()))
                    handle.commit()
                except IntegrityError:
                    # Somebody inserted this key between the read and here. The unique
                    # constraint is what makes that another round rather than a second row.
                    continue
            else:
                version = (row.version or 0) + 1
                settled = handle.execute(
                    update(models.Declaration)
                    .where(models.Declaration.id == row.id,
                           models.Declaration.version == row.version)
                    .values(body=held, deleted=False, version=version, origin=_origin()))
                if settled.rowcount != 1:
                    continue
                handle.commit()
            LOGGER.info("declaration %s/%s/%s written at version %s",
                        plugin, kind, name or "-", version)
            return held
        handle.rollback()
        LOGGER.error("declaration %s/%s/%s moved under every one of %s attempts to amend it, "
                     "so nothing was stored", plugin, kind, name or "-", ROUNDS)
        raise ServiceUnavailable(f"{plugin}/{kind}/{name or '-'} is being written faster than "
                                 f"it can be amended; nothing was stored, so try again")
    except Exception:
        handle.rollback()
        raise
    finally:
        handle.close()


def merge(plugin: str, kind: str, name: str, body: dict, defaults: dict) -> dict:
    """
    Change only the keys that were sent, leaving the rest as they were.
    """
    return amend(plugin, kind, name,
                 {key: value for key, value in (body or {}).items() if value is not None},
                 defaults)


def listing(plugin: str, kind: str = "") -> list:
    """
    Every document a plugin owns, optionally of one kind.
    """
    handle = database.session()
    try:
        query = select(models.Declaration).where(models.Declaration.plugin == plugin,
                                                 models.Declaration.deleted.is_(False))
        if kind:
            query = query.where(models.Declaration.kind == kind)
        return [{"kind": row.kind, "name": row.name, "version": row.version,
                 "body": dict(row.body)} for row in handle.scalars(query).all()]
    finally:
        handle.close()
