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
The supervisor pass over the job directories, with the build processes faked and findmnt
stubbed: a build that ended, one over its limit, one this daemon holds no process for, and
a pass that cannot run.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import fcntl
import itertools
import json
import logging
import os
import signal
import subprocess
import time

import pytest

from trixd.common.errors import BackendFailure
from trixd.plugins.osimage import backend, jobs, push, supervisor
from trixd.utils import shell

from tests.unit.test_osimage_runner import REQUEST, VERDICT, Commands, Ran, _ago

# A pid no process holds, for a job whose build this daemon never started.
DEAD_PID = 4194303
REQUEST_ID = "1761818733.1234567891234"


class FakeChild:
    """
    A build process in the shape of Popen that the supervisor and the cancel use: a pid,
    a poll, a wait that times out while it runs, and a return code once it ended.
    """

    def __init__(self, pid: int, code: int = None, stubborn: bool = False,
                 wait_raises: bool = False) -> None:
        """
        Running when no code is given; a stubborn one ends only on SIGKILL.
        """
        self.pid = pid
        self.returncode = code
        self.stubborn = stubborn
        self.wait_raises = wait_raises
        self.poll_error = None
        self.polled: int = 0
        self.waited: list = []

    def poll(self):
        """
        The return code, or nothing while it runs.
        """
        self.polled += 1
        if self.poll_error:
            raise self.poll_error
        return self.returncode

    def wait(self, timeout=None):
        """
        The return code, or a timeout while it runs.
        """
        if self.wait_raises:
            raise AssertionError("something waited for the build")
        self.waited.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("build", timeout)
        return self.returncode


class Children:
    """
    Popen and os.killpg as the daemon meets them: what was started and how, the signals
    sent to a group, and what each build does with them.
    """

    def __init__(self) -> None:
        """
        Nothing started, every start succeeds, every build ends on SIGTERM.
        """
        self.made: list = []
        self.calls: list = []
        self.signals: list = []
        self.known: dict = {}
        self.refuse = None
        self.stubborn: bool = False
        self.wait_raises: bool = False
        self.before = None
        self.on_signal = None
        self.pids = itertools.count(40000)

    def spawn(self, code: int = None) -> FakeChild:
        """
        A build process that is running, or has ended with that code.
        """
        child = FakeChild(next(self.pids), code, self.stubborn, self.wait_raises)
        self.known[child.pid] = child
        return child

    def popen(self, argv, **kwargs) -> FakeChild:
        """
        Record the start, and what the output descriptor pointed at while it was open.
        """
        if self.before:
            self.before()
        if self.refuse:
            raise self.refuse
        out = kwargs.get("stdout")
        fd = out if isinstance(out, int) else getattr(out, "fileno", lambda: None)()
        opened = None
        if fd is not None:
            opened = {"path": os.path.realpath(os.readlink(f"/proc/self/fd/{fd}")),
                      "append": bool(os.O_APPEND & fcntl.fcntl(fd, fcntl.F_GETFL)),
                      "mode": os.fstat(fd).st_mode & 0o777}
        self.calls.append((list(argv), kwargs, opened))
        child = self.spawn()
        self.made.append(child)
        return child

    def killpg(self, pgid: int, sig: int) -> None:
        """
        Record the signal; the build ends on it unless it is stubborn and it is not SIGKILL.
        """
        self.signals.append((pgid, sig))
        if self.on_signal:
            self.on_signal()
        child = self.known.get(pgid)
        if child is None:
            raise ProcessLookupError(pgid)
        if child.returncode is None and (sig == signal.SIGKILL or not child.stubborn):
            child.returncode = -int(sig)


@pytest.fixture
def children(monkeypatch):
    """
    The daemon's Popen and os.killpg replaced, and an empty registry of its builds.
    """
    held = Children()
    monkeypatch.setattr(subprocess, "Popen", held.popen)
    monkeypatch.setattr(os, "killpg", held.killpg)
    monkeypatch.setattr(jobs, "CHILDREN", {}, raising=False)
    return held


