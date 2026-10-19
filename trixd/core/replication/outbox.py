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
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import delete, func, select

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, TOKEN_HEADER
from trixd.common.settings import current as settings
from trixd.core.replication import applied
from trixd.core.replication.sweep import TIMEOUT as SWEEP_TIMEOUT
from trixd.core.replication.sweep import held_token, trust
from trixd.core.replication.sweep import _token as peer_token
from trixd.db import engine as database
from trixd.db import models

LOGGER = trixlog.get("outbox")

QUEUED: str = "queued"
DONE: str = "done"
FAILED: str = "failed"

# What one attempt learned about the peer, which is not what it learned about the row. A
# refusal and a 5xx are the peer answering, and say nothing about the next node; only
# silence is evidence about the peer itself. Keeping these apart is what decides whether a
# pass carries on and whether the breaker counts it.
NOT_ATTEMPTED: str = "not-attempted"
NO_ANSWER: str = "no-answer"
ANSWERED: str = "answered"
# This process's other sender has the row, so it was neither offered nor refused.
IN_FLIGHT: str = "in-flight"

# The caller is waiting on this, so it is shorter than the sweep's: the row makes giving up
# free, and a dark peer must not hold a request thread. The inline path's alone - the drain
# pays the sweep's, imported rather than repeated, since a merely slow peer drains nothing.
TIMEOUT: int = 3
# After this many offers a row still waiting is said loudly, once, and then every
# this-many offers. Offers, not passes: a row the peer keeps answering for is not offered
# on every pass once it has backed off, so its first loud line lands late in the span it is
# allowed to be owed rather than twelve passes in. It is NOT given up on: a peer away for
# an hour of patching would otherwise leave a terminal row that stops everything for that
# target with no way back. What a row needs is to be seen, which is
# `/system/replication/owed` and the health counts, not to be abandoned.
LOUD_AFTER: int = 12
# How long a finished row is kept, so a caller that polls slowly still sees it.
KEEP_SECONDS: int = 3600
# What a message says when the peer answered and declined to do the work, and how long the
# peer is allowed to keep answering without applying it. The answer can be a moment of Luna
# lag on the peer, which the next pass clears; it can also be a node that peer will never
# know, and a row with no terminal state is cleared by nothing and holds its node's later
# actions for ever.
#
# It bounds every way the peer can answer and not do it - a decline in the body, a 5xx, a
# 401 or a 403 - because each of those leaves the same row in the same place, and only
# silence is a reason to keep asking indefinitely.
#
# How long rather than how many attempts. `attempts` counts every outcome - a dark peer, a
# 5xx, a pass with no token held - so eleven passes of a peer being patched, which is under
# an hour at the default interval, made the row terminal on its FIRST decline. `created` is
# when this intent was written down and is reset by a newer one, so the span is the thing
# meant all along: how long the peer has owed this without taking it.
DECLINED: str = "the peer did not apply it"
DECLINED_FOR_SECONDS: int = 3600
# How a row the peer has answered for waits before it is offered again: the sweep interval,
# doubling per offer, and never slower than the cap. The cap is what makes the rate a
# property of this module rather than of a site's INTERVAL - sweeping every thirty seconds
# offered a declining row a hundred and twenty times inside the hour it is allowed to be
# owed, which is a site's sweep frequency deciding how hard one row is retried. At the
# interval this code defaults to the cap is the interval, so such a site sees no change.
BACKOFF_FACTOR: int = 2
BACKOFF_CAP: int = 300
# How long a refusal nothing has cleared is kept. It holds its node's later actions, and
# only a newer action for that node clears it - so where nobody acts on that node again,
# the hold is permanent and lives in a row nothing reaps. A day is long enough to be seen
# in health and in `owed`, and long enough for an upgrade to land on the peer.
KEEP_REFUSED: int = 86400

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


def chain_of(target: str) -> str:
    """
    What two actions for one node share: the target without the body digest.

    One definition, because `drain` and the inline send hold a chain behind an action that
    has not landed and `submit` clears one, and any two of them disagreeing would leave
    rows nothing can clear, or send a node's actions out of order.
    """
    return target.split("|")[0]


