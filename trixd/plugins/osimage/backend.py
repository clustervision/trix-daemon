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
The routes of the image build: Luna's create, which writes the job and starts the build
process, show the progress, cancel, list, and the settings. Everything slow, the sources
read included, runs in that process from the job directory, and the supervisor finishes
what it leaves.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import threading

from trixd.common import logging as trixlog
from trixd.common.errors import BadRequest, Conflict, NotFound, ServiceUnavailable
from trixd.plugins.osimage import checkout, jobs, rules, runner
from trixd.utils import declarations

LOGGER = trixlog.get("osimage")

PLUGIN: str = "osimage"

# repo_url has no default: the books run as root, so which archive to fetch and run is a
# setting the installer or the operator names, pinned to a tag, never a branch that moves.
DEFAULT_SETTINGS: dict = {
    "repo": "/trinity/trinityx-combined",
    "repo_url": "",
    "answers": "/trinity/local/backup/group_vars/all.yml",
    "images_root": "/trinity/images",
    "state_root": "/trinity/local/trixd/var/osimage",
    "python": "/trinity/local/python",
    "collections": "",
    "mirrors": {},
    "mirrors_file": "/trinity/local/trixd/daemon/config/osimage-mirrors.yaml",
    "concurrency": 2,
    "limit_seconds": 21600,
    "keep_days": 14,
    "builder": runner.BOOKS,
    "stub_gap_seconds": 3,
}
# The settings that must be absolute paths, and the ones that must be at least one.
PATH_SETTINGS: tuple = ("repo", "answers", "images_root", "state_root", "python", "collections",
                        "mirrors_file")
# The path settings that may be empty: collections then are where ansible looks by itself.
EMPTY_PATH_SETTINGS: tuple = ("collections",)
COUNT_SETTINGS: tuple = ("concurrency", "limit_seconds", "keep_days", "stub_gap_seconds")
# What the runner is handed in request.json: it reads nothing of the daemon's.
RUNNER_SETTINGS: tuple = ("repo", "repo_url", "answers", "images_root", "python", "collections",
                          "builder", "stub_gap_seconds")

# The count of running builds and the start of one are a single step, so two starts
# arriving together cannot both pass the cap.
START_LOCK = threading.Lock()


def held_settings() -> dict:
    """
    The settings as held, over the defaults, so a key added since they were written
    still has a value.
    """
    return dict(DEFAULT_SETTINGS, **(declarations.read(PLUGIN, "settings", "") or {}))


def runner_argv(jobdir: str, settings: dict) -> list:
    """
    The command of the build: the runner, under the interpreter the books are installed for.
    """
    return [os.path.join(settings["python"], "bin", "python3"), "-m",
            "trixd.plugins.osimage.runner", jobdir]


def runner_env(settings: dict) -> dict:
    """
    What the build runs with: the daemon's environment over a home, a PATH that starts
    with that interpreter's directory, and a UTF-8 locale.
    """
    path = ":".join([os.path.join(settings["python"], "bin"), *runner.SYSTEM_PATH])
    return dict(os.environ, HOME="/root", PATH=path, LANG="C.UTF-8")


def tree_under(path: str, images_root: str, name: str) -> str:
    """
    The image directory Luna resolved, once it is an absolute path below images_root with
    no symlink leading out of it, because the build mounts, chroots and removes inside it,
    and is the directory the books build, images_root/<name>, because the result is read
    and packed from the path. Luna's own spelling is kept, so the two sides name one tree.
    """
    root = os.path.realpath(images_root)
    if not os.path.isabs(path) or not os.path.realpath(path).startswith(root + os.sep):
        raise BadRequest(f"path must be an absolute path under images_root ({images_root}): {path}")
    built = os.path.join(images_root, name)
    if os.path.realpath(path) != os.path.realpath(built):
        raise BadRequest(f"path must be the directory the books build for {name}, {built}: {path}")
    return path


def _jobdir(name: str) -> str:
    """
    The directory of a build asked for by name, or 404. The name rule is applied before
    any path is made from it, so no path is built from an unchecked name.
    """
    state_root = held_settings()["state_root"]
    if not rules.NAME_RULE.match(name or "") or not jobs.exists(state_root, name):
        raise NotFound(f"no build named {name}")
    return jobs.path(state_root, name)


