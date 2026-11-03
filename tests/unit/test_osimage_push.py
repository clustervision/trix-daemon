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
What a build tells Luna, against a Luna that is a list of calls: the stage lines in
batches and from where they were left, the sign of life of a silent build, the pack of a
build that finished asked for on the stream of its request and nothing after it, the end
of a build that failed, was cancelled or whose pack did not start, a Luna that does not
answer, and a build that ended while the daemon was down.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import logging
import os

import pytest

from trixd.common.errors import ServiceUnavailable
from trixd.plugins.osimage import jobs, push

from tests.unit.test_osimage_runner import REQUEST, VERDICT, _ago

REQUEST_ID = "1761818733.1234567891234"
STATUS = "monitor/status"
RECORD = "config/osimage/rocky9"
PACK = "config/osimage/rocky9/_pack"
VERSION = "5.14.0-503.el9"
SET_VERSION = {"config": {"osimage": {"rocky9": {"kernelversion": VERSION}}}}
PACK_BODY = {"config": {"osimage": {"rocky9": {"request_id": REQUEST_ID}}}}
FATAL = 'fatal: [rocky9.osimages.luna]: FAILED! => {"msg": "no"}'


class Luna:
    """
    Luna as the daemon's client sees it: every call is kept in order, and each kind can be
    made to fail the way a Luna that is down, or one that refuses the pack, does.
    """

    def __init__(self) -> None:
        """
        Answering, and starting every pack.
        """
        self.calls: list = []
        self.entries: list = []
        self.down: bool = False
        self.pack_down: bool = False
        self.pack_refused: int = 0
        self.refused: set = set()
        self.pack_answer = {"message": "queued", "request_id": REQUEST_ID}
        self.status_answer = {"message": "success"}
        self.record_held = True
        self.record = {"config": {"osimage": {"rocky9": {"kernelversion": None}}}}

    def post(self, uri: str, payload=None):
        """
        A POST: a status is kept as (verb, uri, request id, messages) and its entries as
        sent, the update of an image record as (SET, uri, body), the pack as (PACK, uri,
        body), unless Luna is down or refuses that request. A refused pack is answered the
        way the client answers a status at or above 400, and a 404 the way it answers one.
        """
        if self.down:
            raise ServiceUnavailable("Luna is not answering: connection refused")
        if uri == PACK:
            if self.pack_down:
                raise ServiceUnavailable("Luna is not answering: connection refused")
            self.calls.append(("PACK", uri, payload))
            if self.pack_refused == 404:
                return None
            if self.pack_refused:
                raise ServiceUnavailable(f"Luna answered {self.pack_refused} for {uri}")
            return self.pack_answer
        if uri != STATUS:
            self.calls.append(("SET", uri, payload))
            if self.record_held:
                self.record = payload
            return None
        status = payload["monitor"]["status"]
        if status["request_id"] in self.refused:
            raise ServiceUnavailable("Luna answered 400 for monitor/status")
        self.entries.extend(status["messages"])
        self.calls.append(("POST", uri, status["request_id"],
                           [item["message"] for item in status["messages"]]))
        return self.status_answer

    def get(self, uri: str, params: dict = None):
        """
        A GET, which is how the record is read, before it is written and back after; a
        record Luna does not hold is answered as the client answers a 404.
        """
        if self.down:
            raise ServiceUnavailable("Luna is not answering: connection refused")
        if uri == RECORD:
            return self.record if self.record_held else None
        self.calls.append(("GET", uri))
        return None

    def posts(self) -> list:
        """
        The messages of every POST to the status route, in order.
        """
        return [call[3] for call in self.calls if call[0] == "POST"]

    def eofs(self) -> int:
        """
        How many times the stream was ended.
        """
        return sum(message == push.END for batch in self.posts() for message in batch)


@pytest.fixture
def luna(monkeypatch):
    """
    The client the pusher talks to, replaced.
    """
    fake = Luna()
    monkeypatch.setattr(push, "luna", lambda: fake)
    return fake


@pytest.fixture
def records():
    """
    What the pusher logs, which a test reads after the run.
    """
    held = []
    handler = logging.Handler()
    handler.emit = held.append
    level = push.LOGGER.level
    push.LOGGER.addHandler(handler)
    push.LOGGER.setLevel(logging.DEBUG)
    yield held
    push.LOGGER.removeHandler(handler)
    push.LOGGER.setLevel(level)


@pytest.fixture
def state_root(tmp_path):
    """
    A state root of the scratch tree.
    """
    return str(tmp_path / "state")


