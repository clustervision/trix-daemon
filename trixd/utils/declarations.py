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

# How many times a change reads the row again after losing the version it wrote against. A
# round is only lost to a write that landed, so the bound is reached by a key being written
# faster than it can be read, not by a race. Past it the caller is refused: answering 200
# with a document that was never stored is the one outcome this must not produce.
ROUNDS: int = 5


def _origin() -> str:
    """
    Which controller wrote this, so a document can be traced to where it was changed.
    """
    return socket.gethostname().split(".")[0]


def _settle(plugin: str, kind: str, name: str, decide):
    """
    Read the document, let `decide` say what it should become, and write that conditional on
    the version that was read.

    Every change to a document goes through here, so the precondition cannot be on one verb
    and absent from the next. Read and write in two steps and a second caller lands in
    between: both read one version, both wrote a whole document, the later one won, and the
    caller that lost was answered 200 with a document that was never stored - after which
    the surviving document replicates like any other, so the peer agrees with it and nothing
    reports a conflict. Written against the version that was read, a row that moved
    underneath is read again and `decide` asked over what is there now.

    `decide` is handed the row as it stands, or None, and answers the body to store, whether
    the document is now deleted, and what the caller is told. None means there is nothing to
    write. Answers the version written and that answer, or None.
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
            decided = decide(row)
            if decided is None:
                return None
            body, deleted, answer = decided
            if row is None:
                try:
                    handle.add(models.Declaration(plugin=plugin, kind=kind, name=name,
                                                  body=body, deleted=deleted,
                                                  origin=_origin()))
                    handle.commit()
                except IntegrityError:
                    # Somebody inserted this key between the read and here. The unique
                    # constraint is what makes that another round rather than a second row.
                    continue
                return 1, answer
            version = (row.version or 0) + 1
            settled = handle.execute(
                update(models.Declaration)
                .where(models.Declaration.id == row.id,
                       models.Declaration.version == row.version)
                .values(body=body, deleted=deleted, version=version, origin=_origin()))
            if settled.rowcount != 1:
                continue
            handle.commit()
            return version, answer
        handle.rollback()
        LOGGER.error("declaration %s/%s/%s moved under every one of %s attempts to change "
                     "it, so nothing was stored", plugin, kind, name or "-", ROUNDS)
        # Typed, and 5xx on purpose. A bare exception never reaches the TrixdError handler,
        # so the caller would be told nothing at all; and the outbox keeps a 5xx queued
        # while it makes any other 4xx terminal, so a conflict here would lose a replicated
        # write on the peer for good over contention a retry settles.
        raise ServiceUnavailable(f"{plugin}/{kind}/{name or '-'} is being written faster "
                                 f"than it can be changed; nothing was stored, so try again")
    except Exception:
        handle.rollback()
        raise
    finally:
        handle.close()


def forget(plugin: str, kind: str, name: str) -> bool:
    """
    Mark one document deleted, so the deletion reaches the peer instead of being undone by it.

    The row stays: the sweep re-adds a row that is missing here and present there, so a real
    delete comes back within the interval. Marked, it counts as no row on both.
    """
    def decide(row):
        """
        A live row is marked; one already marked is re-marked so the delete can be restated.
        """
        if row is None:
            return None
        # Marked here already, but the peer may still hold a live row at this version.
        # Bumping is what lets the delete, issued again, settle that; a silent no-op leaves
        # the operator no way to break the tie.
        return {}, True, not row.deleted

    settled = _settle(plugin, kind, name, decide)
    if settled is None:
        return False
    version, gone = settled
    if gone:
        LOGGER.info("declaration %s/%s/%s forgotten at version %s",
                    plugin, kind, name or "-", version)
    else:
        LOGGER.info("declaration %s/%s/%s was already forgotten, re-marked at version %s",
                    plugin, kind, name or "-", version)
    return gone


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
    def decide(_row):
        """
        The whole document, whatever is held. Writing a key again revives it: the marker is
        how a deletion travels, and it has travelled by the time anyone writes here.
        """
        return dict(body), False, dict(body)

    version, stored = _settle(plugin, kind, name, decide)
    LOGGER.info("declaration %s/%s/%s written at version %s",
                plugin, kind, name or "-", version)
    return stored


def amend(plugin: str, kind: str, name: str, changes: dict, defaults: dict) -> dict:
    """
    Put `changes` over the stored document and write the result, as one step.

    Taken separately the read and the write lose one of two callers; what makes them one
    step is `_settle`, which writes against the version it read.
    """
    def decide(row):
        """
        What is held with `changes` over it, or the defaults where nothing is held.
        """
        # A marked row is the record of a deletion, not a document; writing a key again
        # revives it, as the marker has travelled by the time anyone writes here.
        held = dict(row.body) if row is not None and not row.deleted else dict(defaults)
        held.update(changes or {})
        return held, False, held

    version, held = _settle(plugin, kind, name, decide)
    LOGGER.info("declaration %s/%s/%s written at version %s",
                plugin, kind, name or "-", version)
    return held


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
