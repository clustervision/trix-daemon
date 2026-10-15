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
What this controller owes its peers: one row per action per peer, tried at once and
retried by the sweep.

A call to an endpoint the manifest marks `replicate` is written down here before it is
sent, so a peer that is away costs latency rather than divergence. Nothing in this module
runs on a timer: the replication sweep drains what is left over, which is also the only
thing that closes a peer's breaker.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import hashlib
import json
import threading
import time
from datetime import datetime, timezone

import requests
from sqlalchemy import delete, select

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, TOKEN_HEADER
from trixd.common.settings import current as settings
from trixd.core.replication.sweep import trust
from trixd.core.replication.sweep import _token as peer_token
from trixd.db import engine as database
from trixd.db import models

LOGGER = trixlog.get("outbox")

QUEUED: str = "queued"
DONE: str = "done"
FAILED: str = "failed"

# The caller is waiting on this, so it is seconds rather than the sweep's thirty: the row
# makes giving up free, and a dark peer must not hold a request thread.
TIMEOUT: int = 3
# After this many sweep passes a row stops riding the sweep. A row retried for ever is a
# divergence nobody is told about.
MAX_ATTEMPTS: int = 12
# How long a finished row is kept, so a caller that polls slowly still sees it.
KEEP_SECONDS: int = 3600

# Consecutive transport failures at a peer, and whether its breaker is open. Process-local
# on purpose: one worker means this is controller-local, the breaker is this process's
# observation of its own socket rather than a fact worth replicating, and a row would put a
# write on every failure and a read on every call on the hot path.
BREAK_AFTER: int = 3
_failures: dict = {}
_open: dict = {}
_lock = threading.Lock()


def target_of(variables: dict, body) -> str:
    """
    What makes two calls the same intent: the path variables, then a digest of the body.

    Two writes for one node collapse to the newest; a rename never collapses, because the
    name it is coming from is in the body and so in the digest.
    """
    named = ":".join(f"{key}={variables[key]}" for key in sorted(variables or {}))
    if body in (None, {}, ""):
        return named[:255]
    digest = hashlib.sha256(json.dumps(body, sort_keys=True,
                                       default=str).encode()).hexdigest()[:16]
    return f"{named}|{digest}"[:255] if named else digest


def peers() -> list:
    """
    The peers to replicate to, which are the peers the sweep reconciles with.
    """
    config = settings().replication
    return list(config.peers) if config.enabled else []


def breaker_open(peer: str) -> bool:
    """
    Whether this peer is currently believed to be down.
    """
    with _lock:
        return peer in _open


def record_failure(peer: str) -> None:
    """
    One transport failure. The third in a row opens the breaker, so a storm against a dark
    peer costs three slow calls rather than one per node.
    """
    with _lock:
        _failures[peer] = _failures.get(peer, 0) + 1
        if _failures[peer] >= BREAK_AFTER and peer not in _open:
            _open[peer] = time.time()
            LOGGER.warning("%s has failed %d times in a row: not attempting replication to "
                           "it until a sweep pass reaches it", peer, _failures[peer])


def record_success(peer: str) -> None:
    """
    The peer answered. Only this closes a breaker, and the sweep is the only prober.
    """
    with _lock:
        was_open = _open.pop(peer, None)
        _failures.pop(peer, None)
    if was_open is not None:
        LOGGER.warning("%s is answering again after %.0fs: replicating to it once more",
                       peer, time.time() - was_open)


def states() -> dict:
    """
    What the health endpoint publishes: per peer, whether its breaker is open and how many
    actions are waiting for it.
    """
    handle = database.session()
    try:
        waiting = {}
        for row in handle.scalars(select(models.Outbox)
                                  .where(models.Outbox.state == QUEUED)).all():
            waiting[row.peer] = waiting.get(row.peer, 0) + 1
        failed = len(handle.scalars(select(models.Outbox)
                                    .where(models.Outbox.state == FAILED)).all())
    finally:
        handle.close()
    with _lock:
        down = dict(_open)
    return {peer: {"answering": peer not in down, "pending": waiting.get(peer, 0)}
            for peer in set(peers()) | set(waiting) | set(down)} | {"failed": failed}


def submit(peer: str, plugin: str, path: str, method: str, variables: dict, body,
           request_id: str, origin: str) -> int:
    """
    Write down one action for one peer, replacing whatever was pending for the same target.

    The replacement is the point: the newest intent is the only one worth sending, and a
    stale row applied late is how a retry undoes newer state.
    """
    mark = target_of(variables, body)
    payload = json.dumps({"variables": variables or {}, "body": body})
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(
            models.Outbox.peer == peer, models.Outbox.plugin == plugin,
            models.Outbox.path == path, models.Outbox.method == method,
            models.Outbox.target == mark))
        if row is None:
            row = models.Outbox(peer=peer, plugin=plugin, path=path, method=method,
                                target=mark)
            handle.add(row)
        row.request_id = request_id
        row.payload = payload
        row.origin = origin
        row.created = datetime.now(timezone.utc).replace(tzinfo=None)
        row.state = QUEUED
        row.attempts = 0
        row.message = ""
        row.finished = None
        handle.commit()
        return row.id
    finally:
        handle.close()


def _send(peer: str, row) -> tuple:
    """
    Offer one action to one peer. Answers (state, message).

    The sign-in, the renewal margin and the certificate are the sweep's, unchanged: a
    second way to authenticate to the same peer is a second thing to get wrong.
    """
    verify = trust()
    held = json.loads(row.payload or "{}")
    envelope = {"plugin": row.plugin, "path": row.path, "method": row.method,
                "variables": held.get("variables") or {}, "body": held.get("body"),
                "origin": row.origin, "request_id": row.request_id}
    url = f"{settings().api.protocol}://{peer}{API_PREFIX}/system/replication/apply"
    for attempt in (1, 2):
        try:
            answer = requests.post(url, json=envelope, timeout=TIMEOUT, verify=verify,
                                   headers={TOKEN_HEADER: peer_token(peer, verify,
                                                                     renew=attempt == 2)})
        except Exception as error:
            return QUEUED, str(error)
        if answer.status_code in (401, 403) and attempt == 1:
            continue
        if answer.status_code < 300:
            return DONE, ""
        if answer.status_code >= 500 or answer.status_code in (401, 403):
            return QUEUED, f"{answer.status_code}: {answer.text[:200]}"
        # Any other 4xx is terminal: the action is malformed, or names an endpoint this
        # peer does not serve. No number of passes fixes a version skew.
        return FAILED, f"{answer.status_code}: {answer.text[:200]}"
    return QUEUED, "no answer"


def _finish(row_id: int, state: str, message: str) -> None:
    """
    Write down what happened to one row, counting the attempt.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        if row is None:
            return
        row.attempts += 1
        row.message = message[:2048]
        if state == QUEUED and row.attempts >= MAX_ATTEMPTS:
            state = FAILED
            message = f"gave up after {row.attempts} attempts: {message}"
            row.message = message[:2048]
            LOGGER.error("%s still owes %s %s %s after %d attempts: %s", row.origin,
                         row.peer, row.method, row.path, row.attempts, message)
        row.state = state
        if state in (DONE, FAILED):
            row.finished = datetime.now(timezone.utc).replace(tzinfo=None)
        handle.commit()
    finally:
        handle.close()


def _attempt(peer: str, row_id: int) -> str:
    """
    One try at one row, updating the breaker from the outcome.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        if row is None or row.state != QUEUED:
            return DONE if row is not None else FAILED
        state, message = _send(peer, row)
    finally:
        handle.close()
    if state == DONE:
        record_success(peer)
    elif state == QUEUED:
        record_failure(peer)
    _finish(row_id, state, message)
    return state


def replicate(plugin: str, path: str, method: str, variables: dict, body,
              result, request_id: str, origin: str) -> dict:
    """
    Tell every peer about a call this controller has already made, and say what happened.

    Never raises: the local work is done and answered, and a replication that cannot be
    attempted must not turn a successful call into a failed one.
    """
    configured = peers()
    if not configured:
        return {"request_id": request_id, "peers": {}, "state": "off"}
    # Nothing changed, nothing to tell: the local path deliberately costs nothing for a
    # node that rebooted unchanged, and replicating anyway would undo that at the one
    # moment it matters. Only an explicit falsy `changed` skips, so the default is to send.
    if isinstance(result, dict) and "changed" in result and not result["changed"]:
        LOGGER.debug("%s %s changed nothing, so nothing is replicated", method, path)
        return {"request_id": request_id, "peers": {}, "state": "skipped"}

    answered = {}
    for peer in configured:
        try:
            row_id = submit(peer, plugin, path, method, variables, body, request_id, origin)
            if breaker_open(peer):
                answered[peer] = "peer-down"
                continue
            answered[peer] = _attempt(peer, row_id)
        except Exception as error:
            LOGGER.error("could not write down %s %s for %s: %s", method, path, peer, error)
            answered[peer] = "failed"
    return {"request_id": request_id, "peers": answered}


def drain(peer: str) -> dict:
    """
    Everything still owed to one peer, oldest intent first. The sweep calls this; nothing
    else does, and nothing runs between passes.
    """
    handle = database.session()
    try:
        waiting = handle.scalars(select(models.Outbox)
                                 .where(models.Outbox.peer == peer,
                                        models.Outbox.state == QUEUED)
                                 .order_by(models.Outbox.created,
                                           models.Outbox.id)).all()
        rows = [row.id for row in waiting]
    finally:
        handle.close()
    sent, left = 0, 0
    for row_id in rows:
        if _attempt(peer, row_id) == DONE:
            sent += 1
        else:
            # The peer has stopped answering: the rest keep their place for the next pass.
            left = len(rows) - sent
            break
    if sent:
        LOGGER.info("sent %d owed action(s) to %s", sent, peer)
    _prune()
    return {"sent": sent, "left": left}


def _prune() -> None:
    """
    Forget rows that are done. A failed one is evidence and is kept.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None).timestamp() - KEEP_SECONDS
    handle = database.session()
    try:
        handle.execute(delete(models.Outbox).where(
            models.Outbox.state == DONE,
            models.Outbox.finished < datetime.fromtimestamp(cutoff)))
        handle.commit()
    except Exception as error:
        handle.rollback()
        LOGGER.error("could not prune sent actions: %s", error)
    finally:
        handle.close()
