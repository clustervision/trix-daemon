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
One directory per build under the state root: what was asked, what was decided, where
the build is, what it said, and how it ended. The daemon and the runner share it through
files alone.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import datetime
import json
import os
import shutil
import signal
import subprocess
import threading

from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure, Conflict, NotFound
from trixd.plugins.osimage import mounts
from trixd.utils import files

LOGGER = trixlog.get("osimage.jobs")

REQUEST: str = "request.json"
VERDICT: str = "verdict.json"
STATE: str = "state.json"
PROGRESS: str = "progress.jsonl"
LOG: str = "build.log"
RESULT: str = "result.json"
PHASE: str = "phase"
MOUNTS: str = "mnt"
REPOS: str = "repo"
# The targets a runner bound under the image tree, one a line, written before each mount.
TREE_MOUNTS: str = "tree-mounts"
# The process group of the book while it runs, in a session of its own; 0 once it ended.
BOOK_GROUP: str = "book-group"
INVENTORY: str = "inventory"

TERMINAL: tuple = ("done", "failed", "cancelled")
TRANSITIONS: dict = {"prepared": ("running", "cancelled"),
                     "running": ("done", "failed", "cancelled")}
# A job whose build never started is forgotten after this long.
PREPARED_SECONDS: int = 3600
# The exit the runner records when it was stopped, so a result can say cancelled.
EXIT_CANCELLED: int = 143
# The runner gets SIGTERM first, to stop the book, unmount and record the result; what is
# left of it after this many seconds is killed.
STOP_GRACE: int = 90

# The daemon's one writer lock for state.json: the request threads and the supervisor
# read, decide and write, and the count behind the concurrency cap is read here too.
LOCK = threading.Lock()
# The build processes this daemon started, by job name, under LOCK. Here and not in the
# backend, which the loader imports under two names, so it would be two dicts.
CHILDREN: dict = {}


def path(state_root: str, name: str) -> str:
    """
    The directory of one job.
    """
    return os.path.join(state_root, name)


def exists(state_root: str, name: str) -> bool:
    """
    Whether a job of that name is held, in any state.
    """
    return os.path.isfile(os.path.join(path(state_root, name), STATE))


def names(state_root: str) -> list:
    """
    Every job held, by name. A state root that is not there yet holds none.
    """
    if not os.path.isdir(state_root):
        return []
    return sorted(name for name in os.listdir(state_root) if exists(state_root, name))


