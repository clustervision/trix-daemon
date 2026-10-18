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
A queue for work that outlives its request, and the state a caller polls for.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from trixd.common import logging as trixlog
from trixd.db import engine as database
from trixd.db import models

LOGGER = trixlog.get("tasks")

QUEUED: str = "queued"
RUNNING: str = "running"
DONE: str = "done"
FAILED: str = "failed"

# How long a finished row is kept, so a caller that polls slowly still sees it.
KEEP_SECONDS: int = 3600

_work: queue.Queue = queue.Queue()
_worker: threading.Thread = None
_handlers: dict = {}


def request_id() -> str:
    """
    Unique across controllers and restarts, and readable in a log.
    """
    return f"{int(time.time())}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def register(subsystem: str, handler) -> None:
    """
    What runs a task of this kind. A plugin registers when it loads.
    """
    _handlers[subsystem] = handler


def unregister(subsystem: str) -> bool:
    """
    Take back what runs a task of this kind, for when its plugin is unplugged.

    Without this a disabled plugin still answered for its subsystem: a notify naming one
    of its events was accepted instead of refused, and dispatched into code that had been
    unplugged, taking the render lock on the way.
    """
    return _handlers.pop(subsystem, None) is not None


def handler(subsystem: str):
    """
    What runs a task of this kind, or None when nothing registered for it. A caller
    reaches a plugin's own dispatcher by name, without knowing which plugin it is.
    """
    return _handlers.get(subsystem)


def submit(subsystem: str, task: str, param: str = "", principal: str = "") -> str:
    """
    Take the work, write it down, answer at once. The caller polls the id.
    """
    identifier = request_id()
    handle = database.session()
    try:
        handle.add(models.Task(request_id=identifier, principal=principal,
                               subsystem=subsystem, task=task, param=param,
                               state=QUEUED))
        handle.commit()
    finally:
        handle.close()
    _work.put(identifier)
    LOGGER.info("queued %s %s %s as %s", subsystem, task, param, identifier)
    return identifier


def status(identifier: str) -> dict:
    """
    Where one request got to, or None when nothing knows that id.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.Task)
                            .where(models.Task.request_id == identifier))
        if row is None:
            return None
        return {"request_id": row.request_id, "subsystem": row.subsystem,
                "task": row.task, "param": row.param, "state": row.state,
                "message": row.message, "created": row.created.isoformat(),
                "finished": row.finished.isoformat() if row.finished else None}
    finally:
        handle.close()


def _run_one(identifier: str) -> None:
    """
    Mark it running, do it, write down what happened either way.
    """
    try:
        handle = database.session()
    except Exception as error:
        # Outside the try that was meant to hold it, this was the one line in here that
        # could reach the worker loop, which had nothing to catch it. The engine's own
        # note-keeping guards the same call for the same reason.
        LOGGER.error("could not take a session to run task %s: %s", identifier, error)
        return
    try:
        row = handle.scalar(select(models.Task)
                            .where(models.Task.request_id == identifier))
        if row is None:
            return
        handler = _handlers.get(row.subsystem)
        row.state = RUNNING
        handle.commit()

        if handler is None:
            outcome, message = FAILED, f"nothing handles {row.subsystem}"
        else:
            try:
                message = handler(row.task, row.param) or "done"
                outcome = DONE
            except Exception as error:
                outcome, message = FAILED, str(error)
                LOGGER.error("task %s failed: %s", identifier, error)

        row.state = outcome
        row.message = str(message)[:2048]
        row.finished = datetime.now(timezone.utc).replace(tzinfo=None)
        handle.commit()
    except Exception as error:
        handle.rollback()
        LOGGER.error("could not run task %s: %s", identifier, error)
    finally:
        handle.close()


def _loop() -> None:
    """
    One worker, taking one request at a time, for the life of the daemon.
    """
    while True:
        identifier = _work.get()
        try:
            _run_one(identifier)
            _prune()
        except Exception as error:
            # This thread is the only worker and nothing starts a second one, so anything
            # reaching here used to end every queued action for the life of the daemon:
            # submit went on answering `queued`, nothing ran it, and the only trace was
            # the interpreter's own excepthook rather than a line from this daemon.
            LOGGER.error("the task worker could not finish %s: %s", identifier, error)
        finally:
            # Whether it worked or not: a get that never answers leaves anything waiting on
            # the queue waiting for good.
            _work.task_done()


def _prune() -> None:
    """
    Forget finished tasks older than KEEP_SECONDS, so the table stays the size of a day.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=KEEP_SECONDS)
    try:
        handle = database.session()
    except Exception as error:
        LOGGER.error("could not take a session to prune finished tasks: %s", error)
        return
    try:
        handle.execute(delete(models.Task).where(models.Task.state.in_((DONE, FAILED)),
                                                  models.Task.finished < cutoff))
        handle.commit()
    except Exception as error:
        handle.rollback()
        LOGGER.error("could not prune finished tasks: %s", error)
    finally:
        handle.close()


def start() -> None:
    """
    Bring the worker up, and requeue anything a restart left behind.
    """
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _requeue()
    _prune()
    _worker = threading.Thread(target=_loop, name="trixd-tasks", daemon=True)
    _worker.start()
    LOGGER.info("task worker started")


def _requeue() -> None:
    """
    A row left queued or running is work this daemon accepted and did not finish.
    """
    try:
        handle = database.session()
    except Exception as error:
        LOGGER.error("could not take a session to requeue interrupted tasks: %s", error)
        return
    try:
        rows = handle.scalars(select(models.Task).where(
            models.Task.state.in_((QUEUED, RUNNING)))).all()
        for row in rows:
            row.state = QUEUED
            _work.put(row.request_id)
        if rows:
            LOGGER.warning("requeued %d task(s) a restart interrupted", len(rows))
        handle.commit()
    except Exception as error:
        handle.rollback()
        LOGGER.error("could not requeue: %s", error)
    finally:
        handle.close()


def drain(timeout: float = 5.0) -> bool:
    """
    Wait up to timeout for every queued task to finish; a test's helper. True if they did.
    """
    deadline = time.monotonic() + timeout
    while _work.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _work.unfinished_tasks