def _job(state_root: str, lines: int = 0, name: str = "rocky9", request_id: str = REQUEST_ID) -> str:
    """
    A running job with that many progress lines, the way the runner writes them.
    """
    jobs.create(state_root, name, REQUEST, VERDICT)
    jobdir = jobs.path(state_root, name)
    jobs.move(jobdir, "running", unit=f"trixd-osimage-{name}", request_id=request_id)
    _say(jobdir, *[f"line {number}" for number in range(lines)])
    return jobdir


def _done(jobdir: str, version: str = VERSION) -> None:
    """
    The job ended well, with the result the runner records, the kernel version in it.
    """
    jobs.files.write(os.path.join(jobdir, jobs.RESULT), json.dumps({"ok": True, "kernelversion": version}))
    jobs.move(jobdir, "done")


def _say(jobdir: str, *messages: str, status: int = 200) -> None:
    """
    Lines appended to the job's progress file, the next sequence numbers on.
    """
    where = os.path.join(jobdir, jobs.PROGRESS)
    seq = len(jobs.lines_since(jobdir)[0])
    with open(where, "a", encoding="utf-8") as handle:
        for offset, message in enumerate(messages):
            handle.write(json.dumps({"seq": seq + offset, "message": message, "status": status}) + "\n")


def test_the_lines_go_in_bounded_batches_to_the_stream_of_the_request(luna, state_root):
    """
    Two hundred and fifty lines are three POSTs of at most the batch size, each to the
    status receiver with the request id, in order, and the offset in the job's state file
    says how far they got. A pass with nothing new says nothing.
    """
    jobdir = _job(state_root, lines=250)
    pusher = push.Pusher()
    for expected in (100, 200, 250):
        pusher.relay(state_root)
        assert jobs.read_state(jobdir)["pushed"] == expected
    pusher.relay(state_root)
    assert [len(batch) for batch in luna.posts()] == [100, 100, 50]
    assert all(call[:3] == ("POST", STATUS, REQUEST_ID) for call in luna.calls)
    assert luna.posts()[0][0] == "line 0" and luna.posts()[2][-1] == "line 249"
    assert len(luna.calls) == 3, "a pass with nothing new still called Luna"


def test_a_restart_goes_on_from_the_offset_and_a_later_line_is_sent_once(luna, state_root):
    """
    A pusher that did not make the first pass, which is what a daemon that restarted is,
    neither repeats the lines delivered nor loses the ones after them.
    """
    jobdir = _job(state_root, lines=150)
    push.Pusher().relay(state_root)
    restarted = push.Pusher()
    restarted.relay(state_root)
    _say(jobdir, "late line")
    restarted.relay(state_root)
    restarted.relay(state_root)
    sent = [message for batch in luna.posts() for message in batch]
    assert sent == [f"line {number}" for number in range(150)] + ["late line"]


def test_the_task_lines_stay_in_the_job_and_the_play_and_fatal_lines_go(luna, state_root):
    """
    One line per stage reaches the operator: the plays, the recap, the fatal that ended
    the run and the runner's own lines go, the tasks and handlers are kept for the show
    route alone, and the offset moves past them. A batch of tasks alone posts nothing.
    """
    jobdir = _job(state_root)
    _say(jobdir, "PLAY [compute]", "TASK [x]", "RUNNING HANDLER [y]", "answers from /x")
    _say(jobdir, FATAL, status=500)
    _say(jobdir, "PLAY RECAP", "ansible-playbook exited 2")
    pusher = push.Pusher()
    pusher.relay(state_root)
    assert luna.posts() == [["PLAY [compute]", "answers from /x", FATAL, "PLAY RECAP",
                             "ansible-playbook exited 2"]]
    assert jobs.read_state(jobdir)["pushed"] == 7
    _say(jobdir, "TASK [a]", "TASK [b]")
    pusher.relay(state_root)
    assert len(luna.posts()) == 1 and jobs.read_state(jobdir)["pushed"] == 9


def test_a_very_long_line_is_cut_so_a_post_stays_small(luna, state_root):
    """
    A failed task can print a whole package manager's output on one line.
    """
    jobdir = _job(state_root)
    _say(jobdir, "x" * 50000)
    push.Pusher().relay(state_root)
    assert len(luna.posts()[0][0]) == push.MESSAGE_MOST