def due_in(attempts: int) -> int:
    """
    How long after `created` a row that has been offered this many times may be offered
    again: the interval, doubled per offer already made, capped, and never longer than the
    span the peer is allowed to owe it for.

    Derived rather than stored, because the two fields it needs are already kept together:
    `created` is when this intent was written down and `attempts` how many times it has
    been offered, and `submit` resets both - so a newer intent starts again at the interval
    with nothing anywhere to put back, and a fresh row is due at once.

    The clamp is what keeps the span exact. The pass that makes a row terminal is the pass
    that offers it, so a row owed for longer than DECLINED_FOR_SECONDS has to be due, or
    the wait defers the exit as well as the retry - and a row carrying a large `attempts`
    would be deferred past any time anyone will wait, which is the wedge the span exists to
    remove. The interval floors at one second so the loop is bounded by the cap rather than
    by `attempts`.
    """
    base = max(1, settings().replication.interval)
    grown = 0
    while grown < attempts and base * BACKOFF_FACTOR ** grown < BACKOFF_CAP:
        grown += 1
    waited = base * (BACKOFF_FACTOR ** grown - 1) + (attempts - grown) * BACKOFF_CAP
    return min(waited, DECLINED_FOR_SECONDS)


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
        # A count on its own cannot tell a queue that is busy from one that is stuck: both
        # are a number of rows. How long the oldest has been waiting can, and it is one
        # aggregate rather than a row read.
        oldest = handle.scalar(select(func.min(models.Outbox.created))
                               .where(models.Outbox.state == QUEUED))
    finally:
        handle.close()
    with _lock:
        down = len(_open)
    waiting = 0
    if oldest is not None:
        # Both are naive UTC, so they are subtracted as datetimes: .timestamp() would read
        # each as local time, which cancels out except across a daylight-saving change.
        waiting = max(0, int((datetime.now(timezone.utc).replace(tzinfo=None)
                              - oldest).total_seconds()))
    return {"pending": counted.get(QUEUED, 0), "failed": counted.get(FAILED, 0),
            "peers": len(peers()), "not_answering": down, "oldest_pending": waiting}


def owed() -> dict:
    """
    What is still owed, peer by peer, for somebody who is allowed to know the peers.

    One shape always: a client reading `owed["actions"]` gets a list whether or not a peer
    is down. The peers-down notice used to be the first element of the same list, which
    meant the shape changed exactly when somebody was looking at it.
    """
    handle = database.session()
    try:
        rows = handle.scalars(select(models.Outbox)
                              .where(models.Outbox.state.in_((QUEUED, FAILED)))
                              .order_by(models.Outbox.created,
                                        models.Outbox.id)).all()
        listed = [{"peer": row.peer, "plugin": row.plugin, "path": row.path,
                   "method": row.method, "target": row.target, "state": row.state,
                   "attempts": row.attempts, "message": row.message,
                   "created": row.created.isoformat()} for row in rows]
    finally:
        handle.close()
    with _lock:
        down = sorted(_open)
    return {"not_answering": down, "actions": listed}


def _clear_refused(handle, peer: str, plugin: str, chain: str) -> None:
    """
    Drop what this peer refused for the same node, so a new intent is not held behind it.

    Whoever acts on that node again states what it should be now, which is a better account
    of the intent than the refused row is. The refusal is said as the row goes, because the
    row is the only other place it was written down.
    """
    refused = handle.scalars(select(models.Outbox).where(
        models.Outbox.peer == peer, models.Outbox.plugin == plugin,
        models.Outbox.state == FAILED)).all()
    for row in refused:
        if chain_of(row.target) != chain:
            continue
        LOGGER.warning("discarding %s %s, which %s refused (%s): a newer action for %s has "
                       "arrived and says what it should be now", row.method, row.path,
                       peer, row.message, chain)
        handle.delete(row)


def _held_behind(peer: str, plugin: str, chain: str, row_id: int) -> bool:
    """
    Whether something owed for the same node has to go before this row.

    The same question `drain` asks, asked the same way: the oldest owed row for a node goes
    first, and what comes after it assumes it landed. Only the head of a chain may be sent,
    so a first action for a node is never held, and a row is never held behind one that is
    done - nothing waits for a row that will not be sent again.
    """
    handle = database.session()
    try:
        owing = handle.scalars(select(models.Outbox)
                               .where(models.Outbox.peer == peer,
                                      models.Outbox.plugin == plugin,
                                      models.Outbox.state.in_((QUEUED, FAILED)))
                               .order_by(models.Outbox.created,
                                         models.Outbox.id)).all()
        head = next((row.id for row in owing if chain_of(row.target) == chain), row_id)
    finally:
        handle.close()
    return head != row_id