@pytest.fixture
def root(rig, tmp_path, monkeypatch, children):
    """
    The settings pointing at a scratch state root, every command stubbed, the build
    processes faked and a fresh supervisor. Answers the state root and the commands.
    """
    monkeypatch.setattr(supervisor, "INTERVAL", 0)
    commands = Commands()
    monkeypatch.setattr(shell, "run", commands.run)
    state_root = str(tmp_path / "state")
    backend.Backend().settings(body={"state_root": state_root, "images_root": str(tmp_path / "images")})
    return state_root, commands


def _running(state_root: str, name: str, result: dict = None, started_ago: int = 0,
             request_id: str = None, child: FakeChild = None) -> str:
    """
    A running job, held by the child it is given, or by none where the daemon never
    started it; with a result where the runner has ended, and its start moved back
    where the test wants it old.
    """
    jobs.create(state_root, name, REQUEST, VERDICT)
    jobdir = jobs.path(state_root, name)
    jobs.move(jobdir, "running", pid=child.pid if child else DEAD_PID, request_id=request_id)
    if child:
        jobs.CHILDREN[name] = child
    if result is not None:
        jobs.files.write(os.path.join(jobdir, jobs.RESULT), json.dumps(result))
    if started_ago:
        jobs.update(jobdir, started=_ago(started_ago))
    return jobdir


def _said(watcher: supervisor.Supervisor, passes: int = 1) -> list:
    """
    Run the supervisor that many times and answer what it, the job directory and the
    backend logged at error or above.
    """
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    # The backend's logger is the parent of the other two, so one handler on it sees
    # every record once.
    loggers = (supervisor.LOGGER, jobs.LOGGER, backend.LOGGER)
    levels = [logger.level for logger in loggers]
    backend.LOGGER.addHandler(handler)
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
    try:
        for _ in range(passes):
            watcher.once()
    finally:
        backend.LOGGER.removeHandler(handler)
        for logger, level in zip(loggers, levels):
            logger.setLevel(level)
    return [record.getMessage() for record in records if record.levelno >= logging.ERROR]


def _luna(monkeypatch) -> list:
    """
    Luna's status route replaced; answers the messages posted to it, in order.
    """
    said = []

    class Luna:
        def post(self, uri, payload=None):
            said.extend(item["message"] for item in payload["monitor"]["status"]["messages"])
            return {"message": "success"}

    monkeypatch.setattr(push, "luna", Luna)
    return said


@pytest.mark.parametrize("result, state, exit_code, reason", [
    ({"ok": True, "exit": 0, "reason": "", "kernelversion": "5.14.0-503.el9"}, "done", 0, ""),
    ({"ok": False, "exit": 1, "reason": "ansible-playbook exited 2"}, "failed", 1,
     "ansible-playbook exited 2"),
    ({"ok": False, "exit": 143, "reason": "cancelled"}, "cancelled", 143, "cancelled"),
])
def test_a_build_that_ended_is_recorded_from_what_the_runner_left(root, children, result, state,
                                                                   exit_code, reason):
    """
    A child that ended with a result is done or failed as the result says, and cancelled
    when the runner was stopped; the child is reaped and nothing is signalled. The reader
    then sees the kernel version the runner recorded.
    """
    state_root, commands = root
    child = children.spawn(code=exit_code)
    jobdir = _running(state_root, "rocky9", result, child=child)
    watcher = supervisor.Supervisor()
    watcher.once()
    held = jobs.read_state(jobdir)
    assert (held["state"], held["exit"], held["reason"]) == (state, exit_code, reason)
    assert held["finished"]
    assert child.polled and "rocky9" not in jobs.CHILDREN and children.signals == []
    if state == "done":
        assert backend.Backend().show("rocky9")["kernelversion"] == "5.14.0-503.el9"
    assert watcher.state()["running"] == 1 and watcher.state()["answering"] is True


def test_a_child_that_ended_without_a_result_is_failed(root, children):
    """
    A build process that is gone and left no result was interrupted: failed with that
    reason, the child reaped and nothing signalled.
    """
    state_root, commands = root
    child = children.spawn(code=3)
    jobdir = _running(state_root, "rocky9", child=child)
    supervisor.Supervisor().once()
    held = jobs.read_state(jobdir)
    assert (held["state"], held["reason"]) == ("failed", "the build process ended without a result")
    assert held["finished"]
    assert child.polled and "rocky9" not in jobs.CHILDREN and children.signals == []


