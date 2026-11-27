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
The trigger that finishes what the build processes leave: one that ended is recorded, a
build over its limit is stopped, and old job directories are forgotten. The same thread tells Luna how each build is going.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import threading
import time

from trixd.common import logging as trixlog
from trixd.plugins.osimage import backend, jobs, push
from trixd.utils import threads

LOGGER = trixlog.get("osimage.supervisor")

# How often the running builds are looked at. 0 switches it off, which is what a test wants.
INTERVAL: int = 15
# How often Luna is told, which is also how often the thread wakes: a job's lines are sent
# at most this often, in batches.
PUSH_INTERVAL: int = 2


class Supervisor:
    """
    One pass over the job directories per interval, the first at once.
    """

    def __init__(self) -> None:
        """
        The supervisor, not yet running.
        """
        self.stop = threading.Event()
        self.thread = None
        self.last_pass: float = 0.0
        self.failing: bool = False
        self.relaying: bool = True
        self.pusher = push.Pusher()
        self.broken: set = set()
        self.running: int = 0
        self.limit: int = backend.DEFAULT_SETTINGS["concurrency"]

    def once(self) -> None:
        """
        One pass, guarded: a pass that fails is said once and the next one still runs,
        because a controller without findmnt must not lose the daemon.
        """
        self.last_pass = time.time()
        try:
            self._pass()
        except Exception as error:
            if not self.failing:
                LOGGER.error("the image build supervisor could not complete a pass: %s", error)
            self.failing = True
            return
        # A build that cannot be watched keeps the supervisor from answering, though the
        # pass itself completed.
        if self.failing and not self.broken:
            LOGGER.info("the image build supervisor is passing again")
        self.failing = bool(self.broken)

    def relay(self) -> None:
        """
        One push to Luna, guarded like a pass: a pusher that fails is said once, and the
        builds, which do not depend on it, go on.
        """
        try:
            self.pusher.relay(backend.held_settings()["state_root"])
        except Exception as error:
            if self.relaying:
                LOGGER.error("the image build supervisor could not tell Luna: %s", error)
            self.relaying = False
            return
        if not self.relaying:
            LOGGER.info("the image build supervisor is telling Luna again")
        self.relaying = True

    def _pass(self) -> None:
        """
        Each running build against its process and its limit, then the prune. A build that
        cannot be watched is said once and does not stop the others.
        """
        settings = backend.held_settings()
        state_root = settings["state_root"]
        watched = jobs.running(state_root)
        self.running, self.limit = len(watched), settings["concurrency"]
        for held in watched:
            name = held["name"]
            try:
                self._watch(jobs.path(state_root, name), held, settings)
            except Exception as error:
                if name not in self.broken:
                    LOGGER.error("the image build supervisor could not watch the build of %s: %s",
                                 name, error)
                self.broken.add(name)
            else:
                self.broken.discard(name)
        forgotten = jobs.prune(state_root, settings["keep_days"])
        if forgotten:
            LOGGER.info("forgot %s", ", ".join(forgotten))

    def _watch(self, jobdir: str, held: dict, settings: dict) -> None:
        """
        One running build: one this daemon holds no process for is left alone, a live
        one is stopped when it has run too long, and one that ended is recorded from
        what the runner left.
        """
        name = held["name"]
        child = jobs.child(name)
        if child is None:
            # A restart of the daemon is the operator's doing; the cancel route clears the job.
            return
        code = child.poll()
        if code is None:
            if jobs.seconds_since(held["started"]) > settings["limit_seconds"]:
                self._stop(jobdir, held, f"exceeded {settings['limit_seconds']} s")
            return
        result = jobs.read_json(jobdir, jobs.RESULT)
        if not result:
            LOGGER.error("the build process of %s ended with status %s and left no result: "
                         "interrupted", name, code)
            jobs.finish(jobdir, "failed", "the build process ended without a result")
        elif result.get("ok"):
            jobs.finish(jobdir, "done")
        elif result.get("exit") == jobs.EXIT_CANCELLED:
            jobs.finish(jobdir, "cancelled")
        else:
            jobs.finish(jobdir, "failed", result.get("reason") or "failed")
        jobs.drop(name)

    def _stop(self, jobdir: str, held: dict, reason: str) -> None:
        """
        Stop a build for a reason of this daemon's, and record that reason over whatever
        the runner then wrote.
        """
        LOGGER.error("stopping the build of %s: %s", held["name"], reason)
        jobs.stop(held["name"], jobdir)
        jobs.finish(jobdir, "failed", reason)

    def baseline(self) -> None:
        """
        Nothing to measure before the reconcile: the first pass is the baseline.
        """

    def _loop(self) -> None:
        """
        The first pass at once, then a push every push interval and a pass every interval
        until the daemon stops.
        """
        self.once()
        self.relay()
        while not self.stop.wait(PUSH_INTERVAL):
            if time.time() - self.last_pass >= INTERVAL:
                self.once()
            self.relay()

    def start(self) -> bool:
        """
        Begin supervising, unless the interval says not to.
        """
        if INTERVAL <= 0:
            LOGGER.info("the image build supervisor is switched off")
            return False
        if self.thread is not None and self.thread.is_alive():
            LOGGER.info("the image build supervisor is already running")
            return False
        self.stop.clear()
        self.thread = threads.Thread(target=self._loop, name="trixd-osimage", daemon=True)
        self.thread.start()
        LOGGER.info("supervising image builds every %ss", INTERVAL)
        return True

    def shutdown(self) -> None:
        """
        Ask the supervisor to stop; it ends at its next wake-up. The builds are left as they are.
        """
        self.stop.set()

    def state(self) -> dict:
        """
        What the supervisor is doing, for the health endpoint.
        """
        return {"answering": not self.failing and self.last_pass > 0,
                "last_pass": self.last_pass,
                "running": self.running,
                "limit": self.limit}


_supervisor: Supervisor = Supervisor()


def current() -> Supervisor:
    """
    The image build supervisor for this process.
    """
    return _supervisor