def test_a_build_that_is_done_is_packed_on_the_stream_of_its_request_and_nothing_more_is_said(
        luna, state_root):
    """
    Once the last line is delivered, the kernel version is set on Luna's record and the
    pack is asked for with a POST naming the id Luna sent, once each and in that order.
    Luna's pack chain ends the stream from there: no closing line and no EOF are sent, the
    pack's answer is not read, and nothing is said after that however often the pass runs.
    """
    jobdir = _job(state_root, lines=3)
    _done(jobdir)
    pusher = push.Pusher()
    for _ in range(4):
        pusher.relay(state_root)
    assert luna.calls == [
        ("POST", STATUS, REQUEST_ID, ["line 0", "line 1", "line 2"]),
        ("SET", RECORD, SET_VERSION),
        ("PACK", PACK, PACK_BODY)]
    assert luna.calls[2][2]["config"]["osimage"]["rocky9"]["request_id"] \
        == jobs.read_state(jobdir)["request_id"] == REQUEST_ID
    assert luna.eofs() == 0
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


@pytest.mark.parametrize("state, reason, said", [
    ("failed", "ansible-playbook exited 2: fatal: no", "the build of rocky9 failed: "
                                                        "ansible-playbook exited 2: fatal: no"),
    ("failed", "", "the build of rocky9 failed: no reason recorded"),
    ("cancelled", "cancelled by tester", "the build of rocky9 was cancelled"),
])
def test_a_build_that_failed_or_was_stopped_ends_with_a_failure_line_and_no_pack(
        luna, state_root, state, reason, said):
    """
    The closing line says the build failed and why, in its words, because Luna stores every
    line as a success; one EOF follows, and Luna is never asked to pack a tree that was not
    built.
    """
    jobdir = _job(state_root)
    jobs.move(jobdir, state, reason)
    pusher = push.Pusher()
    pusher.relay(state_root)
    pusher.relay(state_root)
    assert luna.calls == [("POST", STATUS, REQUEST_ID, [said, "EOF"])]
    if state == "failed":
        assert "failed" in said and (reason or "no reason recorded") in said
    assert luna.eofs() == 1
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


def test_every_entry_carries_the_message_alone(luna, state_root):
    """
    An entry with the remote fields set is read by Luna as another host's stream, and a
    status on an entry is not read at all: the lines, the failure line and EOF each go as
    a message and nothing else.
    """
    jobdir = _job(state_root, lines=2)
    _say(jobdir, FATAL, status=500)
    push.Pusher().relay(state_root)
    jobs.move(jobdir, "failed", "ansible-playbook exited 2")
    push.Pusher().relay(state_root)
    assert len(luna.entries) == 5
    for entry in luna.entries:
        assert "remote_request_id" not in entry
        assert "remote_host" not in entry
        assert set(entry) == {"message"}


def test_a_silent_build_is_told_still_building_after_the_heartbeat_and_not_inside_it(
        luna, state_root):
    """
    A running build with nothing new for the heartbeat gets one line naming its phase,
    none inside the bound, and a pusher that did not make the earlier passes, which is
    what a daemon that restarted is, reads when the last post was from the job and does
    not send another; lines delivered count as a post.
    """
    jobdir = _job(state_root)
    jobs.write_phase(jobdir, "compute")
    past = _ago(getattr(push, "HEARTBEAT", 600) + 1)
    pusher = push.Pusher()
    pusher.relay(state_root)
    assert luna.posts() == []
    jobs.update(jobdir, started=past)
    pusher.relay(state_root)
    pusher.relay(state_root)
    push.Pusher().relay(state_root)
    assert luna.posts() == [["still building rocky9: compute"]]
    jobs.update(jobdir, posted=past)
    _say(jobdir, "PLAY [compute]")
    pusher.relay(state_root)
    pusher.relay(state_root)
    assert luna.posts() == [["still building rocky9: compute"], ["PLAY [compute]"]]
    jobs.update(jobdir, posted=past)
    pusher.relay(state_root)
    assert luna.posts()[-1] == ["still building rocky9: compute"]


def test_a_build_nobody_asked_for_is_not_told_to_anybody(luna, state_root):
    """
    A job prepared and started without a request has no stream, so nothing is sent for it.
    """
    jobdir = _job(state_root, lines=3, request_id=None)
    _done(jobdir)
    push.Pusher().relay(state_root)
    assert luna.calls == [] and jobs.read_state(jobdir)["pushed"] == 0


