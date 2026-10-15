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
from sqlalchemy import delete, func, select

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, TOKEN_HEADER
from trixd.common.settings import current as settings
from trixd.core.replication.sweep import held_token, trust
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
    # The digest is what tells two bodies apart, so it is the part that must survive: trim
    # the names instead. Truncating the whole string would cut the digest off the end and
    # silently merge two different intents - a rename with a different `was` among them.
    return f"{named[:238]}|{digest}" if named else digest


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
    What the health endpoint publishes: how much is owed and how many peers are not
    answering - counts only.

    No peer names and no rows: health takes no token, so naming the other controllers
    there would publish the pair's layout to anyone who can reach the port. The counts
    say whether to look; `GET /system/replication/owed` says where, behind the gate.
    Counted in the database rather than by loading every row, because an unauthenticated
    route must not cost more the further behind it falls.
    """
    handle = database.session()
    try:
        counted = dict(handle.execute(
            select(models.Outbox.state, func.count())
            .where(models.Outbox.state.in_((QUEUED, FAILED)))
            .group_by(models.Outbox.state)).all())
    finally:
        handle.close()
    with _lock:
        down = len(_open)
    return {"pending": counted.get(QUEUED, 0), "failed": counted.get(FAILED, 0),
            "peers": len(peers()), "not_answering": down}


def owed() -> list:
    """
    What is still owed, peer by peer, for somebody who is allowed to know the peers.
    """
    handle = database.session()
    try:
        rows = handle.scalars(select(models.Outbox)
                              .where(models.Outbox.state.in_((QUEUED, FAILED)))
                              .order_by(models.Outbox.created)).all()
        listed = [{"peer": row.peer, "plugin": row.plugin, "path": row.path,
                   "method": row.method, "target": row.target, "state": row.state,
                   "attempts": row.attempts, "message": row.message,
                   "created": row.created.isoformat()} for row in rows]
    finally:
        handle.close()
    with _lock:
        down = sorted(_open)
    return [{"not_answering": down}, *listed] if down else listed


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


def _send(peer: str, row, may_sign_in: bool = True) -> tuple:
    """
    Offer one action to one peer. Answers (state, message, reached), where reached says
    whether the peer was actually contacted - having no token to present is not evidence
    about the peer and must not count towards its breaker.

    The renewal margin and the certificate are the sweep's, unchanged: a second way to
    authenticate to the same peer is a second thing to get wrong.

    `may_sign_in` is false on a caller's request path. Signing in carries the sweep's own
    thirty-second timeout, so inheriting it would undo this module's short one and hold a
    thread for half a minute against a dark peer - the stall the breaker exists to stop,
    arriving through the back door. With nothing held, the row waits for the sweep, which
    can afford to sign in.
    """
    verify = trust()
    if not may_sign_in and not held_token(peer):
        return QUEUED, "no token held for the peer, so the sweep will sign in", False
    held = json.loads(row.payload or "{}")
    envelope = {"plugin": row.plugin, "path": row.path, "method": row.method,
                "variables": held.get("variables") or {}, "body": held.get("body"),
                "origin": row.origin, "request_id": row.request_id}
    url = f"{settings().api.protocol}://{peer}{API_PREFIX}/system/replication/apply"
    for attempt in (1, 2):
        if may_sign_in:
            token = peer_token(peer, verify, renew=attempt == 2)
        else:
            # Held or nothing: never a sign-in on a request path.
            token = held_token(peer)
            if not token:
                return QUEUED, "the held token expired, so the sweep will sign in", False
        try:
            answer = requests.post(url, json=envelope, timeout=TIMEOUT, verify=verify,
                                   headers={TOKEN_HEADER: token})
        except Exception as error:
            return QUEUED, str(error), True
        if answer.status_code in (401, 403) and attempt == 1:
            continue
        if answer.status_code < 300:
            return DONE, "", True
        if answer.status_code >= 500 or answer.status_code in (401, 403):
            return QUEUED, f"{answer.status_code}: {answer.text[:200]}", True
        # Any other 4xx is terminal: the action is malformed, or names an endpoint this
        # peer does not serve. No number of passes fixes a version skew.
        return FAILED, f"{answer.status_code}: {answer.text[:200]}", True
    return QUEUED, "no answer", True


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


class _Held:
    """
    One row's fields, detached from the session that read them.
    """

    def __init__(self, row) -> None:
        """
        Copy what sending needs, so no connection is held while the network is waited on.
        """
        self.plugin = row.plugin
        self.path = row.path
        self.method = row.method
        self.payload = row.payload
        self.origin = row.origin
        self.request_id = row.request_id


def _attempt(peer: str, row_id: int, may_sign_in: bool = True) -> str:
    """
    One try at one row, updating the breaker from the outcome.

    The row is copied out and the session closed before anything is sent: the pool is five
    connections with ten of overflow, and up to forty threadpool threads can be in here at
    once against a slow peer. Holding one across a network wait would starve every other
    caller of the database, which is the same stall the breaker exists to prevent one
    layer down.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        if row is None or row.state != QUEUED:
            return DONE if row is not None else FAILED
        held = _Held(row)
    finally:
        handle.close()
    state, message, reached = _send(peer, held, may_sign_in)
    if state == DONE:
        record_success(peer)
    elif state == QUEUED and reached:
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
            # Not on a caller's thread: no sign-in, and a short timeout.
            answered[peer] = _attempt(peer, row_id, may_sign_in=False)
        except Exception as error:
            # Not "failed": that word means the peer refused it, and this never reached the
            # peer. Two calls for one target can race on the unique key; the loser says so
            # honestly and the winner's row is the newer intent anyway.
            LOGGER.error("could not write down %s %s for %s: %s", method, path, peer, error)
            answered[peer] = "not-written-down"
    return {"request_id": request_id, "peers": answered}


def drain(peer: str) -> dict:
    """
    Everything still owed to one peer, oldest intent first. The sweep calls this; nothing
    else does, and nothing runs between passes.

    A row that has been given up on stops the queue behind it rather than being stepped
    over. The later actions assume the earlier one landed - a rename assumes the file it
    renames was written - so carrying on would apply them over a prerequisite that never
    happened, turning a loud failure into quiet wrong state. It is the same fail-stop the
    journal uses, and it needs a person: section 8's `failed` count and the log are what tell
    them.
    """
    handle = database.session()
    try:
        waiting = handle.scalars(select(models.Outbox)
                                 .where(models.Outbox.peer == peer,
                                        models.Outbox.state.in_((QUEUED, FAILED)))
                                 .order_by(models.Outbox.created,
                                           models.Outbox.id)).all()
        rows = [(row.id, row.state) for row in waiting]
    finally:
        handle.close()
    sent, left, blocked = 0, 0, 0
    for index, (row_id, state) in enumerate(rows):
        if state == FAILED:
            blocked = len(rows) - index
            LOGGER.error("%d action(s) for %s are stuck behind one that was given up on; "
                         "nothing further is sent to it until that row is dealt with",
                         blocked, peer)
            break
        if _attempt(peer, row_id) == DONE:
            sent += 1
        else:
            # The peer has stopped answering: the rest keep their place for the next pass.
            left = len(rows) - index - 1
            break
    if sent:
        LOGGER.info("sent %d owed action(s) to %s", sent, peer)
    _prune()
    return {"sent": sent, "left": left, "blocked": blocked}


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