def submit(peer: str, plugin: str, path: str, method: str, variables: dict, body,
           request_id: str, origin: str) -> int:
    """
    Write down one action for one peer, replacing whatever was pending for the same target.

    The replacement is the point: the newest intent is the only one worth sending, and a
    stale row applied late is how a retry undoes newer state.

    A refusal for the same node goes at the same time, whatever its body. `drain` holds a
    node's chain behind a refused row, and only an identical call would have replaced that
    row - so a node whose rename was refused stayed held until one arrived, which is a
    wedge for everything else that node needs.
    """
    mark = target_of(variables, body)
    payload = json.dumps({"variables": variables or {}, "body": body})
    handle = database.session()
    try:
        _clear_refused(handle, peer, plugin, chain_of(mark))
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
    Offer one action to one peer. Answers (state, message, learned), where learned is one of
    the three above: having no token to present is not evidence about the peer, and a peer
    that answers badly is not a peer that has stopped answering.

    The renewal margin and the certificate are the sweep's, unchanged: a second way to
    authenticate to the same peer is a second thing to get wrong.

    `may_sign_in` is false on a caller's request path. Signing in carries the sweep's own
    thirty-second timeout, so inheriting it would undo this module's short one and hold a
    thread for half a minute against a dark peer - the stall the breaker exists to stop,
    arriving through the back door. With nothing held, the row waits for the sweep, which
    can afford to sign in.

    It decides the wait on the offer itself for the same reason: nobody is waiting on a
    sweep pass, and a peer answering slower than the request path's few seconds drained
    nothing at all, every pass, while doing the work each time and looking dark for it.
    What keeps a pass away from a peer that really is dark is the drain stopping on the
    first silence and the breaker, not the clock on one offer.
    """
    verify = trust()
    patience = SWEEP_TIMEOUT if may_sign_in else TIMEOUT
    if not may_sign_in and not held_token(peer):
        return QUEUED, "no token held for the peer, so the sweep will sign in", NOT_ATTEMPTED
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
                return (QUEUED, "the held token expired, so the sweep will sign in",
                        NOT_ATTEMPTED)
        try:
            answer = requests.post(url, json=envelope, timeout=patience, verify=verify,
                                   headers={TOKEN_HEADER: token})
        except Exception as error:
            # Nothing came back, which is the one thing that is evidence about the peer.
            return QUEUED, str(error), NO_ANSWER
        if answer.status_code in (401, 403) and attempt == 1:
            continue
        if answer.status_code < 300:
            declined = _skipped(answer)
            if declined:
                # Not done, so it keeps its place and holds its own node's chain: whatever
                # comes after it assumes it landed. It rides the sweep like any other owed
                # row, bounded by DECLINED_FOR_SECONDS, after which it is terminal and can be
                # cleared by the next action for that node and reaped.
                return QUEUED, f"{DECLINED}: {declined}", ANSWERED
            return DONE, "", ANSWERED
        if answer.status_code >= 500 or answer.status_code in (401, 403):
            return QUEUED, f"{answer.status_code}: {answer.text[:200]}", ANSWERED
        # Any other 4xx is terminal: the action is malformed, or names an endpoint this
        # peer does not serve. No number of passes fixes a version skew - and a peer with
        # the plugin unplugged answers 503, so it is kept above rather than dropped here.
        return FAILED, f"{answer.status_code}: {answer.text[:200]}", ANSWERED
    return QUEUED, "no answer", NO_ANSWER


def _skipped(answer) -> str:
    """
    What the peer said it did not do, or empty when it did the work.

    A success and the work having happened are not the same answer: a handler that finds
    the node absent from its own Luna says so in the body and answers 200, and recording
    that as done left no account of it anywhere. `changed: False` is not this - that is the
    peer already holding what was asked for, which is done, and is the ordinary answer when
    both controllers write one shared file.
    """
    try:
        held = answer.json() or {}
    except ValueError:
        return ""
    return applied.declined(held.get("result"))


# Rows one of this process's two senders has in flight, so the other does not send them too.
_sending: set = set()


def _claim(row_id: int) -> bool:
    """
    Take one row for sending, or say somebody else already has it.
    """
    with _lock:
        if row_id in _sending:
            return False
        _sending.add(row_id)
        return True


def _release(row_id: int) -> None:
    """
    Give the row back, sent or not.
    """
    with _lock:
        _sending.discard(row_id)


def _finish(row_id: int, state: str, message: str, learned: str = ANSWERED) -> None:
    """
    Write down what happened to one row, counting the attempt.

    `learned` is what the attempt learned about the peer, and it is what decides whether a
    row still queued is allowed to stay queued for ever. The peer answering and not doing
    the work is bounded; silence is not. It defaults to the peer having answered, which is
    what a caller writing an outcome down by hand is saying happened.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        if row is None:
            return
        row.attempts += 1
        row.message = message[:2048]
        owed_for = (datetime.now(timezone.utc).replace(tzinfo=None)
                    - row.created).total_seconds()
        if state == QUEUED and learned == ANSWERED and owed_for >= DECLINED_FOR_SECONDS:
            # The peer has answered for long enough without doing it. Terminal is what gives
            # it an exit: the next action for this node clears it, and a day later it is
            # reaped - neither of which looks at a queued row, so such a row kept asking for
            # ever and held the node with it.
            #
            # On what the attempt learned, not on the message: keyed on the decline's
            # wording it bounded a decline and left a 5xx, a 401 and a 403 - the same row,
            # in the same place, saying the same thing - with no exit at all. Any way the
            # peer can answer and not do the work is this case, including a new one.
            state = FAILED
            LOGGER.error("%s has answered for %s %s for %.0f minutes without applying "
                         "it, so it is not asked again: %s", row.peer, row.method,
                         row.path, owed_for / 60, message)
        if state == QUEUED and row.attempts and row.attempts % LOUD_AFTER == 0:
            # Said, not abandoned: it keeps riding the sweep until the peer takes it.
            LOGGER.error("%s has owed %s a %s %s for %d offers now: %s", row.origin,
                         row.peer, row.method, row.path, row.attempts, message)
        if state == FAILED:
            # The row is not permanent evidence - the next action for this node discards it
            # - so what the peer said has to reach the log as it happens.
            LOGGER.error("%s refused %s %s (%s): %s - it holds this node's later actions "
                         "until a newer one arrives", row.peer, row.method, row.path,
                         row.target, message)
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