def stamp() -> str:
    """
    Now, as the timestamps in state.json are written.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def seconds_since(when: str) -> float:
    """
    How long ago a timestamp of state.json was.
    """
    then = datetime.datetime.fromisoformat(when)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()


def read_json(jobdir: str, filename: str) -> dict:
    """
    One of the job's documents, or an empty one where it is not written yet.
    """
    where = os.path.join(jobdir, filename)
    if not os.path.isfile(where):
        return {}
    with open(where, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_state(jobdir: str) -> dict:
    """
    Where the job is.
    """
    if not os.path.isfile(os.path.join(jobdir, STATE)):
        raise NotFound(f"no build named {os.path.basename(jobdir)}")
    return read_json(jobdir, STATE)


def create(state_root: str, name: str, request: dict, verdict: dict) -> dict:
    """
    Write a job whose build is about to start, or refuse: one of that name is running, or
    ended and has not finished telling Luna. A job of that name that ended, or whose build
    never started, is replaced (nothing runs for either), unless its directory still has
    something mounted below it.
    """
    with LOCK:
        if exists(state_root, name):
            held = read_state(path(state_root, name))
            if held["state"] == "running":
                raise Conflict(f"a build named {name} is running")
            if owes_stream(held):
                raise Conflict(f"the earlier build of {name} is {held['state']} and its stream "
                               f"is still being delivered")
            if not _remove(path(state_root, name)):
                raise Conflict(f"a build named {name} is {held['state']} and still has a mount "
                               f"below its directory")
        jobdir = path(state_root, name)
        files.write(os.path.join(jobdir, REQUEST), json.dumps(request, indent=1), 0o600)
        files.write(os.path.join(jobdir, VERDICT), json.dumps(verdict, indent=1), 0o600)
        state = {"name": name, "state": "prepared", "prepared": stamp(), "started": None,
                 "finished": None, "pid": None, "exit": None, "reason": "",
                 "request_id": None, "pushed": 0, "closing": ""}
        return _write_state(jobdir, state)


def move(jobdir: str, state: str, reason: str = "", **fields) -> dict:
    """
    Take the job to its next state, with the timestamp that state carries, or refuse a
    step the machine has no edge for.
    """
    with LOCK:
        held = read_state(jobdir)
        if state not in TRANSITIONS.get(held["state"], ()):
            raise Conflict(f"build {held['name']} is {held['state']}, not {state}")
        held.update(fields)
        held["state"] = state
        if reason:
            held["reason"] = reason
        if state == "running":
            held["started"] = stamp()
        if state in TERMINAL:
            held["finished"] = stamp()
        LOGGER.info("build %s is %s%s", held["name"], state, f": {reason}" if reason else "")
        return _write_state(jobdir, held)


def update(jobdir: str, **fields) -> dict:
    """
    Record fields of the job that belong to no state change, such as how far its lines
    were delivered.
    """
    with LOCK:
        held = read_state(jobdir)
        held.update(fields)
        return _write_state(jobdir, held)


def running(state_root: str) -> list:
    """
    The state of every job that is running; one whose build never started counts for
    nothing.
    """
    held = [read_state(path(state_root, name)) for name in names(state_root)]
    return [state for state in held if state["state"] == "running"]


def owes_stream(state: dict) -> bool:
    """
    Whether a job has a request to answer and is not done telling Luna.
    """
    return bool(state.get("request_id")) and state.get("closing", "") != "closed"


def unclosed(state_root: str) -> list:
    """
    The state of every job that owes its stream, whether it is still running or ended
    while the daemon was down.
    """
    held = [read_state(path(state_root, name)) for name in names(state_root)]
    return [state for state in held if owes_stream(state)]


def start(name: str, jobdir: str, argv: list, env: dict) -> subprocess.Popen:
    """
    Start the build as a child of the daemon in a session of its own, so a signal to its
    group reaches the book and nothing else. Its output goes to the job's log, opened for
    append as the runner opens it, so what it prints before it has opened its own files
    is still there. Called under the start lock.
    """
    log = os.open(os.path.join(jobdir, LOG), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True, env=env)
    finally:
        os.close(log)
    with LOCK:
        CHILDREN[name] = process
    return process


def child(name: str):
    """
    The build process this daemon started for a job, or nothing where it holds none,
    which is what a restart of the daemon leaves.
    """
    with LOCK:
        return CHILDREN.get(name)


def drop(name: str) -> None:
    """
    Forget a build process that ended and was recorded.
    """
    with LOCK:
        CHILDREN.pop(name, None)


def stop(name: str, jobdir: str) -> None:
    """
    Stop the build process of a job and wait for it, so the caller may read the result
    once this answers: SIGTERM to its group, which the runner passes on to the book, and
    SIGKILL to both groups when it has not ended within the grace. A job this daemon holds
    no process for is left alone.
    """
    process = child(name)
    if process is None:
        return
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(STOP_GRACE)
    except subprocess.TimeoutExpired:
        LOGGER.error("the build of %s did not end within %s s of SIGTERM, killing it", name,
                     STOP_GRACE)
        _signal_group(process, signal.SIGKILL)
        _kill_book(jobdir)
        process.wait(STOP_GRACE)
    drop(name)
    LOGGER.info("stopped the build of %s", name)


def lines_since(jobdir: str, since: int = 0) -> tuple:
    """
    The progress lines from a sequence number on, and the number to ask for next. A
    line the runner is in the middle of writing is left for the next read.
    """
    where = os.path.join(jobdir, PROGRESS)
    if not os.path.isfile(where):
        return [], since
    lines = []
    with open(where, "r", encoding="utf-8") as handle:
        for raw in handle:
            try:
                line = json.loads(raw)
            except ValueError:
                break
            if line["seq"] >= since:
                lines.append(line)
    return lines, (lines[-1]["seq"] + 1) if lines else since


def read_phase(jobdir: str) -> str:
    """
    What the runner says it is doing, or nothing before it has said.
    """
    return files.read_text(os.path.join(jobdir, PHASE)).strip()


def write_phase(jobdir: str, phase: str) -> None:
    """
    The runner's side of read_phase.
    """
    files.write(os.path.join(jobdir, PHASE), phase + "\n", 0o600)


def write_book_group(jobdir: str, pgid: int) -> None:
    """
    The runner's note of the book's process group: its pgid while it runs, 0 once it ended.
    """
    files.write(os.path.join(jobdir, BOOK_GROUP), f"{pgid}\n", 0o600)


def read_book_group(jobdir: str) -> int:
    """
    The process group of a book still running, or 0.
    """
    return int(files.read_text(os.path.join(jobdir, BOOK_GROUP), "0").strip() or 0)


def show(jobdir: str, since: int = 0) -> dict:
    """
    The job as a reader is answered: its state, the runner's phase, the lines from a
    sequence number on, and what the runner recorded once it ended.
    """
    held = read_state(jobdir)
    lines, following = lines_since(jobdir, since)
    answer = {key: held.get(key) for key in ("name", "state", "prepared", "started",
                                              "finished", "exit", "reason", "pid")}
    answer.update({"phase": read_phase(jobdir), "lines": lines, "next": following})
    result = read_json(jobdir, RESULT)
    if result:
        answer.update({"kernelversion": result.get("kernelversion", ""),
                       "client_version": result.get("client_version", "")})
    return answer


def prune(state_root: str, keep_days: int) -> list:
    """
    Forget what nobody will ask for again: a job written an hour ago whose build never
    started, and one that ended more than keep_days ago. A directory with anything still mounted
    below it is left alone and said so, because removing it would remove what is
    mounted there.
    """
    forgotten = []
    for name in names(state_root):
        jobdir = path(state_root, name)
        held = read_state(jobdir)
        if held["state"] == "prepared":
            stale = seconds_since(held["prepared"]) > PREPARED_SECONDS
        elif held["state"] in TERMINAL:
            stale = seconds_since(held["finished"]) > keep_days * 86400
        else:
            stale = False
        if not stale or not _remove(jobdir):
            continue
        LOGGER.info("pruned build %s, %s since %s", name, held["state"],
                    held["finished"] or held["prepared"])
        forgotten.append(name)
    return forgotten


def finish(jobdir: str, state: str, reason: str = "") -> dict:
    """
    End a job whose build process has ended: what the runner recorded, a lazy unmount of what
    it left below its mount directory, said loudly, and the move. A mount table that
    cannot be read does not keep the job running for ever; it is said instead. A job
    that already ended, because a cancel and the supervisor met, answers what is recorded.
    """
    result = read_json(jobdir, RESULT)
    try:
        left = (mounts.release(os.path.join(jobdir, MOUNTS))
                + mounts.release_listed(os.path.join(jobdir, TREE_MOUNTS)))
    except BackendFailure as error:
        left = []
        LOGGER.error("the mount table below %s could not be read: %s", jobdir, error)
    if left:
        LOGGER.error("still mounted below %s after a lazy unmount: %s", jobdir, ", ".join(left))
    try:
        return move(jobdir, state, reason or result.get("reason", ""), exit=result.get("exit"))
    except Conflict:
        held = read_state(jobdir)
        if held["state"] not in TERMINAL:
            raise
        LOGGER.info("build %s had already ended as %s", held["name"], held["state"])
        return held


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    """
    Signal the group of a build process, which a cancel and the supervisor may both be
    ending: a process already reaped is left alone, and a group that is already gone is
    said and is no failure.
    """
    # A reaped pid is free and may belong to another process by now.
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        LOGGER.info("the process group %s had already ended", process.pid)


def _kill_book(jobdir: str) -> None:
    """
    SIGKILL to the book's own group, which the kill of the runner's group does not reach:
    a book that did not end on the SIGTERM the runner passed on, a package manager in a
    transaction, would go on writing into the tree Luna then removes.
    """
    pgid = read_book_group(jobdir)
    if not pgid:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        LOGGER.info("the book's process group %s had already ended", pgid)


def _remove(jobdir: str) -> bool:
    """
    Remove a job directory, unless something is still mounted below it: the directory
    held loop mounts, and removing it then would remove what is mounted there.
    """
    left = mounts.targets_below(jobdir)
    if left:
        LOGGER.error("not removing %s: still mounted below it: %s", jobdir, ", ".join(left))
        return False
    shutil.rmtree(jobdir)
    return True


def _write_state(jobdir: str, state: dict) -> dict:
    """
    state.json, through the one atomic writer.
    """
    files.write(os.path.join(jobdir, STATE), json.dumps(state, indent=1), 0o600)
    return state
