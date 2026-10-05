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
The replicated tables as documents, addressed by what both controllers share.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import hashlib
from datetime import datetime, timezone
from json import dumps

from sqlalchemy import select
from sqlalchemy.orm import Session

from trix.common import logging as trixlog
from trix.common.constants import ROLE_NONE
from trix.db import models

LOGGER = trixlog.get("replication")

# Autoincrement is local: the peer inserts its own row and gets its own number, so
# a document is addressed by the columns the request was made with and never by id.
ORDER: tuple = ("principal", "declaration")

MODELS: dict = {
    "principal": models.Principal,
    "declaration": models.Declaration,
}


def _stamp(value) -> str:
    """
    The version and time a row was last written.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


def _common(row) -> dict:
    """
    The columns every replicated document carries.
    """
    return {"version": row.version or 1, "origin": row.origin or "",
            "updated_at": _stamp(row.updated_at)}


def to_document(handle: Session, table: str, row) -> dict:
    """
    One row as a document keyed by names rather than by identifiers.
    """
    body = dict(_common(row))
    if table == "principal":
        body.update(key=[row.name], name=row.name, password_digest=row.password_digest,
                    enabled=bool(row.enabled), role=row.role)
    elif table == "declaration":
        body.update(key=[row.plugin, row.kind, row.name], plugin=row.plugin,
                    kind=row.kind, name=row.name, body=dict(row.body or {}))
    return body


def export_table(handle: Session, table: str) -> list:
    """
    Every row of one table, in a deterministic order.
    """
    model = MODELS[table]
    documents = [to_document(handle, table, row)
                 for row in handle.scalars(select(model)).all()]
    return sorted(documents, key=lambda item: [str(part) for part in item["key"]])


def table_hash(handle: Session, table: str) -> str:
    """
    A digest of the table's contents, for deciding whether to copy anything.
    """
    return hashlib.sha256(dumps(export_table(handle, table), sort_keys=True).encode()).hexdigest()


def hashes(handle: Session) -> dict:
    """
    One hash per replicated table.
    """
    return {table: table_hash(handle, table) for table in ORDER}


def _find(handle: Session, table: str, key: list):
    """
    The local row a document addresses, or None.
    """
    model = MODELS[table]
    if table == "principal":
        return handle.scalar(select(model).where(model.name == key[0]))
    return handle.scalar(select(model).where(model.plugin == key[0], model.kind == key[1],
                                             model.name == key[2]))


def _columns(handle: Session, table: str, document: dict) -> dict:
    """
    The column values for a document.
    """
    key = document["key"]
    if table == "principal":
        return {"name": key[0],
                "password_digest": document.get("password_digest", ""),
                "enabled": bool(document.get("enabled", True)),
                "role": document.get("role", ROLE_NONE)}
    return {"plugin": key[0], "kind": key[1], "name": key[2],
            "body": dict(document.get("body") or {})}


def _newer(incoming: dict, row) -> bool:
    """
    Last writer wins, by version first and then by the timestamp.
    """
    theirs = int(incoming.get("version") or 1)
    ours = int(row.version or 1)
    if theirs != ours:
        return theirs > ours
    return str(incoming.get("updated_at") or "") > _stamp(row.updated_at)


def merge_table(handle: Session, table: str, documents: list) -> dict:
    """
    Take whatever the peer holds that is newer than ours. Same version and same
    timestamp with different content keeps the local row and is logged.
    """
    model = MODELS[table]
    added = updated = conflicts = 0
    for document in documents:
        columns = _columns(handle, table, document)
        row = _find(handle, table, document["key"])
        if row is None:
            handle.add(model(version=int(document.get("version") or 1),
                             origin=document.get("origin", ""), **columns))
            added += 1
            continue
        if _newer(document, row):
            for column, value in columns.items():
                setattr(row, column, value)
            row.version = int(document.get("version") or 1)
            row.origin = document.get("origin", "")
            updated += 1
        elif (int(document.get("version") or 1) == int(row.version or 1)
              and str(document.get("updated_at") or "") == _stamp(row.updated_at)
              and document != to_document(handle, table, row)):
            conflicts += 1
            LOGGER.warning("conflict on %s %s: same version and time, different content; "
                           "keeping the local row", table, document["key"])
    handle.commit()
    return {"added": added, "updated": updated, "conflicts": conflicts}