def _attempt(peer: str, row_id: int, may_sign_in: bool = True) -> tuple:
    """
    One try at one row, updating the breaker from the outcome. Answers (state, learned).

    What the row learned about the peer is what tells a row the peer answered badly for
    apart from a peer that has stopped answering. They look the same from the row - neither
    is done - and they are not the same for the rest of the queue, nor for the breaker: one
    row refused says nothing about the next node, and nothing at all about the socket.

    The row is copied out and the session closed before anything is sent. The pool holds one
    connection per caller thread and no overflow, and every one of those threads can be in
    here at once against a slow peer, so a connection held across a network wait is a
    connection another caller waits out - the same stall the breaker exists to prevent one
    layer down. The sizes live in `trixd/db/engine.py`; they are not repeated here because
    a number in a comment is the first thing to go stale.
    """
    handle = database.session()
    try:
        row = handle.scalar(select(models.Outbox).where(models.Outbox.id == row_id))
        if row is None:
            return FAILED, ANSWERED
        if row.state != QUEUED:
            # Its own state, where this answered DONE for anything that was no longer
            # queued. A caller's inline attempt can refuse a row between the sweep taking
            # its snapshot and reaching it - one offer's wait a row, so minutes with a queue
            # of any size - and reading that back as done counted it as sent, left
            # its chain unheld, and offered the next action for that node over a
            # prerequisite the peer had refused.
            return row.state, ANSWERED
        held = _Held(row)
    finally:
        handle.close()
    if not _claim(row_id):
        # The other sender has this row in flight. Both live in this process - the sweep's
        # drain and a caller's inline attempt - and both read the row as queued before
        # either marks it, so the same call went to the peer twice. Its own outcome, not
        # ANSWERED: nothing was offered to the peer, and saying it answered badly for this
        # row put a sentence in the log that had not happened.
        return QUEUED, IN_FLIGHT
    try:
        state, message, learned = _send(peer, held, may_sign_in)
        _finish(row_id, state, message, learned)
    finally:
        # After the outcome is written, not before: released first, the row was unclaimed
        # and still queued for as long as the write took, which is the window the claim
        # exists to close.
        _release(row_id)
    if state == DONE:
        record_success(peer)
    elif learned == NO_ANSWER:
        # Only silence counts: a refusal, a 5xx and a decline are all the peer answering,
        # and blaming the socket for them opened the breaker on a healthy peer.
        record_failure(peer)
    return state, learned


