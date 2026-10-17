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
from sqlalchemy.orm.attributes import flag_modified

from trixd.common import logging as trixlog
from trixd.common.constants import ROLE_NONE, ROLES
from trixd.core.render.engine import current as renders
from trixd.core.security import principals
from trixd.db import models

LOGGER = trixlog.get("replication")

# Autoincrement is local: the peer inserts its own row and gets its own number, so
# a document is addressed by the columns the request was made with and never by id.
#
# Both are derived from models.REPLICATED, which sits beside models.LOCAL and is the one
# place a table is said to replicate. A tuple here and a mapping beside it were two more
# hand-kept lists of the same thing, and a table left out of one of them is skipped
# silently - the pass still reports clean, so the first sign is a pair already diverged.


def _models() -> dict:
    """
    The model class behind each replicated table, found by the table name it declares.
    """
    classes = {mapper.class_.__tablename__: mapper.class_
               for mapper in models.Base.registry.mappers}
    unknown = [name for name in models.REPLICATED if name not in classes]
    if unknown:
        raise RuntimeError(f"REPLICATED names {', '.join(unknown)}, which no model declares")
    return {name: classes[name] for name in models.REPLICATED}


ORDER: tuple = tuple(models.REPLICATED)
MODELS: dict = _models()


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


def _apply(row, columns: dict, document: dict) -> None:
    """
    Write a peer's document onto a local row.

    `updated_at` is forced into the statement. Assigning a column the value it already holds
    leaves it clean, so it is left out of the UPDATE and the column's `onupdate` stamps it
    with now instead - and the peer's timestamp has to travel with the row, or the two
    copies never hash the same and every pass copies the row back again. That is the
    ping-pong the stamp exists to prevent, arriving through the one case where the two
    timestamps already agree.
    """
    for column, value in columns.items():
        setattr(row, column, value)
    row.version = int(document.get("version") or 1)
    row.origin = document.get("origin", "")
    flag_modified(row, "updated_at")


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


def _digest(document: dict) -> str:
    """
    A document as one comparable string, for deciding a tie that nothing else can.
    """
    return hashlib.sha256(dumps(document, sort_keys=True, default=str).encode()).hexdigest()


def _wins_tie(table: str, incoming: dict, ours: dict) -> bool:
    """
    Which of two documents with the same version and the same timestamp is kept.

    Both controllers run this over the same pair of documents and must reach the same
    answer, or each keeps its own and the pair never converges - which is what keeping the
    local row always did.

    So a document this daemon would not write as it stands loses, whichever side it is on.
    Vetoing only the *incoming* one read as safe and was not: the controller holding the
    unsound row kept it either way, while the other took or kept by origin, so the tie
    resolved differently on the two and the pair stayed apart - decided by nothing but
    which controller's name sorts higher.

    Otherwise the origin decides, being the field that differs by definition; where even
    that is equal, a digest of the document does, so there is always an answer.
    """
    if _writable(table, incoming) != _writable(table, ours):
        return _writable(table, incoming)
    mine = str(ours.get("origin") or "")
    theirs = str(incoming.get("origin") or "")
    if theirs != mine:
        return theirs > mine
    return _digest(incoming) > _digest(ours)


def _writable(table: str, document: dict) -> bool:
    """
    Whether a document is one this daemon would write down as it stands.

    The merge writes `_columns`, which is not the document: a role off the ladder is written
    as none, so the row would still differ from the document at the same version and time -
    a tie remade on every pass, with the same write and the same claim in the log each time.
    """
    return not (table == "principal"
                and (document.get("role") or ROLE_NONE) not in ROLES)


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
            _apply(row, columns, document)
            updated += 1
            touched.add(tuple(document["key"][:2]))
        elif (int(document.get("version") or 1) == int(row.version or 1)
              and str(document.get("updated_at") or "") == _stamp(row.updated_at)
              and document != to_document(handle, table, row)):
            conflicts += 1
            mine = to_document(handle, table, row)
            if set(document) != set(mine):
                # One build carries a field the other does not, which only a pair part way
                # through an upgrade can produce. Neither side is newer, so nothing here can
                # settle it - taking the shorter document would write the field it lacks
                # back to its default, and the build without the field cannot write it at
                # all, so no choice converges. The local row stands and the pair is left
                # visibly apart: the sweep counts it and health says not_converging.
                LOGGER.error("conflict on %s %s: the peer's document carries %s and ours "
                             "%s, so neither side can settle it; the local row stands and "
                             "the pair stays apart until one of them is written again",
                             table, document["key"], sorted(document), sorted(mine))
                continue
            if not _wins_tie(table, document, mine):
                LOGGER.warning("conflict on %s %s: same version and time, different "
                               "content; the local row wins the tie", table, document["key"])
                continue
            # Nothing else can separate them, and keeping the local row on both controllers
            # left the two copies different for ever: neither side is newer, so neither
            # takes the other's and no later pass resolves it.
            _apply(row, columns, document)
            updated += 1
            touched.add(tuple(document["key"][:2]))
            LOGGER.warning("conflict on %s %s: same version and time, different content; "
                           "taking the peer's, which both controllers decide the same way",
                           table, document["key"])
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
