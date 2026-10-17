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

from trixd.common import logging as trixlog
from trixd.common.constants import ROLE_NONE, ROLES
from trixd.core.render.engine import current as renders
from trixd.core.security import principals
from trixd.db import models

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


def _unstamp(text: str):
    """
    A document's timestamp as the naive UTC time the column stores, or None.
    """
    try:
        value = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


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
                    enabled=bool(row.enabled), role=row.role, deleted=bool(row.deleted))
    elif table == "declaration":
        body.update(key=[row.plugin, row.kind, row.name], plugin=row.plugin,
                    kind=row.kind, name=row.name, body=dict(row.body or {}),
                    deleted=bool(row.deleted))
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


def _role_of(document: dict) -> str:
    """
    The role a peer's document names, where it is one this daemon would grant.

    The API refuses a role off the ladder, and the ladder deliberately excludes rootus: that
    is the ini account, not a role anyone is given. The merge took the string as it came, so
    a peer could write a value no route here accepts - and rootus outranks manager.
    """
    named = document.get("role", ROLE_NONE) or ROLE_NONE
    if named in ROLES:
        return named
    LOGGER.warning("a peer named role %r for %s, which is not a role this daemon grants; "
                   "recorded as %s", named, document.get("key"), ROLE_NONE)
    return ROLE_NONE


def _columns(handle: Session, table: str, document: dict) -> dict:
    """
    The column values for a document.
    """
    key = document["key"]
    if table == "principal":
        return {"name": key[0],
                "password_digest": document.get("password_digest", ""),
                "enabled": bool(document.get("enabled", True)),
                "role": _role_of(document),
                "deleted": bool(document.get("deleted", False))}
    return {"plugin": key[0], "kind": key[1], "name": key[2],
            "body": dict(document.get("body") or {}),
            "deleted": bool(document.get("deleted", False))}


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
    touched = set()
    for document in documents:
        columns = _columns(handle, table, document)
        # The peer's timestamp travels with the row; stamped afresh here, the two copies
        # would never hash the same and each pass would copy the row back again.
        stamped = _unstamp(document.get("updated_at"))
        if stamped is not None:
            columns["updated_at"] = stamped
        row = _find(handle, table, document["key"])
        if row is None:
            handle.add(model(version=int(document.get("version") or 1),
                             origin=document.get("origin", ""), **columns))
            added += 1
            touched.add(tuple(document["key"][:2]))
            continue
        if _newer(document, row):
            for column, value in columns.items():
                setattr(row, column, value)
            row.version = int(document.get("version") or 1)
            row.origin = document.get("origin", "")
            updated += 1
            touched.add(tuple(document["key"][:2]))
        elif (int(document.get("version") or 1) == int(row.version or 1)
              and str(document.get("updated_at") or "") == _stamp(row.updated_at)
              and document != to_document(handle, table, row)):
            conflicts += 1
            LOGGER.warning("conflict on %s %s: same version and time, different content; "
                           "keeping the local row", table, document["key"])
    handle.commit()
    if table == "principal" and (added or updated):
        # A role or a disable from the peer applies on the next request, not after the cache.
        principals.CACHE.clear()
    if table == "declaration":
        # A plugin's own settings arriving from the peer wake the renders that read them,
        # as a change made here would.
        for plugin, kind in sorted(touched):
            renders().notify(f"{plugin}.{kind}")
    return {"added": added, "updated": updated, "conflicts": conflicts}