def replicate(plugin: str, path: str, method: str, variables: dict, body,
              result, request_id: str, origin: str) -> dict:
    """
    Tell every peer about a call this controller has already made, and say what happened.

    Never raises: the local work is done and answered, and a replication that cannot be
    attempted must not turn a successful call into a failed one.

    It holds a node's chain exactly as the sweep's drain does. Sending inline without
    asking delivered a row while an earlier one for the same node was still owed - a delete
    going before the write it supersedes - and the next pass then applied the earlier one
    over it, with both rows done and nothing anywhere saying the peer was wrong.
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
    chain = chain_of(target_of(variables, body))
    for peer in configured:
        try:
            row_id = submit(peer, plugin, path, method, variables, body, request_id, origin)
            if breaker_open(peer):
                answered[peer] = "peer-down"
                continue
            if _held_behind(peer, plugin, chain, row_id):
                # Something earlier for this node has not landed, and this assumes it did.
                # It is written down, so the sweep carries both in order.
                answered[peer] = "held-behind"
                continue
            # Not on a caller's thread: no sign-in, and a short timeout.
            answered[peer] = _attempt(peer, row_id, may_sign_in=False)[0]
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

    A row the peer refused outright stops the rest of **that target's** chain rather than
    being stepped over: the later actions for a target assume the earlier one landed - a
    rename assumes the file it renames was written - so carrying on would apply them over a
    prerequisite that never happened, turning a loud failure into quiet wrong state.

    It stops that node's chain and nothing else. A refusal naming one node says nothing
    about another node, or another plugin, and blocking those would make one malformed
    action an outage for everything this controller owes.

    Which actions are relative was read rather than assumed: `repair` and `rename` take the
    file that is already there, relabel it and move it - `rename_hw_rules` is
    `repair_hw_rules(node, was=...)` and does nothing at all when no file exists. Only the
    PATCH routes write a file from scratch. So a rename behind a refused write would
    silently do nothing, which is the case this exists for.

    What clears a refused row is the next action for the same node, whatever its body:
    `submit` discards it, and the hold goes with it. So a refusal holds the actions that
    were already queued behind it - the ones that assumed it landed - and nothing after
    that, because whoever acts on the node again states what it should be now.
    """
    handle = database.session()
    try:
        waiting = handle.scalars(select(models.Outbox)
                                 .where(models.Outbox.peer == peer,
                                        models.Outbox.state.in_((QUEUED, FAILED)))
                                 .order_by(models.Outbox.created,
                                           models.Outbox.id)).all()
        # The chain is the plugin and the thing named in the path - the node - with the
        # body digest left off, so a PATCH and a rename for one node are the same chain.
        # Keying on the whole target would not group them: the PATCH carries a digest and
        # the rename a different one, which is precisely the pair that must stay ordered.
        rows = [(row.id, row.state, (row.plugin, chain_of(row.target)),
                 row.created, row.attempts) for row in waiting]
    finally:
        handle.close()
    sent, refused, blocked, holding, in_flight, deferred = 0, 0, 0, 0, 0, 0
    stuck = set()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for row_id, state, chain, created, attempts in rows:
        if state == FAILED:
            stuck.add(chain)
            refused += 1
            continue
        if chain in stuck:
            # Something earlier for this same target was refused; this one assumes it.
            blocked += 1
            continue
        if (now - created).total_seconds() < due_in(attempts):
            # Offered recently enough. It is not done, so it holds its own node's chain
            # exactly as a decline does: whatever comes after it assumes it landed.
            stuck.add(chain)
            deferred += 1
            continue
        outcome, learned = _attempt(peer, row_id)
        if outcome == DONE:
            sent += 1
        elif learned == IN_FLIGHT:
            # This process's other sender has the row. Nothing was offered, so there is
            # nothing to conclude about the peer; the chain is still held, because whatever
            # comes after this row assumes it landed and its outcome is not known yet.
            stuck.add(chain)
            in_flight += 1
        elif learned != ANSWERED:
            # Nothing came back. Everything from here, this row included, keeps its place
            # for the next pass: offering the rest would be one whole timeout per row
            # against a peer that is not there, which is the stall the breaker exists to
            # prevent one layer down - and it is this stop, not a short clock on the offer,
            # that keeps a dark peer from taking the pass.
            break
        else:
            # It answered, and answered badly for this row alone - a refusal, a 5xx, or a
            # body saying it did not apply it. The peer is up, so the rest of what it owes
            # is still worth offering; stopping here held every other node behind one row,
            # pass after pass, with nothing able to clear it. This row holds its own node's
            # chain, because what comes after it assumes it landed.
            stuck.add(chain)
            if outcome == FAILED:
                # Terminal, and counted the same whether this pass met the refusal or read
                # it from the row: the pass that met it used to report nothing at all.
                refused += 1
            else:
                holding += 1
    reaped = _reap(peer)
    _prune()
    # Everything this peer still owes after the pass, refusals and held rows included, and
    # after the reap: counted before it, this included the rows the same pass had just let
    # go of. Counting only from the point the pass stopped left out the rows it had held,
    # and arithmetic over four counters is a thing to get wrong rather than to read.
    left = max(0, len(rows) - sent - reaped)
    if sent:
        LOGGER.info("sent %d owed action(s) to %s", sent, peer)
    if refused:
        LOGGER.error("%s refused %d action(s); the next action for a held node clears the "
                     "refusal and the hold", peer, refused)
    if holding:
        LOGGER.warning("%s did not take %d action(s) it answered for; they keep their place "
                       "and hold their own node's later actions, and the rest of what it "
                       "owes was offered", peer, holding)
    if blocked:
        LOGGER.warning("%d action(s) at %s are held behind a refusal or a decline for the "
                       "same node(s)", blocked, peer)
    if in_flight:
        LOGGER.info("%d action(s) for %s were already being sent by this daemon's other "
                    "sender, so this pass left them alone", in_flight, peer)
    if deferred:
        # Debug, not a warning: a queue being retried is in this state most of the time,
        # and a line per pass per peer would bury the ones above it. What is still owed by
        # a deferred row is in `left`, which is what health reports.
        LOGGER.debug("%d action(s) at %s wait for their next offer", deferred, peer)
    return {"sent": sent, "left": left, "refused": refused, "blocked": blocked,
            "in_flight": in_flight, "deferred": deferred}


def _reap(peer: str) -> int:
    """
    Let go of a refusal nothing has cleared, and of what it was holding.

    What is held behind it goes with it rather than being sent: those actions assume the
    refused one landed, so offering them now is the quiet wrong state the hold exists to
    prevent. Dropping them says plainly that the peer is behind for that node until
    somebody acts on it again - which is also what clears every other refusal.
    """
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(seconds=KEEP_REFUSED))
    handle = database.session()
    try:
        stale = handle.scalars(select(models.Outbox).where(
            models.Outbox.peer == peer, models.Outbox.state == FAILED,
            models.Outbox.finished < cutoff)).all()
        gone = 0
        for row in stale:
            chain = chain_of(row.target)
            # Only what was already waiting when the refusal was written down. A later row
            # for this node cannot be held behind it - submitting one clears the refusal -
            # so taking it would be this reap deleting an intent nobody has sent yet.
            behind = [other for other in handle.scalars(select(models.Outbox).where(
                models.Outbox.peer == peer, models.Outbox.plugin == row.plugin,
                models.Outbox.state == QUEUED,
                models.Outbox.created < row.finished)).all()
                if chain_of(other.target) == chain]
            LOGGER.error("letting go of %s %s, which %s refused and nothing has cleared in "
                         "%ds, and of %d action(s) it was holding: %s is behind for %s "
                         "until something acts on it again", row.method, row.path, peer,
                         KEEP_REFUSED, len(behind), peer, chain)
            for other in behind:
                handle.delete(other)
            handle.delete(row)
            gone += 1 + len(behind)
        handle.commit()
        return gone
    except Exception as error:
        handle.rollback()
        LOGGER.error("could not let go of refused actions for %s: %s", peer, error)
        return 0
    finally:
        handle.close()


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