@pytest.mark.parametrize("stubborn, sent", [
    (False, [signal.SIGTERM]),
    (True, [signal.SIGTERM, signal.SIGKILL]),
], ids=["ends on SIGTERM", "needs SIGKILL"])
def test_a_build_over_its_limit_is_stopped(root, children, stubborn, sent):
    """
    Past the run limit a live build is signalled through its group, killed when it does
    not end on SIGTERM, and failed with that reason over whatever the runner then wrote.
    A build is never stopped for want of a reader: Luna follows it through the status it
    pushes.
    """
    state_root, commands = root
    children.stubborn = stubborn
    child = children.spawn()
    jobdir = _running(state_root, "rocky9", {"ok": False, "exit": 143, "reason": "cancelled"},
                      started_ago=22000, child=child)
    said = _said(supervisor.Supervisor())
    assert children.signals == [(child.pid, item) for item in sent]
    held = jobs.read_state(jobdir)
    assert (held["state"], held["exit"], held["reason"]) == ("failed", 143, "exceeded 21600 s")
    assert "stopping the build of rocky9: exceeded 21600 s" in said


def test_a_live_build_within_its_limit_is_left_alone(root, children):
    """
    A child that is still running and has time left is not touched, on any pass.
    """
    state_root, commands = root
    child = children.spawn()
    jobdir = _running(state_root, "rocky9", {"ok": True, "exit": 0}, started_ago=60, child=child)
    watcher = supervisor.Supervisor()
    _said(watcher, passes=2)
    assert jobs.read_state(jobdir)["state"] == "running"
    assert child.polled and children.signals == [] and jobs.CHILDREN["rocky9"] is child


def test_a_child_that_ended_before_its_limit_ran_out_is_recorded_not_stopped(root, children):
    """
    A build whose limit passed after its child ended is recorded from what the runner
    left, nothing is signalled, and the jobs after it in the pass are still handled.
    """
    state_root, commands = root
    first = _running(state_root, "aaa", {"ok": True, "exit": 0, "reason": ""}, started_ago=22000,
                     child=children.spawn(code=0))
    later = _running(state_root, "bbb", {"ok": False, "exit": 1, "reason": "ansible-playbook exited 2"},
                     started_ago=22000, child=children.spawn(code=1))
    watcher = supervisor.Supervisor()
    said = _said(watcher)
    assert jobs.read_state(first)["state"] == "done"
    assert (jobs.read_state(later)["state"], jobs.read_state(later)["reason"]) == (
        "failed", "ansible-playbook exited 2")
    assert children.signals == [] and said == [] and watcher.state()["answering"] is True


def test_a_prepared_job_older_than_an_hour_is_forgotten_and_an_old_one_pruned(root):
    """
    Written an hour ago and its unit never started: gone. Ended longer ago than kept:
    gone. Ended yesterday: kept.
    """
    state_root, commands = root
    for name, field, seconds in (("stale", "prepared", 2 * 3600), ("old", "finished", 20 * 86400),
                                 ("recent", "finished", 86400)):
        jobs.create(state_root, name, REQUEST, VERDICT)
        jobdir = jobs.path(state_root, name)
        if field == "finished":
            jobs.move(jobdir, "running")
            jobs.move(jobdir, "done")
        state = jobs.read_state(jobdir)
        state[field] = _ago(seconds)
        jobs.files.write(os.path.join(jobdir, jobs.STATE), json.dumps(state))
    supervisor.Supervisor().once()
    assert jobs.names(state_root) == ["recent"]
    assert commands.of("findmnt") == [["findmnt", "-rn", "-o", "TARGET"]] * 2