def test_a_luna_that_is_down_costs_the_build_nothing_and_is_said_once(luna, state_root, records):
    """
    Passes against a Luna that does not answer send nothing, move no offset, stop the pass
    at the first refusal and log one error however many there are; when Luna is back every
    line is delivered once, and the recovery is said once.
    """
    jobdir = _job(state_root, lines=5)
    _job(state_root, lines=2, name="other")
    luna.down = True
    pusher = push.Pusher()
    for _ in range(5):
        pusher.relay(state_root)
    assert luna.calls == [] and jobs.read_state(jobdir)["pushed"] == 0
    assert jobs.read_state(jobdir)["state"] == "running"
    errors = [record.getMessage() for record in records if record.levelno >= logging.ERROR]
    assert len(errors) == 1 and "Luna is not taking the status of the builds" in errors[0]

    luna.down = False
    pusher.relay(state_root)
    pusher.relay(state_root)
    assert jobs.read_state(jobdir)["pushed"] == 5
    assert sorted(len(batch) for batch in luna.posts()) == [2, 5]
    recovered = [record.getMessage() for record in records if record.levelno == logging.INFO]
    assert recovered == ["Luna is taking the status of the builds again"]


def test_a_luna_without_the_receiver_is_not_taken_for_delivered(luna, state_root):
    """
    Luna's client answers nothing for a 404; the lines are not counted as delivered.
    """
    jobdir = _job(state_root, lines=2)
    luna.status_answer = None
    push.Pusher().relay(state_root)
    assert jobs.read_state(jobdir)["pushed"] == 0


def test_a_build_that_ended_while_the_daemon_was_down_is_told_in_full(luna, state_root):
    """
    A job whose unit ended, with lines nobody delivered and a result already recorded, is
    found by a pusher that never saw it running: its lines and the pack arrive, and the
    stream is left to the pack chain.
    """
    jobdir = _job(state_root, lines=2)
    _done(jobdir)
    restarted = push.Pusher()
    restarted.relay(state_root)
    restarted.relay(state_root)
    assert [call[0] for call in luna.calls] == ["POST", "SET", "PACK"]
    assert luna.posts() == [["line 0", "line 1"]]


def test_a_luna_that_goes_down_before_the_pack_is_asked_again_and_packs_once(luna, state_root):
    """
    The build is done and only the pack is missing: the pass waits, the next one that finds
    Luna answering asks once, and nothing follows it.
    """
    jobdir = _job(state_root)
    _done(jobdir)
    pusher = push.Pusher()
    luna.pack_down = True
    pusher.relay(state_root)
    pusher.relay(state_root)
    assert [call[0] for call in luna.calls] == ["SET", "SET"]
    luna.pack_down = False
    for _ in range(3):
        pusher.relay(state_root)
    assert [call[0] for call in luna.calls] == ["SET", "SET", "SET", "PACK"]
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


@pytest.mark.parametrize("answered, reason", [
    (400, "Luna answered 400 for config/osimage/rocky9/_pack"),
    (404, "Luna answered 404"),
    (503, "Luna answered 503 for config/osimage/rocky9/_pack"),
])
def test_a_pack_luna_refuses_ends_the_stream_now_with_a_line_saying_so(luna, state_root,
                                                                       answered, reason):
    """
    An answer of 400, 404 or 503 from the pack route means no pack chain exists and
    nothing else will end the stream: the same pass says the pack did not start and why,
    then EOF once, and the pack is not asked for again.
    """
    jobdir = _job(state_root)
    _done(jobdir)
    luna.pack_refused = answered
    pusher = push.Pusher()
    pusher.relay(state_root)
    assert [call[0] for call in luna.calls] == ["SET", "PACK", "POST"]
    line, end = luna.posts()[0]
    assert "pack did not start" in line and reason in line and end == push.END
    pusher.relay(state_root)
    assert [call[0] for call in luna.calls] == ["SET", "PACK", "POST"]
    assert luna.eofs() == 1
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


def test_a_build_without_a_kernel_version_in_its_result_is_not_packed(luna, state_root):
    """
    The version is what Luna's record needs before a pack: a result that names none ends
    the stream with a failure line and EOF, and Luna is asked for neither the update nor
    the pack.
    """
    jobdir = _job(state_root)
    _done(jobdir, version="")
    push.Pusher().relay(state_root)
    assert luna.calls == [("POST", STATUS, REQUEST_ID, [
        "rocky9 is built but its pack did not start and the create failed: the build left no "
        "kernel version to set on the image", "EOF"])]
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