class Backend:
    """
    One build per name, in a job directory, run as a child process of the daemon.
    """

    def settings(self, body: dict = None) -> dict:
        """
        Where the builds run and their limits; a change keeps what was not sent.
        """
        if body:
            for key in PATH_SETTINGS:
                if body.get(key) is None or body[key].startswith("/"):
                    continue
                if body[key] or key not in EMPTY_PATH_SETTINGS:
                    raise BadRequest(f"{key} must be an absolute path")
            if body.get("repo_url") and not body["repo_url"].startswith(checkout.SCHEME):
                raise BadRequest("repo_url must be an https address, or empty")
            if body.get("builder") is not None and body["builder"] not in runner.BUILDERS:
                raise BadRequest(f"builder must be one of {', '.join(runner.BUILDERS)}")
            if body.get("mirrors") is not None:
                rules.check_mirrors(body["mirrors"])
            for key in COUNT_SETTINGS:
                if body.get(key) is not None and body[key] < 1:
                    raise BadRequest(f"{key} must be at least 1")
            return declarations.merge(PLUGIN, "settings", "", body, DEFAULT_SETTINGS)
        return held_settings()

    def listing(self) -> dict:
        """
        Every build held, and the count against the limit.
        """
        settings = held_settings()
        held = [jobs.read_state(jobs.path(settings["state_root"], name))
                for name in jobs.names(settings["state_root"])]
        return {"jobs": [{"name": state["name"], "state": state["state"],
                          "started": state["started"]} for state in held],
                "running": len([state for state in held if state["state"] == "running"]),
                "limit": settings["concurrency"]}

    def create(self, body: dict, caller=None) -> dict:
        """
        Luna's build of an image: the body checked, the job written and the build started,
        and nothing else, because Luna waits five seconds for the answer. The tree is the
        path Luna resolved and the stream is Luna's own request id, kept apart from any id
        of this daemon's. The sources are the body's when it names them, else the entry for
        the record in the mirrors file, else the one in the mirrors setting, else none;
        reading them is left to the build.
        """
        name = body["image"]
        arch = rules.check_create(body)
        settings = held_settings()
        tree = tree_under(body["path"], settings["images_root"], name)
        media, mirrors_from, mirrors_file = body.get("media"), rules.GIVEN, None
        if media is None:
            record = (body["distribution"], body["osrelease"], arch)
            mirrors_from, media = rules.entry_for(rules.read_mirrors(settings["mirrors_file"]),
                                                  *record)
            if mirrors_from:
                mirrors_file = settings["mirrors_file"]
            else:
                mirrors_from, media = rules.entry_for(settings["mirrors"], *record)
        request = {"name": name, "request_id": body["request_id"], "path": tree,
                   "distribution": body["distribution"], "osrelease": body["osrelease"],
                   "architecture": arch, "media": media, "mirrors_from": mirrors_from,
                   "mirrors_file": mirrors_file,
                   "settings": {key: settings[key] for key in RUNNER_SETTINGS}}
        with START_LOCK:
            self._below_cap(settings)
            jobs.create(settings["state_root"], name, request, {"arch": arch})
            state = self._launch(name, jobs.path(settings["state_root"], name), settings,
                                 body["request_id"])
        LOGGER.info("%s handed over the build of %s for request %s: pid %s started",
                    getattr(caller, "name", "unknown"), name, body["request_id"], state["pid"])
        return {"name": name, "state": "running", "pid": state["pid"], "started": state["started"]}

    @staticmethod
    def _below_cap(settings: dict) -> None:
        """
        One more build may run, or 409 naming the count. Called under the start lock.
        """
        busy = jobs.running(settings["state_root"])
        if len(busy) >= settings["concurrency"]:
            raise Conflict(f"{len(busy)} builds running, limit {settings['concurrency']}")

    @staticmethod
    def _launch(name: str, jobdir: str, settings: dict, request_id: str) -> dict:
        """
        The build process started on a job just written, and the job running for the stream
        of the request; a build started and not recorded running is stopped again, so none
        is left unwatched. Called under the start lock.
        """
        try:
            child = jobs.start(name, jobdir, runner_argv(jobdir, settings), runner_env(settings))
        except OSError as error:
            raise ServiceUnavailable(f"the build of {name} could not be started: {error}") from None
        try:
            return jobs.move(jobdir, "running", pid=child.pid, request_id=request_id)
        except Exception:
            jobs.stop(name, jobdir)
            raise

    def show(self, name: str, since: int = None) -> dict:
        """
        Where one build is, and its lines from a sequence number on.
        """
        jobdir = _jobdir(name)
        answer = jobs.show(jobdir, since or 0)
        return answer

    def cancel(self, name: str, caller=None) -> dict:
        """
        Stop a build, waiting for its process so the result it records is there to read.
        """
        jobdir = _jobdir(name)
        held = jobs.read_state(jobdir)
        if held["state"] in jobs.TERMINAL:
            raise Conflict(f"build {name} is already {held['state']}")
        who = getattr(caller, "name", "unknown")
        if held["state"] == "running":
            jobs.stop(name, jobdir)
            ended = jobs.finish(jobdir, "cancelled", f"cancelled by {who}")
        else:
            ended = jobs.move(jobdir, "cancelled", f"cancelled by {who}")
        return {"name": name, "state": ended["state"]}