def test_what_the_runner_left_mounted_is_lazily_unmounted_and_said(root, children):
    """
    A build that ended with a mount still below the job's mnt: unmounted lazily, said
    loudly, and what the table still shows afterwards said too.
    """
    state_root, commands = root
    jobdir = _running(state_root, "rocky9", {"ok": False, "exit": 1, "reason": "killed"},
                      child=children.spawn(code=1))
    left = os.path.join(os.path.realpath(jobdir), "mnt", "0")
    tables = [f"/\n{left}", f"/\n{left}", "/"]
    commands.answers["findmnt"] = lambda: Ran(tables.pop(0))
    said = _said(supervisor.Supervisor())
    assert ["umount", "-l", "--", left] in commands.ran
    assert jobs.read_state(jobdir)["state"] == "failed"
    assert said == [f"still mounted below {jobdir} after a lazy unmount: {left}"]


def test_a_pass_that_cannot_run_is_said_once_and_the_next_one_still_runs(root, children):
    """
    A controller without findmnt: the prune fails the pass, health says so, the daemon
    and the next pass live on, the build that was being watched is left running, and the
    failure is logged once rather than every fifteen seconds. Answering again is said too.
    """
    state_root, commands = root
    _running(state_root, "rocky9", child=children.spawn())
    jobs.create(state_root, "stale", REQUEST, VERDICT)
    state = jobs.read_state(jobs.path(state_root, "stale"))
    state["prepared"] = _ago(2 * 3600)
    jobs.files.write(os.path.join(jobs.path(state_root, "stale"), jobs.STATE), json.dumps(state))
    commands.answers["findmnt"] = BackendFailure("findmnt is not installed")
    watcher = supervisor.Supervisor()
    said = _said(watcher, passes=3)
    assert said == ["the image build supervisor could not complete a pass: findmnt is not installed"]
    assert watcher.state()["answering"] is False and watcher.state()["last_pass"] > 0
    assert jobs.read_state(jobs.path(state_root, "rocky9"))["state"] == "running"
    commands.answers["findmnt"] = Ran("")
    watcher.once()
    assert watcher.state()["answering"] is True
    assert jobs.names(state_root) == ["rocky9"]


def test_a_build_that_cannot_be_watched_does_not_stop_the_others_or_the_prune(root, children):
    """
    One job whose pass raises is said with its name, once, while the next job is still
    recorded and the old directories are still forgotten; the supervisor then says it is
    not answering, and says so no more once the job can be watched.
    """
    state_root, commands = root
    broken = children.spawn()
    broken.poll_error = OSError("the process table is unreadable")
    _running(state_root, "a-broken", child=broken)
    ended = _running(state_root, "b-ended", {"ok": True, "exit": 0, "reason": ""},
                     child=children.spawn(code=0))
    jobs.create(state_root, "stale", REQUEST, VERDICT)
    state = jobs.read_state(jobs.path(state_root, "stale"))
    state["prepared"] = _ago(2 * 3600)
    jobs.files.write(os.path.join(jobs.path(state_root, "stale"), jobs.STATE), json.dumps(state))
    watcher = supervisor.Supervisor()
    said = _said(watcher, passes=2)
    assert said == ["the image build supervisor could not watch the build of a-broken: "
                    "the process table is unreadable"]
    assert jobs.read_state(ended)["state"] == "done"
    assert jobs.names(state_root) == ["a-broken", "b-ended"]
    assert watcher.state()["answering"] is False

    broken.poll_error = None
    watcher.once()
    assert watcher.state()["answering"] is True


def test_a_missing_state_root_reads_as_no_jobs(root):
    """
    A daemon that never built has no state root: a pass finds nothing, runs nothing and
    is answering; the listing says no jobs and the limit.
    """
    state_root, commands = root
    assert not os.path.exists(state_root)
    watcher = supervisor.Supervisor()
    watcher.once()
    assert commands.ran == []
    state = watcher.state()
    assert set(state) >= {"answering", "last_pass"}
    assert state["answering"] is True and state["running"] == 0 and state["limit"] == 2
    assert backend.Backend().listing() == {"jobs": [], "running": 0, "limit": 2}


def test_the_first_pass_runs_at_once_and_the_thread_stops_when_asked(root, monkeypatch):
    """
    Started, the supervisor passes before its first wait; shut down, the thread ends. At
    interval zero it does not start at all.
    """
    state_root, commands = root
    watcher = supervisor.Supervisor()
    assert watcher.start() is False
    monkeypatch.setattr(supervisor, "INTERVAL", 60)
    assert watcher.start() is True
    for _ in range(50):
        if watcher.last_pass:
            break
        time.sleep(0.05)
    assert watcher.last_pass > 0, "no pass ran at start"
    assert watcher.start() is False
    watcher.shutdown()
    watcher.thread.join(timeout=2)
    assert not watcher.thread.is_alive()