def test_a_record_luna_does_not_hold_ends_the_stream_before_any_write_or_pack(luna, state_root):
    """
    A record removed during the build is found gone before anything is written to it:
    Luna's update route would create one, and the pack would then pack it. The stream ends
    saying so, and neither the version nor the pack is sent.
    """
    jobdir = _job(state_root)
    _done(jobdir)
    luna.record_held = False
    push.Pusher().relay(state_root)
    assert [call[0] for call in luna.calls] == ["POST"], "the record was written or packed"
    assert luna.posts() == [["rocky9 is built but its pack did not start and the create failed: "
                             "Luna does not hold the image", "EOF"]]
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


def test_an_answer_that_luna_itself_is_broken_is_asked_again_and_packs_once(luna, state_root):
    """
    A 500 from Luna on the pack is a transient of Luna's own, like no answer at all: the
    build is done and only the pack is missing, so the stream is not ended and the next
    pass that finds Luna answering packs, once.
    """
    jobdir = _job(state_root)
    _done(jobdir)
    luna.pack_refused = 500
    pusher = push.Pusher()
    pusher.relay(state_root)
    assert [call[0] for call in luna.calls] == ["SET", "PACK"], "the stream was ended on a 500"
    assert luna.eofs() == 0 and jobs.read_state(jobdir)["closing"] != push.CLOSED
    luna.pack_refused = 0
    pusher.relay(state_root)
    pusher.relay(state_root)
    assert [call[0] for call in luna.calls] == ["SET", "PACK", "SET", "PACK"]
    assert luna.eofs() == 0 and jobs.read_state(jobdir)["closing"] == push.CLOSED


def test_a_crash_after_a_refused_pack_writes_the_ending_again_and_asks_for_no_second_pack(
        luna, state_root, monkeypatch):
    """
    The daemon dies between the ending being sent and the closing being written: the
    pusher after the restart sends the line and EOF again, which Luna takes at least once,
    and does not ask for the pack again, which Luna might now accept on a stream that
    already carries an EOF.
    """
    jobdir = _job(state_root)
    _done(jobdir)
    luna.pack_refused = 503
    update = jobs.update
    died = []

    def once(where: str, **fields):
        if fields.get("closing") == push.CLOSED and not died:
            died.append(True)
            raise RuntimeError("the daemon died here")
        return update(where, **fields)
    monkeypatch.setattr(jobs, "update", once)
    with pytest.raises(RuntimeError):
        push.Pusher().relay(state_root)
    push.Pusher().relay(state_root)
    assert [call[0] for call in luna.calls].count("PACK") == 1, "the pack was asked for again"
    assert luna.posts()[-1][-1] == push.END and "pack did not start" in luna.posts()[-1][0]
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


def test_a_job_luna_does_not_take_does_not_hold_back_the_others_and_the_outage_is_said_once(
        luna, state_root, records):
    """
    One stream Luna refuses is left for the next pass while the next job's lines still
    go; the refusal is one log line however many passes it takes, and the job is told
    once Luna takes it.
    """
    stuck = _job(state_root, lines=2, name="a-first", request_id="1761818733.1")
    _job(state_root, lines=2, name="b-second", request_id="1761818733.2")
    luna.refused = {"1761818733.1"}
    pusher = push.Pusher()
    for _ in range(3):
        pusher.relay(state_root)
    assert [call[2] for call in luna.calls] == ["1761818733.2"]
    assert jobs.read_state(stuck)["pushed"] == 0
    assert jobs.read_state(jobs.path(state_root, "b-second"))["pushed"] == 2
    errors = [record for record in records if record.levelno >= logging.ERROR]
    assert len(errors) == 1

    luna.refused = set()
    pusher.relay(state_root)
    assert jobs.read_state(stuck)["pushed"] == 2
    assert [record.getMessage() for record in records if record.levelno == logging.INFO] == [
        "Luna is taking the status of the builds again"]


def test_a_luna_that_stays_away_past_the_patience_ends_with_a_failure_line(luna, state_root,
                                                                           monkeypatch):
    """
    The pack is asked for again while Luna may only be restarting; past the patience the
    build is told as built and not packed, with Luna's own words.
    """
    monkeypatch.setattr(push, "PACK_PATIENCE", 0)
    jobdir = _job(state_root)
    _done(jobdir)
    luna.pack_down = True
    push.Pusher().relay(state_root)
    assert luna.posts() == [["rocky9 is built but its pack did not start and the create failed: "
                             "Luna is not answering: connection refused", "EOF"]]
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED
