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
What a build tells Luna: its stage lines, in batches, to the status stream of the request
that started it, a sign of life while it is silent, and at the end the kernel version and
the pack, asked for on that same stream so Luna's pack chain goes on writing to it and
ends it. A build that fails, or whose pack does not start, is ended here instead, with a
line that says why and EOF. The job's own state file keeps how far the lines were
delivered, so a restart of the daemon loses none. Delivery is at least once: a crash
between Luna taking a batch and the offset being written sends that one batch again. A
build that ended while the daemon was down is still told.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import time
import urllib.parse

from trixd.common import logging as trixlog
from trixd.common.errors import ServiceUnavailable
from trixd.plugins.luna.client import current as luna
from trixd.plugins.osimage import jobs

LOGGER = trixlog.get("osimage.push")

STATUS_ROUTE: str = "monitor/status"
# The record's kernel version is set through its ordinary update, not the kernel route; the
# pack is asked for with a POST naming the stream, where a GET would open one of its own.
RECORD_ROUTE: str = "config/osimage/{name}"
PACK_ROUTE: str = "config/osimage/{name}/_pack"
END: str = "EOF"
# The status of the line that says a build or its pack did not end well, as Luna words its own
# failed or cancelled creates; every other line is stored as Luna stores a line with none.
FAILURE: int = 501
# How many lines one POST carries, and how long a pack Luna does not answer is asked for.
BATCH_MOST: int = 100
PACK_PATIENCE: int = 120
# How long a running build may be silent before Luna is told it goes on: Luna sweeps a
# stream nothing was written to for an hour.
HEARTBEAT: int = 600
# A message is a line of the book's output, which a failed task can make very long.
MESSAGE_MOST: int = 1000
# The lines kept for the show route and not sent: one line per stage goes to the operator,
# not one per task.
TASKS: tuple = ("TASK [", "RUNNING HANDLER [")
# How Luna's client words an answer at or above 400, as against no answer at all, and the
# answers that are an outage all the same: Luna itself, or its gateway, broken for a moment.
ANSWERED: str = "Luna answered "
TRANSIENT: tuple = ("500", "502", "504")

# What a job's closing holds once the stream is no longer this daemon's to write to, and
# while the ending is this daemon's and being written: a restart then writes it again and
# asks for no pack.
CLOSED: str = "closed"
ENDING: str = "ending"


class Unanswered(Exception):
    """
    Luna did not take what was sent; that job waits for the next pass.
    """


class Pusher:
    """
    One pass over the jobs that still have something to tell, in the order of the names.
    """

    def __init__(self) -> None:
        """
        Luna assumed answering, and no pack asked for yet.
        """
        self.down: bool = False
        self.took: bool = False
        self.packing: dict = {}

    def relay(self, state_root: str) -> None:
        """
        One batch for each job that has lines, and the end of each job that has ended. A
        job Luna does not take is left for the next pass and the others still go; the
        outage ends when a pass is taken whole.
        """
        self.took = refused = False
        for held in jobs.unclosed(state_root):
            try:
                self._job(jobs.path(state_root, held["name"]), held)
            except Unanswered:
                refused = True
        if self.down and self.took and not refused:
            LOGGER.info("Luna is taking the status of the builds again")
            self.down = False

    def _job(self, jobdir: str, held: dict) -> None:
        """
        The next batch of one job's lines; once none are left and the job has ended, its
        end; while it runs and has been silent for the heartbeat, a sign of life.
        """
        lines = jobs.lines_since(jobdir, held.get("pushed", 0))[0][:BATCH_MOST]
        if lines:
            fields = {"pushed": lines[-1]["seq"] + 1}
            said = [line["message"] for line in lines if not line["message"].startswith(TASKS)]
            if said:
                self._send(held["request_id"], said)
                fields["posted"] = jobs.stamp()
            jobs.update(jobdir, **fields)
        elif held["state"] in jobs.TERMINAL:
            self._end(jobdir, held)
        elif held["state"] == "running" \
                and jobs.seconds_since(held.get("posted") or held["started"]) >= HEARTBEAT:
            self._send(held["request_id"],
                       [f"still building {held['name']}: {jobs.read_phase(jobdir)}"])
            jobs.update(jobdir, posted=jobs.stamp())

    def _end(self, jobdir: str, held: dict) -> None:
        """
        A build that ended: the pack when it ended well, and Luna's pack chain then owns the
        stream and ends it; otherwise the line that says why, and EOF. A restart that finds
        the ending half written writes it again and asks for no second pack.
        """
        name = held["name"]
        if held["state"] == "done":
            if held.get("closing") != ENDING:
                failure = self._pack(name, held["request_id"],
                                     jobs.read_json(jobdir, jobs.RESULT).get("kernelversion"))
                if not failure:
                    jobs.update(jobdir, closing=CLOSED)
                    self.packing.pop(name, None)
                    return
                held = jobs.update(jobdir, closing=ENDING, reason=failure)
            said = (f"{name} is built but its pack did not start and the create failed: "
                    f"{held['reason']}")
        elif held["state"] == "cancelled":
            said = f"the build of {name} was cancelled"
        else:
            reason = held.get("reason") or "no reason recorded"
            said = f"the build of {name} failed: {reason}"
        self._send(held["request_id"], [said, END], FAILURE)
        jobs.update(jobdir, closing=CLOSED)
        self.packing.pop(name, None)

    def _pack(self, name: str, request_id: str, version: str) -> str:
        """
        Set the kernel version the build found on Luna's record, once Luna is seen to hold
        the record, then ask Luna to pack the image on the stream of the request. Answers
        why the pack did not start, or nothing when it did. A Luna that does not answer,
        or answers that it is broken for the moment, is asked again by the next pass for
        a while, because the build is done and only the pack is missing; the version is
        set again then, which changes nothing. A Luna that answers and refuses is not
        asked again: no pack chain exists, so the stream is ended here.
        """
        if not version:
            LOGGER.error("the result of %s names no kernel version, so it is not packed", name)
            return "the build left no kernel version to set on the image"
        quoted = urllib.parse.quote(name, safe="")
        try:
            # The update route creates a record it does not hold, so one removed during
            # the build is looked for before it is written to.
            if name not in self._record(quoted).get("config", {}).get("osimage", {}):
                LOGGER.error("Luna does not hold an image %s, so it is not packed", name)
                return "Luna does not hold the image"
            luna().post(RECORD_ROUTE.format(name=quoted),
                        {"config": {"osimage": {name: {"kernelversion": version}}}})
            # Luna answers a change with no body, as it does a missing record: read it back.
            held = self._record(quoted).get("config", {}).get("osimage", {}).get(name, {})
            if held.get("kernelversion") != version:
                LOGGER.error("Luna does not hold an image %s with the kernel version %s", name, version)
                return "Luna does not hold the image"
            answer = luna().post(PACK_ROUTE.format(name=quoted),
                                 {"config": {"osimage": {name: {"request_id": request_id}}}})
        except ServiceUnavailable as error:
            if _outage(error.message):
                first = self.packing.setdefault(name, time.monotonic())
                if time.monotonic() - first < PACK_PATIENCE:
                    self._unanswered(error.message)
            LOGGER.error("the pack of %s did not start: %s", name, error.message)
            return error.message
        if answer is None:
            LOGGER.error("Luna refused the pack of %s", name)
            return "Luna answered 404"
        self._answering()
        LOGGER.info("the pack of %s runs on the stream of %s", name, request_id)
        return ""

    @staticmethod
    def _record(quoted: str) -> dict:
        """
        Luna's osimage record of that name, or nothing where Luna holds none.
        """
        return luna().get(RECORD_ROUTE.format(name=quoted)) or {}

    def _send(self, request_id: str, messages: list, status: int = None) -> None:
        """
        Lines into the status stream of a request. An entry carries the message, and the
        status when one is given, but not on EOF; the remote fields never: Luna reads an
        entry with them set as another host's stream.
        """
        body = {"monitor": {"status": {"request_id": request_id, "messages": [
            {"message": text[:MESSAGE_MOST], **({"status": status} if status and text != END else {})}
            for text in messages]}}}
        try:
            answer = luna().post(STATUS_ROUTE, body)
        except ServiceUnavailable as error:
            self._unanswered(error.message)
        if answer is None:
            self._unanswered(f"Luna does not know {STATUS_ROUTE}")
        self._answering()

    def _unanswered(self, why: str) -> None:
        """
        Luna did not take it: said loudly once for the outage, and the job is left.
        """
        if not self.down:
            LOGGER.error("Luna is not taking the status of the builds, which go on and are "
                         "told later: %s", why)
        self.down = True
        raise Unanswered(why)

    def _answering(self) -> None:
        """
        Luna took something.
        """
        self.took = True


def _outage(message: str) -> bool:
    """
    Whether a failure of Luna's client is Luna being away for the moment, to be asked
    again, rather than an answer that refuses: no answer at all, or one of the transient
    codes.
    """
    if not message.startswith(ANSWERED):
        return True
    return message[len(ANSWERED):].split(" ")[0] in TRANSIENT
