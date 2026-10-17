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
Writing the audit trail. Refusals are recorded too, or it answers the wrong question.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from trixd.common import logging as trixlog
from trixd.common.settings import current as settings
from trixd.db import engine as database
from trixd.db import models

LOGGER = trixlog.get("audit")

GRANTED: str = "granted"
REFUSED: str = "refused"

DETAIL_LIMIT: int = 1024
# How wide each text column actually is, read from the model rather than repeated here. A
# value wider than its column is not a caller's mistake to pass on: on an engine that
# enforces the width the insert raises, and because rows are written in batches of up to
# LIMIT, one oversized row takes every unrelated row in that batch with it. `principal` and
# `object` come from the request, so an unauthenticated caller decides their length.
_WIDTHS: dict = {name: column.type.length
                 for name, column in models.Audit.__table__.columns.items()
                 if getattr(column.type, "length", None)}


def _fits(field: str, value: str) -> str:
    """
    The value, cut to the width of the column it goes in.
    """
    limit = _WIDTHS.get(field)
    return value if limit is None or len(value) <= limit else value[:limit]
# One commit per batch rather than per request: the cost a busy daemon would otherwise pay.
BATCH_ROWS: int = 500
BATCH_WAIT: float = 0.5
PRUNE_EVERY: int = 3600
READ_LIMIT: int = 1000

_queue: queue.Queue = queue.Queue()
_writer: threading.Thread = None
_stop = threading.Event()


def _row(entry: dict) -> models.Audit:
    """
    One audit row from a queued entry.
    """
    return models.Audit(**entry)


def _entry(principal: str, action: str, object_name: str, outcome: str, detail: str) -> dict:
    """
    What a row holds, captured when it happened.
    """
    return {"at": datetime.now(timezone.utc).replace(tzinfo=None),
            "principal": _fits("principal", principal or "anonymous"),
            "action": _fits("action", action),
            "object": _fits("object", object_name or ""),
            "outcome": _fits("outcome", outcome),
            "detail": (detail or "")[:DETAIL_LIMIT]}


def _write(entries: list) -> None:
    """
    Commit a batch in one session; a failure is logged, never raised into a request.
    """
    own = database.session()
    try:
        own.add_all([_row(entry) for entry in entries])
        own.commit()
    except Exception as error:
        own.rollback()
        LOGGER.error("could not record %d audit row(s): %s", len(entries), error)
    finally:
        own.close()


def record(principal: str, action: str, object_name: str = "",
           outcome: str = GRANTED, detail: str = "", handle=None) -> None:
    """
    One row. With the request's own session it commits with the change it records;
    otherwise it is queued and written in a batch.
    """
    entry = _entry(principal, action, object_name, outcome, detail)
    if handle is not None:
        handle.add(_row(entry))
        return
    if _writer is not None and _writer.is_alive():
        _queue.put(entry)
        return
    _write([entry])


def granted(principal: str, action: str, object_name: str = "", detail: str = "",
            handle=None) -> None:
    """
    Something a caller was allowed to do.
    """
    record(principal, action, object_name, GRANTED, detail, handle)


def refused(principal: str, action: str, object_name: str = "", detail: str = "",
            handle=None) -> None:
    """
    A refusal never rides the request's session: that request is about to roll back.
    """
    record(principal, action, object_name, REFUSED, detail)


def prune() -> int:
    """
    Forget rows older than [AUDIT] KEEP.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=settings().audit.keep)
    handle = database.session()
    try:
        removed = handle.execute(delete(models.Audit).where(models.Audit.at < cutoff)).rowcount
        handle.commit()
    except Exception as error:
        handle.rollback()
        LOGGER.error("could not prune the audit trail: %s", error)
        return 0
    finally:
        handle.close()
    if removed:
        LOGGER.info("pruned %d audit row(s) older than %ss", removed, settings().audit.keep)
    return removed or 0


def _loop() -> None:
    """
    Gather rows for up to BATCH_WAIT, write them together, and prune once an hour.
    """
    pruned = 0.0
    while True:
        try:
            first = _queue.get(timeout=BATCH_WAIT)
        except queue.Empty:
            first = None
        batch = [] if first is None else [first]
        while first is not None and len(batch) < BATCH_ROWS:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            _write(batch)
            for _ in batch:
                _queue.task_done()
        if time.monotonic() - pruned > PRUNE_EVERY:
            prune()
            pruned = time.monotonic()
        if _stop.is_set() and _queue.empty():
            return


def start() -> None:
    """
    Start the batch writer.
    """
    global _writer
    if _writer is not None and _writer.is_alive():
        return
    _stop.clear()
    _writer = threading.Thread(target=_loop, name="trixd-audit", daemon=True)
    _writer.start()


def flush(timeout: float = 5.0) -> bool:
    """
    Wait until every queued row is written. True if it was.
    """
    deadline = time.monotonic() + timeout
    while _queue.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.02)
    return not _queue.unfinished_tasks


def shutdown() -> None:
    """
    Write what is queued and stop.
    """
    _stop.set()
    flush()


def read(principal: str = "", outcome: str = "", since: datetime = None,
         limit: int = 100) -> list:
    """
    The newest rows first, filtered, for GET /api/v1/system/audit.
    """
    flush(1.0)
    query = select(models.Audit).order_by(models.Audit.id.desc())
    if principal:
        query = query.where(models.Audit.principal == principal)
    if outcome:
        query = query.where(models.Audit.outcome == outcome)
    if since is not None:
        query = query.where(models.Audit.at >= since)
    handle = database.session()
    try:
        rows = handle.scalars(query.limit(max(1, min(int(limit), READ_LIMIT)))).all()
        return [{"at": row.at.replace(tzinfo=timezone.utc).isoformat(), "principal": row.principal,
                 "action": row.action, "object": row.object, "outcome": row.outcome,
                 "detail": row.detail} for row in rows]
    finally:
        handle.close()