def test_a_build_that_ended_is_recorded_and_then_told_to_luna(root, children, monkeypatch):
    """
    The pass records the build that ended and the push that follows it delivers the lines,
    sets the kernel version and asks for the pack on the stream of the request; the stream
    is then the pack chain's.
    """
    calls = []
    held = {"config": {"osimage": {"rocky9": {}}}}

    class Luna:
        def post(self, uri, payload=None):
            if uri == "config/osimage/rocky9/_pack":
                calls.append(("PACK", uri, payload))
                return {"message": "queued"}
            if uri != "monitor/status":
                calls.append(("SET", uri))
                held.update(payload)
                return None
            calls.append(("POST", payload["monitor"]["status"]["request_id"],
                          [item["message"] for item in payload["monitor"]["status"]["messages"]]))
            return {"message": "success"}

        def get(self, uri, params=None):
            if uri == "config/osimage/rocky9":
                return held
            calls.append(("GET", uri))
            return None

    monkeypatch.setattr(push, "luna", Luna)
    state_root, commands = root
    jobdir = _running(state_root, "rocky9", {"ok": True, "exit": 0, "reason": "",
                                             "kernelversion": "5.14.0-503.el9"},
                      request_id=REQUEST_ID, child=children.spawn(code=0))
    jobs.files.write(os.path.join(jobdir, jobs.PROGRESS),
                     json.dumps({"seq": 0, "message": "PLAY [x]", "status": 200}) + "\n")
    watcher = supervisor.Supervisor()
    watcher.once()
    for _ in range(3):
        watcher.relay()
    assert calls == [("POST", REQUEST_ID, ["PLAY [x]"]),
                     ("SET", "config/osimage/rocky9"),
                     ("PACK", "config/osimage/rocky9/_pack",
                      {"config": {"osimage": {"rocky9": {"request_id": REQUEST_ID}}}})]
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


def test_a_push_that_fails_is_said_once_and_the_builds_go_on(root, children, monkeypatch):
    """
    A failure of the push itself, which is not Luna being away, is logged for the first pass
    only and does not stop the passes over the builds.
    """
    state_root, commands = root
    jobdir = _running(state_root, "rocky9", {"ok": True, "exit": 0, "reason": ""},
                      child=children.spawn(code=0))
    watcher = supervisor.Supervisor()

    def broken(_):
        raise RuntimeError("state.json is not JSON")

    monkeypatch.setattr(watcher.pusher, "relay", broken)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    supervisor.LOGGER.addHandler(handler)
    try:
        watcher.relay()
        watcher.relay()
        watcher.once()
    finally:
        supervisor.LOGGER.removeHandler(handler)
    said = [record.getMessage() for record in records if record.levelno >= logging.ERROR]
    assert said == ["the image build supervisor could not tell Luna: state.json is not JSON"]
    assert jobs.read_state(jobdir)["state"] == "done"


def test_a_build_this_daemon_holds_no_process_for_is_left_alone(root, children, monkeypatch):
    """
    A job recorded as running with no process in the registry, which is what a restart of
    the daemon leaves, is not finished by any pass: it stays running, Luna hears nothing
    and nothing is signalled. The cancel route is what clears it.
    """
    state_root, commands = root
    said = _luna(monkeypatch)
    jobdir = _running(state_root, "rocky9", request_id=REQUEST_ID)
    assert "rocky9" not in jobs.CHILDREN
    before = jobs.read_state(jobdir)
    watcher = supervisor.Supervisor()
    watcher.once()
    watcher.relay()
    assert jobs.read_state(jobdir)["state"] == "running", "the pass finished a build it holds no process for"
    assert jobs.read_state(jobdir) == before and said == []
    watcher.once()
    watcher.relay()
    assert jobs.read_state(jobdir) == before and said == []
    assert children.signals == [] and children.calls == []
