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
from trix.db import models

LOGGER = trixlog.get("replication")

# Autoincrement is local: the peer inserts its own row and gets its own number, so
# a document is addressed by the columns the request was made with and never by id.
ORDER: tuple = ("principal", "usergroup", "level", "membership", "level_capability",
                "grant", "permission", "declaration")

MODELS: dict = {
    "principal": models.Principal,
    "usergroup": models.Usergroup,
    "level": models.Level,
    "membership": models.Membership,
    "level_capability": models.LevelCapability,
    "grant": models.Grant,
    "permission": models.Permission,
    "declaration": models.Declaration,
}


def _stamp(value) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


def _name_of(handle: Session, model, row_id):
    if not row_id:
        return ""
    row = handle.get(model, row_id)
    return row.name if row is not None else ""


def _id_of(handle: Session, model, name: str):
    if not name:
        return None
    return handle.scalar(select(model.id).where(model.name == name))


def _common(row) -> dict:
    return {"version": row.version or 1, "origin": row.origin or "",
            "updated_at": _stamp(row.updated_at)}


def to_document(handle: Session, table: str, row) -> dict:
    """
    One row as a document keyed by names rather than by identifiers.
    """
    body = dict(_common(row))
    if table == "principal":
        body.update(key=[row.name], name=row.name, kind=row.kind, source=row.source,
                    password_digest=row.password_digest, enabled=bool(row.enabled))
    elif table in ("usergroup", "level"):
        body.update(key=[row.name], name=row.name)
    elif table == "membership":
        body.update(key=[_name_of(handle, models.Principal, row.principal_id),
                         _name_of(handle, models.Usergroup, row.usergroup_id)])
    elif table == "level_capability":
        body.update(key=[_name_of(handle, models.Level, row.level_id), row.capability],
                    capability=row.capability)
    elif table == "grant":
        body.update(key=[_name_of(handle, models.Principal, row.principal_id),
                         _name_of(handle, models.Usergroup, row.usergroup_id),
                         _name_of(handle, models.Level, row.level_id), row.scope or ""],
                    scope=row.scope or "")
    elif table == "permission":
        body.update(key=[row.object_kind, row.object_name],
                    object_kind=row.object_kind, object_name=row.object_name,
                    owner=_name_of(handle, models.Principal, row.owner_id),
                    usergroup=_name_of(handle, models.Usergroup, row.usergroup_id),
                    owner_bits=row.owner_bits, group_bits=row.group_bits,
                    other_bits=row.other_bits)
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
    model = MODELS[table]
    if table in ("principal", "usergroup", "level"):
        return handle.scalar(select(model).where(model.name == key[0]))
    if table == "membership":
        principal = _id_of(handle, models.Principal, key[0])
        usergroup = _id_of(handle, models.Usergroup, key[1])
        if not principal or not usergroup:
            return None
        return handle.scalar(select(model).where(model.principal_id == principal,
                                                 model.usergroup_id == usergroup))
    if table == "level_capability":
        level = _id_of(handle, models.Level, key[0])
        if not level:
            return None
        return handle.scalar(select(model).where(model.level_id == level,
                                                 model.capability == key[1]))
    if table == "grant":
        return handle.scalar(select(model).where(
            model.principal_id == _id_of(handle, models.Principal, key[0]),
            model.usergroup_id == _id_of(handle, models.Usergroup, key[1]),
            model.level_id == _id_of(handle, models.Level, key[2]),
            model.scope == (key[3] or "")))
    if table == "permission":
        return handle.scalar(select(model).where(model.object_kind == key[0],
                                                 model.object_name == key[1]))
    return handle.scalar(select(model).where(model.plugin == key[0], model.kind == key[1],
                                             model.name == key[2]))


def _columns(handle: Session, table: str, document: dict) -> dict:
    """
    The column values for a document, every name resolved to a local identifier.
    None means a row it depends on has not arrived, so the next pass retries it.
    """
    key = document["key"]
    if table == "principal":
        return {"name": key[0], "kind": document.get("kind", "user"),
                "source": document.get("source", "local"),
                "password_digest": document.get("password_digest", ""),
                "enabled": bool(document.get("enabled", True))}
    if table in ("usergroup", "level"):
        return {"name": key[0]}
    if table == "membership":
        principal = _id_of(handle, models.Principal, key[0])
        usergroup = _id_of(handle, models.Usergroup, key[1])
        if not principal or not usergroup:
            return None
        return {"principal_id": principal, "usergroup_id": usergroup}
    if table == "level_capability":
        level = _id_of(handle, models.Level, key[0])
        if not level:
            return None
        return {"level_id": level, "capability": key[1]}
    if table == "grant":
        level = _id_of(handle, models.Level, key[2])
        if not level:
            return None
        return {"principal_id": _id_of(handle, models.Principal, key[0]),
                "usergroup_id": _id_of(handle, models.Usergroup, key[1]),
                "level_id": level, "scope": key[3] or ""}
    if table == "permission":
        return {"object_kind": key[0], "object_name": key[1],
                "owner_id": _id_of(handle, models.Principal, document.get("owner", "")),
                "usergroup_id": _id_of(handle, models.Usergroup, document.get("usergroup", "")),
                "owner_bits": document.get("owner_bits", "rwu"),
                "group_bits": document.get("group_bits", "r"),
                "other_bits": document.get("other_bits", "")}
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
    added = updated = conflicts = deferred = 0
    for document in documents:
        columns = _columns(handle, table, document)
        if columns is None:
            deferred += 1
            continue
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
    return {"added": added, "updated": updated, "conflicts": conflicts,
            "deferred": deferred}
