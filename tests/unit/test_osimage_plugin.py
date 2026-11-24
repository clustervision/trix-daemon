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
The osimage plugin's routes, loaded into the rig through the real loader, with every
command stubbed: no systemd, no ansible, no root and no image tree.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import ast
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from trixd.common import settings as configuration
from trixd.common.constants import API_PREFIX
from trixd.core.plugins import loader
from trixd.core.render import triggers
from trixd.plugins.osimage import backend, detect, jobs, push, rules, runner, supervisor
from trixd.utils import shell

from tests.unit.test_osimage_runner import (COLLECTIONS, GALAXY_ROLE, Commands,  # noqa: F401
                                            sigterm_restored)
from tests.unit.test_osimage_supervisor import children  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(ROOT, "trixd", "plugins", "osimage", "manifest.yaml")
REQUEST_ID = "1761818733.1234567891234"


@pytest.fixture(autouse=True)
def _no_supervisor_thread(monkeypatch):
    """
    The trigger registers when the plugin loads; with the interval at zero it starts no
    thread, so a pass runs only when a test asks for one.
    """
    monkeypatch.setattr(supervisor, "INTERVAL", 0)


@pytest.fixture
def plugin(rig, tmp_path, monkeypatch, children):
    """
    The plugin loaded into the rig, its paths under a scratch tree, the tools, the
    collections and the Galaxy role present (the role under a home of the scratch
    tree's own, so the controller's does not count), every command stubbed and the build
    processes faked. Answers the client, the headers and the commands.
    """
    commands = Commands()
    monkeypatch.setattr(shell, "run", commands.run)
    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    repo = tmp_path / "repo"
    books = repo / "site"
    (books / "plugins" / "connection").mkdir(parents=True)
    (books / "group_vars").mkdir()
    (books / "group_vars" / "all.yml").write_text("trix_version: 15\n")
    (books / "ansible.cfg").write_text("[defaults]\ninventory = hosts\n")
    (books / "plugins" / "connection" / "lchroot.py").write_text("")
    for name in COLLECTIONS:
        (tmp_path / "collections" / "ansible_collections" / name.replace(".", "/")).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(runner, "GALAXY_ROLES", ("~/.ansible/roles",))
    (tmp_path / ".ansible" / "roles" / GALAXY_ROLE).mkdir(parents=True)
    entry = loader.load(rig, "osimage")
    assert entry.state == "loaded", entry.reason
    assert "osimage_jobs" in triggers.states(), "the supervisor was not registered"
    client = TestClient(rig)
    token = client.post("/api/v1/auth/token", json={"username": "tester", "password": "secret"})
    headers = {"x-access-tokens": token.json()["token"]}
    settings = {"repo": str(repo), "repo_url": "", "images_root": str(tmp_path / "images"),
                "state_root": str(tmp_path / "state"), "python": str(tmp_path / "python"),
                "collections": str(tmp_path / "collections")}
    changed = client.patch(f"{API_PREFIX}/osimage/settings", json=settings, headers=headers)
    assert changed.status_code == 200, changed.text
    return client, headers, commands


def _create(client, headers, tmp_path, name: str = "rocky9", **body):
    """
    What Luna's create plugin sends: the six fields of the osimage record, the tree
    under the images root, the architecture in Luna's word, over which the body says; a
    field given as None is left out.
    """
    sent = {"image": name, "request_id": REQUEST_ID, "path": str(tmp_path / "images" / name),
            "distribution": "redhat", "osrelease": "9.6", "architecture": "x86", **body}
    return client.post(f"{API_PREFIX}/osimage/create",
                       json={key: value for key, value in sent.items() if value is not None},
                       headers=headers)


def _manifest() -> dict:
    """
    The shipped manifest, as written.
    """
    with open(MANIFEST, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_the_fixed_routes_are_declared_before_any_name_route():
    """
    A route matched by name would swallow `settings`, `create` and `""`, so the manifest
    declares those first, and `create` is a reserved job name; and each route admits the
    role the design gave it.
    """
    endpoints = _manifest()["endpoints"]
    named = [number for number, item in enumerate(endpoints) if "<name>" in item["path"]]
    fixed = [number for number, item in enumerate(endpoints) if "<name>" not in item["path"]]
    assert fixed and named and max(fixed) < min(named), [item["path"] for item in endpoints]
    assert [item["path"] for item in endpoints[:4]] == ["", "settings", "settings", "create"]
    assert set(rules.RESERVED_NAMES) == {item["path"] for item in endpoints if item["path"]
                                         and "<name>" not in item["path"]}
    minimums = {(item["path"], tuple(item["methods"])): item["minimum"] for item in endpoints}
    assert minimums == {("", ("GET",)): "reader",
                        ("settings", ("GET",)): "reader",
                        ("settings", ("PATCH",)): "rootus",
                        ("create", ("POST",)): "operator",
                        ("<name>", ("GET",)): "reader",
                        ("<name>/actions/cancel", ("POST",)): "operator"}


def test_a_mirrors_key_is_the_create_fields_the_schema_takes():
    """
    The map is keyed by what Luna sends, so the key rule is built of the create schema's
    own patterns for distribution, osrelease and architecture, and drifts with none of them.
    """
    fields = _manifest()["schemas"]["create"]
    parts = [fields[name]["pattern"].strip("^$") for name in ("distribution", "osrelease",
                                                                "architecture")]
    assert rules.MIRROR_KEY.pattern == f"^({parts[0]})/({parts[1]})(?:/({parts[2]}))?$"
    assert rules.MIRROR_KEY.match("redhat/9.6/x86") and rules.MIRROR_KEY.match("ubuntu/24.04")
    assert not rules.MIRROR_KEY.match("redhat") and not rules.MIRROR_KEY.match("redhat/9.6/x86/more")
    assert re.compile(parts[0]).match("redhat") and re.compile(parts[2]).match("x86")


def test_the_body_keys_of_every_schema_collide_with_no_column_and_no_path_variable():
    """
    A name is global: a key of a body that is also a column of the database layout or
    a path variable of any shipped manifest would meet a rule written for the other
    meaning. Enumerated from every schema, so the next key is checked too. Luna's create
    carries its `path` and `request_id`: columns of the audit and replication rows, and
    `request_id` the path variable of the services status route, where it is this
    daemon's own task id. Meant, kept apart in the handler, and listed here so nothing
    else slips in.
    """
    schemas = _manifest()["schemas"]
    meant = {"create": {"path", "request_id"}}
    assert set(meant) <= set(schemas) and all(schemas.values()), "a schema declares nothing"
    with open(os.path.join(ROOT, "trixd", "db", "models.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    columns = {node.target.id for node in ast.walk(tree)
               if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
               and isinstance(node.value, ast.Call)
               and getattr(node.value.func, "id", "") == "mapped_column"}
    assert columns, "no column was found, so this proves nothing"
    variables = set()
    for manifest in glob.glob(os.path.join(ROOT, "trixd", "plugins", "*", "manifest.yaml")):
        with open(manifest, encoding="utf-8") as handle:
            for endpoint in yaml.safe_load(handle).get("endpoints") or []:
                variables.update(endpoint.get("params") or {})
    assert variables & set().union(*meant.values()), "no meant key is a path variable, so the list is stale"
    for name, fields in schemas.items():
        keys = set(fields)
        assert keys & (columns | variables) == meant.get(name, set()), \
            f"a key of {name} is also a column or a path variable: {keys & (columns | variables)}"


def test_a_create_from_luna_is_taken_at_once_with_nothing_detected_or_asked(plugin, tmp_path, monkeypatch,
                                                                           children):
    """
    Luna waits five seconds for the answer, so the accept checks the body, writes the job
    and starts the build process, and reads no source, probes no tool and reaches nothing
    over the network. The job carries Luna's request id as the stream, its path as the
    tree and its architecture in the books' spelling; the process is the runner, with the
    environment the books need. Luna calls under the account trixd.ini names, which is the
    one used here.
    """
    client, headers, commands = plugin

    def never(*args, **kwargs):
        raise AssertionError("the accept must not detect, probe or reach the network")
    monkeypatch.setattr(detect, "identify_all", never)
    monkeypatch.setattr(runner, "preconditions", never)
    monkeypatch.setattr(backend.checkout, "check_url", never)
    monkeypatch.setattr(push, "luna", never)
    monkeypatch.setattr(socket.socket, "connect", never)
    assert configuration.current().api.username == "tester", "the rig's account is not the ini one"
    began = time.monotonic()
    answer = _create(client, headers, tmp_path)
    assert answer.status_code == 200, answer.text
    assert time.monotonic() - began < 5
    body = answer.json()
    child = children.made[0]
    assert (body["name"], body["state"], body["pid"]) == ("rocky9", "running", child.pid)
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")
    python = str(tmp_path / "python")
    assert commands.ran == []
    assert len(children.calls) == 1
    argv, kwargs, _ = children.calls[0]
    assert argv == [f"{python}/bin/python3", "-m", "trixd.plugins.osimage.runner", jobdir]
    assert kwargs["env"]["HOME"] == "/root" and kwargs["env"]["LANG"] == "C.UTF-8"
    assert kwargs["env"]["PATH"] == f"{python}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    state = jobs.read_state(jobdir)
    assert (state["state"], state["pid"], state["request_id"]) == ("running", child.pid, REQUEST_ID)
    assert state["started"] == body["started"] and state["pushed"] == 0 and state["closing"] == ""
    assert jobs.read_json(jobdir, jobs.REQUEST) == {
        "name": "rocky9", "request_id": REQUEST_ID, "path": str(tmp_path / "images" / "rocky9"),
        "distribution": "redhat", "osrelease": "9.6", "architecture": "x86_64", "media": None,
        "mirrors_from": None,
        "settings": {"repo": str(tmp_path / "repo"), "repo_url": "",
                     "answers": backend.DEFAULT_SETTINGS["answers"],
                     "images_root": str(tmp_path / "images"), "python": python,
                     "collections": str(tmp_path / "collections"),
                     "builder": "books", "stub_gap_seconds": 3}}
    assert jobs.read_json(jobdir, jobs.VERDICT) == {"arch": "x86_64"}
    listed = client.get(f"{API_PREFIX}/osimage", headers=headers).json()
    assert listed["running"] == 1 and listed["jobs"][0]["state"] == "running"


def test_the_build_runs_in_a_session_of_its_own_with_its_output_in_the_job_log(plugin, tmp_path,
                                                                               children):
    """
    The build is a child with no input, in a session and group of its own so a signal to
    the group reaches the book and nothing else, and its output goes straight to the
    job's build.log, opened for append, so what it prints before the runner has opened
    its own files is still in the job's log.
    """
    client, headers, commands = plugin
    assert _create(client, headers, tmp_path).status_code == 200
    assert len(children.calls) == 1, "no build process was started"
    argv, kwargs, opened = children.calls[0]
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("stdin") == subprocess.DEVNULL
    assert kwargs.get("stderr") == subprocess.STDOUT
    log = os.path.join(jobs.path(str(tmp_path / "state"), "rocky9"), jobs.LOG)
    assert opened == {"path": os.path.realpath(log), "append": True, "mode": 0o600}


def test_the_create_answers_without_waiting_for_the_build(plugin, tmp_path, children):
    """
    The accept hands the build over and answers: nothing waits for the child, which would
    hold the five seconds Luna allows for the length of a build.
    """
    client, headers, commands = plugin
    children.wait_raises = True
    answer = _create(client, headers, tmp_path)
    assert answer.status_code == 200, answer.text
    assert len(children.made) == 1, "no build process was started"
    assert children.made[0].waited == [] and children.signals == []


def test_a_create_that_names_sources_carries_them_to_the_job(plugin, tmp_path):
    """
    The seventh key is for a caller that has sources; it travels as sent, over any
    mirrors entry, and a source the rules refuse is refused before anything is written.
    """
    client, headers, commands = plugin
    root = str(tmp_path / "mirror")
    held = client.patch(f"{API_PREFIX}/osimage/settings",
                        json={"mirrors": {"redhat/9.6": ["https://mirror.invalid/rocky/9/"]}},
                        headers=headers)
    assert held.status_code == 200, held.text
    assert _create(client, headers, tmp_path, media=[root]).status_code == 200
    request = jobs.read_json(jobs.path(str(tmp_path / "state"), "rocky9"), jobs.REQUEST)
    assert request["media"] == [root]
    refused = _create(client, headers, tmp_path, "rocky8", media=["ftp://mirror/rocky"])
    assert refused.status_code == 400 and "media item 1 is not a URL" in refused.text
    assert jobs.names(str(tmp_path / "state")) == ["rocky9"]


def test_the_sources_of_a_create_without_media_come_from_the_mirrors_map_by_record(plugin, tmp_path):
    """
    In order: the body's media, else the map's entry for the record and its architecture
    in any spelling, else the entry for the distribution and release alone, else none;
    the request written for the unit carries what was resolved.
    """
    client, headers, commands = plugin
    mirrors = {"redhat/9.6/x86": ["https://mirror.invalid/rocky/9/x86/"],
               "redhat/9.6": ["https://mirror.invalid/rocky/9/"],
               "ubuntu/24.04": ["https://mirror.invalid/ubuntu/"]}
    held = client.patch(f"{API_PREFIX}/osimage/settings", json={"mirrors": mirrors, "concurrency": 9},
                        headers=headers)
    assert held.status_code == 200, held.text
    assert held.json().get("mirrors") == mirrors, held.text
    cases = [("a-body", {"media": ["/isos/rocky.iso"]}, ["/isos/rocky.iso"]),
             ("b-arch", {}, ["https://mirror.invalid/rocky/9/x86/"]),
             ("c-plain", {"architecture": "arm64"}, ["https://mirror.invalid/rocky/9/"]),
             ("d-ubuntu", {"distribution": "ubuntu", "osrelease": "24.04"}, ["https://mirror.invalid/ubuntu/"]),
             ("e-none", {"osrelease": "9.5"}, None)]
    rungs = {"a-body": "create", "b-arch": "redhat/9.6/x86", "c-plain": "redhat/9.6",
             "d-ubuntu": "ubuntu/24.04", "e-none": None}
    for name, body, media in cases:
        answer = _create(client, headers, tmp_path, name, **body)
        assert answer.status_code == 200, (name, answer.text)
        request = jobs.read_json(jobs.path(str(tmp_path / "state"), name), jobs.REQUEST)
        assert request["media"] == media, name
        assert request.get("mirrors_from", "not recorded") == rungs[name], name
    assert "mirrors" not in jobs.read_json(jobs.path(str(tmp_path / "state"), "e-none"),
                                           jobs.REQUEST)["settings"]


@pytest.mark.parametrize("source", [
    "https://admin:secret@mirror.invalid/rocky/9/",
    "http://admin@mirror.invalid/rocky/9/",
    "file://admin:secret@/srv/mirror/",
])
def test_a_source_carrying_a_user_name_or_password_is_refused_and_not_repeated(plugin, tmp_path, source):
    """
    A credential in a URL would be kept in the job files and the logs, shown in the
    process listing and written into the image's repository files, so the URL is refused
    and its text is not echoed back.
    """
    client, headers, commands = plugin
    answer = _create(client, headers, tmp_path, media=["/mirror", source])
    assert answer.status_code == 400, answer.text
    assert "media item 2 must not carry a user name or password" in answer.text
    assert "secret" not in answer.text and "admin" not in answer.text
    assert jobs.names(str(tmp_path / "state")) == []


def test_an_at_sign_outside_the_authority_of_a_url_is_not_a_credential(plugin, tmp_path):
    """
    Only what stands before the host is a user name; an at sign in the path or the query
    is part of the address.
    """
    client, headers, commands = plugin
    assert client.patch(f"{API_PREFIX}/osimage/settings", json={"concurrency": 9}, headers=headers).status_code == 200
    for name, source in (("a-path", "https://mirror.invalid/users/a@b/"),
                         ("b-query", "https://mirror.invalid/rocky/9/?token=a@b"),
                         ("c-query", "https://mirror.invalid/rocky/9/?x=y")):
        answer = _create(client, headers, tmp_path, name, media=[source])
        assert answer.status_code == 200, (name, answer.text)


def test_a_source_over_2048_characters_is_refused_and_eight_of_2048_are_taken(plugin, tmp_path):
    """
    An item is held to a length a repository URL or a path never needs, and the count of
    eight is unchanged.
    """
    client, headers, commands = plugin
    eight = ["/" + str(number) + "a" * (2047 - len(str(number))) for number in range(8)]
    assert all(len(item) == 2048 for item in eight)
    assert _create(client, headers, tmp_path, "a-eight", media=eight).status_code == 200
    refused = _create(client, headers, tmp_path, "b-long", media=["/mirror", "/" + "a" * 2048])
    assert refused.status_code == 400, refused.text
    assert "media item 2 is longer than 2048 characters" in refused.text
    assert jobs.names(str(tmp_path / "state")) == ["a-eight"]


@pytest.mark.parametrize("source, name", [
    ("epel=https://mirror.invalid/epel/", "epel"),
    ("BaseOS=/srv/mirror/BaseOS", "BaseOS"),
])
def test_a_named_repository_is_refused_in_one_sentence_until_a_family_maps_it(plugin, tmp_path, source,
                                                                              name):
    """
    The name=URL form is reserved: it is refused with a sentence that says what to pass
    instead, and the URL after the name is not repeated.
    """
    client, headers, commands = plugin
    answer = _create(client, headers, tmp_path, media=[source])
    assert answer.status_code == 400, answer.text
    assert f"media item 1: named repositories ({name}=URL) are not supported yet, pass the mirror root" \
        in answer.text
    assert "mirror.invalid" not in answer.text and "/srv/mirror" not in answer.text
    assert jobs.names(str(tmp_path / "state")) == []


def test_the_mirrors_setting_is_held_to_the_same_rules_as_the_media_of_a_create(plugin):
    """
    The map is checked with the same function as a create's sources, so a credential or a
    named repository cannot be kept as the cluster's default either.
    """
    client, headers, commands = plugin
    for source, said in (("https://admin:secret@mirror.invalid/rocky/9/",
                          "mirrors entry redhat/9: media item 1 must not carry a user name or password"),
                         ("epel=https://mirror.invalid/epel/",
                          "mirrors entry redhat/9: media item 1: named repositories (epel=URL)")):
        answer = client.patch(f"{API_PREFIX}/osimage/settings", json={"mirrors": {"redhat/9": [source]}},
                              headers=headers)
        assert answer.status_code == 400 and said in answer.text, (source, answer.text)


def test_the_collections_setting_may_be_emptied_and_the_paths_may_not(plugin):
    """
    Empty is ansible's own search path, which is what the setting holds unless it is set;
    any other value of it is an absolute path, and the interpreter's setting is never empty.
    """
    client, headers, commands = plugin
    assert backend.DEFAULT_SETTINGS["collections"] == ""
    url = f"{API_PREFIX}/osimage/settings"
    emptied = client.patch(url, json={"collections": ""}, headers=headers)
    assert emptied.status_code == 200, emptied.text
    assert emptied.json().get("collections") == "", emptied.text
    for body, said in (({"collections": "collections"}, "collections must be an absolute path"),
                       ({"python": ""}, "python must be an absolute path")):
        refused = client.patch(url, json=body, headers=headers)
        assert refused.status_code == 400 and said in refused.text, (body, refused.text)


@pytest.mark.parametrize("name, body, status, said", [
    ("settings", {}, 400, "settings is reserved"),
    ("create", {}, 400, "create is reserved"),
    ("Rocky9", {}, 400, "image must match"),
    ("rocky9", {"architecture": "openpower"}, 400, "no luna2-client is published for ppc64le"),
    ("rocky9", {"architecture": "mips64el"}, 400, "architecture mips64el is not one of"),
    ("rocky9", {"osrelease": None}, 422, "osrelease"),
    ("rocky9", {"osrelease": ""}, 422, "osrelease"),
    ("rocky9", {"request_id": "not an id"}, 422, "request_id"),
])
def test_a_create_the_rules_refuse_is_a_400_and_a_missing_field_a_422(plugin, tmp_path, name,
                                                                      body, status, said):
    """
    A field that is missing or mistyped is FastAPI's 422 naming it; an image name no job
    can be made from, or an arch the books do not build, is a 400 naming the rule, before
    anything is written or run.
    """
    client, headers, commands = plugin
    answer = _create(client, headers, tmp_path, name, **body)
    assert answer.status_code == status, answer.text
    assert said in answer.text
    assert commands.ran == [] and jobs.names(str(tmp_path / "state")) == []


@pytest.mark.parametrize("where, said", [
    ("/etc/rocky9", "path must be an absolute path under images_root ({root})"),
    ("rocky9", "path must be an absolute path under images_root ({root})"),
    ("{root}", "path must be an absolute path under images_root ({root})"),
    ("{root}/../rocky9", "path must be an absolute path under images_root ({root})"),
    ("{root}/away/rocky9", "path must be an absolute path under images_root ({root})"),
    ("{root}/rocky9-old", "path must be the directory the books build for rocky9, {root}/rocky9"),
    ("{root}/sub/rocky9", "path must be the directory the books build for rocky9, {root}/rocky9"),
])
def test_a_create_whose_path_is_not_the_tree_the_books_build_is_400(plugin, tmp_path, where, said):
    """
    The unit mounts, chroots and removes inside the tree, so the path has to be absolute
    and below images_root once every symlink is followed; and the books build
    images_root/<image> while the result is read and packed from the path, so the two
    have to be one directory. Each refusal names what was wanted; a path that reaches
    that directory through a symlink is taken, in Luna's spelling.
    """
    client, headers, commands = plugin
    root = tmp_path / "images"
    root.mkdir()
    (root / "away").symlink_to(tmp_path / "elsewhere")
    answer = _create(client, headers, tmp_path, path=where.format(root=root))
    assert answer.status_code == 400, answer.text
    assert said.format(root=root) in answer.text
    assert commands.ran == [] and jobs.names(str(tmp_path / "state")) == []
    (root / "rocky9").mkdir()
    (root / "same").symlink_to(root / "rocky9")
    linked = _create(client, headers, tmp_path, path=f"{root}/same")
    assert linked.status_code == 200, linked.text
    assert jobs.read_json(jobs.path(str(tmp_path / "state"), "rocky9"), jobs.REQUEST)["path"] \
        == f"{root}/same"


def test_a_create_of_a_build_running_or_still_telling_luna_is_409(plugin, tmp_path):
    """
    A second create of the name while the build runs, or while its stream is still being
    delivered, is refused saying which; once the stream is closed the name is free again.
    Past the concurrency limit the create is refused before any job is written.
    """
    client, headers, commands = plugin
    assert _create(client, headers, tmp_path).status_code == 200
    again = _create(client, headers, tmp_path, request_id="1761818733.2")
    assert again.status_code == 409 and "a build named rocky9 is running" in again.text
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")
    jobs.move(jobdir, "failed", "interrupted")
    again = _create(client, headers, tmp_path, request_id="1761818733.2")
    assert again.status_code == 409 and "its stream is still being delivered" in again.text
    assert jobs.read_state(jobdir)["request_id"] == REQUEST_ID
    jobs.update(jobdir, closing="closed")
    assert _create(client, headers, tmp_path, request_id="1761818733.2").status_code == 200
    assert jobs.read_state(jobdir)["request_id"] == "1761818733.2"

    client.patch(f"{API_PREFIX}/osimage/settings", json={"concurrency": 1}, headers=headers)
    capped = _create(client, headers, tmp_path, "rocky8")
    assert capped.status_code == 409 and "1 builds running, limit 1" in capped.text
    assert jobs.names(str(tmp_path / "state")) == ["rocky9"]


def test_a_post_on_create_reaches_the_create_route_and_not_a_name_route(plugin, tmp_path):
    """
    Through the loaded table: a body without Luna's fields posted to `create` is answered
    by the create route, which wants them, and not by any route matched by name.
    """
    client, headers, commands = plugin
    answer = client.post(f"{API_PREFIX}/osimage/create", json={"media": ["/mirror"]}, headers=headers)
    assert answer.status_code == 422, answer.text
    assert "image" in answer.text and "request_id" in answer.text
    assert jobs.names(str(tmp_path / "state")) == []


def test_the_routes_of_a_prepare_and_a_start_are_gone(plugin, tmp_path):
    """
    A POST on a name, and a start under it, answer what any route this daemon does not
    have answers: the one as a verb the name route does not take, the other as a path
    nobody declared. The name is one a build is held under, so a start route that was
    there would answer for it rather than 404 for a name nobody created.
    """
    client, headers, commands = plugin
    assert _create(client, headers, tmp_path).status_code == 200
    ran = len(commands.ran)
    body = {"media": ["/mirror"], "arch": "x86_64", "request_id": REQUEST_ID}
    posted = client.post(f"{API_PREFIX}/osimage/rocky9", json=body, headers=headers)
    assert posted.status_code == client.post(f"{API_PREFIX}/osimage", json=body,
                                             headers=headers).status_code == 405, posted.text
    started = client.post(f"{API_PREFIX}/osimage/rocky9/actions/start", json=body, headers=headers)
    assert started.status_code == client.post(f"{API_PREFIX}/osimage/rocky9/actions/never", json=body,
                                              headers=headers).status_code == 404, started.text
    assert jobs.read_state(jobs.path(str(tmp_path / "state"), "rocky9"))["state"] == "running"
    assert len(commands.ran) == ran


def test_a_refusal_the_build_finds_ends_the_stream_and_the_accept_detected_nothing(
        plugin, tmp_path, monkeypatch, sigterm_restored, children):
    """
    The whole path of a source detection refuses: the accept writes the job and starts
    the build without reading the source, the runner reads it and ends with the refusal as
    its reason, the supervisor records the build as failed, and Luna is told the runner's
    lines and then, once, a line saying the build failed and why, then EOF.
    """
    client, headers, commands = plugin
    read = []
    identify_all = detect.identify_all
    monkeypatch.setattr(detect, "identify_all",
                        lambda *args, **kwargs: read.append(args[0]) or identify_all(*args, **kwargs))
    bare = tmp_path / "bare"
    bare.mkdir()
    assert _create(client, headers, tmp_path, media=[str(bare)]).status_code == 200
    assert read == [], "the accept read the source"
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")

    def refused(*args, **kwargs):
        raise AssertionError("a refused build started a process")
    monkeypatch.setattr(runner.subprocess, "Popen", refused)
    monkeypatch.setattr(runner.platform, "machine", lambda: "x86_64")
    assert runner.Build(jobdir).run() == 1
    assert read == [[str(bare)]]
    children.made[0].returncode = 1
    supervisor.current().once()
    assert jobs.read_state(jobdir)["state"] == "failed"

    posts = []

    class Luna:
        def post(self, uri, payload=None):
            posts.append((uri, payload["monitor"]["status"]["request_id"],
                          [item["message"] for item in payload["monitor"]["status"]["messages"]]))
            return {"message": "success"}
    monkeypatch.setattr(push, "luna", Luna)
    for _ in range(3):
        supervisor.current().relay()
    reason = (f"{bare}: no self-describing metadata found (.treeinfo, .disk/info, dists/*/Release, "
              f"media.1/products, os-release)")
    assert posts == [(push.STATUS_ROUTE, REQUEST_ID, [reason]),
                     (push.STATUS_ROUTE, REQUEST_ID, [f"the build of rocky9 failed: {reason}", push.END])]
    assert jobs.read_state(jobdir)["closing"] == push.CLOSED


def test_two_creates_arriving_together_cannot_both_pass_the_cap(plugin, tmp_path, children):
    """
    Against a cap of one exactly one build is started: the count and the start are one
    step. The first start waits for a second one, which under the lock never arrives, so
    it times out and goes on alone; without the lock both count zero.
    """
    client, headers, commands = plugin
    client.patch(f"{API_PREFIX}/osimage/settings", json={"concurrency": 1}, headers=headers)
    gate = threading.Barrier(2)
    meet = threading.Barrier(2, timeout=0.5)

    def starting():
        try:
            meet.wait()
        except threading.BrokenBarrierError:
            pass
    children.before = starting
    answers = []

    def create(name):
        gate.wait()
        answers.append(_create(client, headers, tmp_path, name).status_code)
    threads = [threading.Thread(target=create, args=(name,)) for name in ("rocky9", "rocky8")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(answers) == [200, 409]
    assert len(children.made) == 1


def test_a_create_whose_build_cannot_be_started_is_503_and_the_next_create_replaces_the_job(
        plugin, tmp_path, children):
    """
    A start the system refuses is 503, and the job it leaves, written and never run,
    counts for nothing and gives way to the next create of the name.
    """
    client, headers, commands = plugin
    children.refuse = OSError(12, "Cannot allocate memory")
    refused = _create(client, headers, tmp_path)
    assert refused.status_code == 503, refused.text
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")
    assert jobs.read_state(jobdir)["state"] == "prepared"
    assert "rocky9" not in jobs.CHILDREN
    listed = client.get(f"{API_PREFIX}/osimage", headers=headers).json()
    assert listed["running"] == 0 and listed["jobs"][0]["state"] == "prepared"

    children.refuse = None
    assert _create(client, headers, tmp_path, request_id="1761818733.2").status_code == 200
    state = jobs.read_state(jobdir)
    assert state["state"] == "running" and state["request_id"] == "1761818733.2"
    assert jobs.CHILDREN["rocky9"] is children.made[0]


def test_a_create_whose_job_cannot_be_recorded_running_stops_the_build_it_started(
        plugin, tmp_path, monkeypatch, children):
    """
    The build is started before the job is recorded as running. When that record fails
    the build is stopped through its group and forgotten, so none runs that no pass
    would watch.
    """
    client, headers, commands = plugin
    moved = jobs.move

    def refusing(jobdir, state, *args, **fields):
        if state == "running":
            raise OSError(28, "No space left on device")
        return moved(jobdir, state, *args, **fields)
    monkeypatch.setattr(jobs, "move", refusing)
    try:
        answer = _create(client, headers, tmp_path)
    except OSError:
        answer = None
    assert len(children.made) == 1, "no build process was started"
    child = children.made[0]
    assert children.signals == [(child.pid, signal.SIGTERM)], "the build it started was left running"
    assert "rocky9" not in jobs.CHILDREN
    assert answer is None or answer.status_code != 200


def test_a_read_from_a_sequence_number_serves_the_lines_from_there(plugin, tmp_path, children):
    """
    `since` serves the lines from there with the next number to ask for; once the runner
    recorded it, the kernel version rides along.
    """
    client, headers, commands = plugin
    assert _create(client, headers, tmp_path).status_code == 200
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")
    with open(os.path.join(jobdir, jobs.PROGRESS), "w", encoding="utf-8") as handle:
        for seq, message in enumerate(["PLAY [localhost]", "TASK [one]", "TASK [two]"]):
            handle.write(json.dumps({"seq": seq, "message": message, "status": 200}) + "\n")
    jobs.write_phase(jobdir, "localhost")

    shown = client.get(f"{API_PREFIX}/osimage/rocky9", params={"since": 1}, headers=headers)
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert (body["name"], body["state"], body["phase"], body["pid"]) == (
        "rocky9", "running", "localhost", children.made[0].pid)
    assert [line["message"] for line in body["lines"]] == ["TASK [one]", "TASK [two]"]
    assert body["next"] == 3 and "notes" not in body
    assert "kernelversion" not in body
    assert client.get(f"{API_PREFIX}/osimage/rocky9", params={"since": "soon"},
                      headers=headers).status_code == 422

    jobs.files.write(os.path.join(jobdir, jobs.RESULT),
                     json.dumps({"ok": True, "exit": 0, "kernelversion": "5.14.0-503.el9"}))
    body = client.get(f"{API_PREFIX}/osimage/rocky9", headers=headers).json()
    assert body["kernelversion"] == "5.14.0-503.el9" and body["next"] == 3
    assert client.get(f"{API_PREFIX}/osimage/nothing", headers=headers).status_code == 404


def test_a_cancel_signals_the_build_and_records_what_the_runner_left(plugin, tmp_path, children):
    """
    A running build: SIGTERM to its group, waited for, then cancelled with the exit the
    runner recorded; one that never started is cancelled with nothing to signal; one that
    ended is 409.
    """
    client, headers, commands = plugin
    assert _create(client, headers, tmp_path).status_code == 200
    child = children.made[0]
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")
    jobs.files.write(os.path.join(jobdir, jobs.RESULT),
                     json.dumps({"ok": False, "exit": 143, "reason": "cancelled"}))
    cancelled = client.post(f"{API_PREFIX}/osimage/rocky9/actions/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"name": "rocky9", "state": "cancelled"}
    assert children.signals == [(child.pid, signal.SIGTERM)] and len(child.waited) == 1
    state = jobs.read_state(jobdir)
    assert (state["state"], state["exit"], state["reason"]) == ("cancelled", 143, "cancelled by tester")
    again = client.post(f"{API_PREFIX}/osimage/rocky9/actions/cancel", headers=headers)
    assert again.status_code == 409 and "build rocky9 is already cancelled" in again.text

    children.refuse = OSError(12, "Cannot allocate memory")
    assert _create(client, headers, tmp_path, "rocky8").status_code == 503
    quiet = client.post(f"{API_PREFIX}/osimage/rocky8/actions/cancel", headers=headers)
    assert quiet.status_code == 200
    assert len(children.signals) == 1
    assert jobs.read_state(jobs.path(str(tmp_path / "state"), "rocky8"))["state"] == "cancelled"
    assert client.post(f"{API_PREFIX}/osimage/nothing/actions/cancel",
                       headers=headers).status_code == 404


def test_a_cancel_records_a_running_build_this_daemon_holds_no_process_for(plugin, tmp_path,
                                                                          children):
    """
    What a restart leaves, a job running with no process in the registry, is not finished
    by a pass; the cancel records it cancelled and answers, with nothing to signal.
    """
    client, headers, commands = plugin
    assert _create(client, headers, tmp_path).status_code == 200
    jobs.drop("rocky9")
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")
    assert jobs.read_state(jobdir)["state"] == "running"
    cancelled = client.post(f"{API_PREFIX}/osimage/rocky9/actions/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"name": "rocky9", "state": "cancelled"}
    assert children.signals == []
    assert jobs.read_state(jobdir)["state"] == "cancelled"


def test_a_cancel_kills_a_build_that_does_not_end_within_the_grace(plugin, tmp_path, children):
    """
    A build that outlives SIGTERM, the grace being over when its wait times out, gets
    SIGKILL to the same group and is waited for again; the cancel then records it.
    """
    client, headers, commands = plugin
    children.stubborn = True
    assert _create(client, headers, tmp_path).status_code == 200
    assert len(children.made) == 1, "no build process was started"
    child = children.made[0]
    cancelled = client.post(f"{API_PREFIX}/osimage/rocky9/actions/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"name": "rocky9", "state": "cancelled"}
    assert children.signals == [(child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)]
    assert len(child.waited) == 2 and child.waited[0] and child.waited[0] > 0
    assert jobs.read_state(jobs.path(str(tmp_path / "state"), "rocky9"))["state"] == "cancelled"


def test_the_kill_after_the_grace_reaches_the_book_in_its_own_session(tmp_path, monkeypatch):
    """
    Real processes: a runner that does not end on SIGTERM, stuck behind a book that
    ignores it, is killed together with the book. The book runs in a second session the
    kill of the runner's group does not reach, so without this a stuck build goes on
    writing into the tree while Luna removes it.
    """
    monkeypatch.setattr(jobs, "STOP_GRACE", 1)
    # Each says ready once it ignores SIGTERM, so the stop is not sent before that.
    stuck = [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                   "print('ready', flush=True); time.sleep(60)"]
    book = subprocess.Popen(stuck, start_new_session=True, stdout=subprocess.PIPE, text=True)
    try:
        assert book.stdout.readline().strip() == "ready"
        process = jobs.start("rocky9", str(tmp_path), stuck, dict(os.environ))
        for _ in range(100):
            if "ready" in open(tmp_path / jobs.LOG, encoding="utf-8").read():
                break
            time.sleep(0.1)
        else:
            pytest.fail("the runner did not start")
        jobs.write_book_group(str(tmp_path), book.pid)
        jobs.stop("rocky9", str(tmp_path))
        assert process.returncode == -signal.SIGKILL
        try:
            ended = book.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("the book outlived the kill of its runner")
        assert ended == -signal.SIGKILL
    finally:
        book.kill()
        book.wait()


def test_a_build_process_that_was_reaped_is_not_signalled(children):
    """
    Once the child is reaped its pid is free and may be another process's, so a signal
    to its group is not sent.
    """
    child = children.spawn(code=0)
    jobs._signal_group(child, signal.SIGTERM)
    assert children.signals == []


def test_a_cancel_that_meets_the_supervisor_answers_what_is_recorded(plugin, tmp_path, children):
    """
    While the cancel waits for the build to end the supervisor sees it gone and records
    the build as ended: the cancel then answers that state instead of a conflict, and the
    signal was sent without the jobs lock held.
    """
    client, headers, commands = plugin
    assert _create(client, headers, tmp_path).status_code == 200
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")

    def met():
        assert jobs.LOCK.acquire(timeout=1), "the stop holds the jobs lock while it signals"
        jobs.LOCK.release()
        jobs.finish(jobdir, "failed", "interrupted")
    children.on_signal = met
    answer = client.post(f"{API_PREFIX}/osimage/rocky9/actions/cancel", headers=headers)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"name": "rocky9", "state": "failed"}
    assert jobs.read_state(jobdir)["reason"] == "interrupted"


def test_the_children_registry_survives_a_reload_of_the_plugin(rig, plugin, tmp_path, children):
    """
    The routes' backend is imported under the plugin's own name and the supervisor's under
    the canonical one, and a reload drops the first: the registry of the builds this daemon
    started has to live where both and the reload leave it, so after a reload the build is
    still held, is not taken for one a restart interrupted, and a cancel still reaches it.
    """
    client, headers, commands = plugin
    assert _create(client, headers, tmp_path).status_code == 200
    assert len(children.made) == 1, "no build process was started"
    child = children.made[0]
    jobdir = jobs.path(str(tmp_path / "state"), "rocky9")
    entry = loader.load(rig, "osimage")
    assert entry.state == "loaded", entry.reason
    assert getattr(jobs, "CHILDREN", {}).get("rocky9") is child
    supervisor.current().once()
    assert jobs.read_state(jobdir)["state"] == "running"
    cancelled = client.post(f"{API_PREFIX}/osimage/rocky9/actions/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert children.signals == [(child.pid, signal.SIGTERM)]


def test_the_settings_round_trip_and_refuse_what_the_daemon_could_not_run_with(plugin, tmp_path):
    """
    What is written is what comes back, over the defaults for what was not sent; a
    relative path, a count below one, a mirrors key that is not the record's fields or
    a mirrors source the create would refuse is refused and nothing changes.
    """
    client, headers, commands = plugin
    held = client.get(f"{API_PREFIX}/osimage/settings", headers=headers).json()
    assert held["repo"] == str(tmp_path / "repo") and held["concurrency"] == 2
    assert set(held) == set(backend.DEFAULT_SETTINGS) and held.get("mirrors") == {}
    changed = client.patch(f"{API_PREFIX}/osimage/settings",
                           json={"limit_seconds": 1800, "keep_days": 3}, headers=headers)
    assert changed.status_code == 200 and changed.json()["limit_seconds"] == 1800
    assert changed.json()["keep_days"] == 3 and changed.json()["concurrency"] == 2
    stubbed = client.patch(f"{API_PREFIX}/osimage/settings",
                           json={"builder": "stub-fail", "stub_gap_seconds": 1}, headers=headers)
    assert stubbed.status_code == 200 and stubbed.json().get("builder") == "stub-fail"
    assert stubbed.json().get("stub_gap_seconds") == 1
    mirrored = client.patch(f"{API_PREFIX}/osimage/settings",
                            json={"mirrors": {"redhat/9.6/arm64": ["/isos/Rocky-9.6-aarch64-dvd.iso"]}},
                            headers=headers)
    assert mirrored.status_code == 200, mirrored.text
    assert mirrored.json()["mirrors"] == {"redhat/9.6/arm64": ["/isos/Rocky-9.6-aarch64-dvd.iso"]}
    for body, said in (({"concurrency": 0}, "concurrency must be at least 1"),
                       ({"state_root": "var/osimage"}, "state_root must be an absolute path"),
                       ({"builder": "hand"}, "builder must be one of books, stub, stub-fail"),
                       ({"stub_gap_seconds": 0}, "stub_gap_seconds must be at least 1"),
                       ({"mirrors": {"Rocky 9": ["/isos/x.iso"]}},
                        "mirrors key 'Rocky 9' is not <distribution>/<osrelease> or "
                        "<distribution>/<osrelease>/<architecture>"),
                       ({"mirrors": {"redhat/9.6/mips64el": ["/isos/x.iso"]}},
                        "mirrors key 'redhat/9.6/mips64el' is not"),
                       ({"mirrors": {"redhat/9.6": []}},
                        "mirrors entry redhat/9.6: media must be a list of 1 to 8 sources"),
                       ({"mirrors": {"redhat/9.6": ["ftp://mirror/rocky"]}},
                        "mirrors entry redhat/9.6: media item 1 is not a URL")):
        refused = client.patch(f"{API_PREFIX}/osimage/settings", json=body, headers=headers)
        assert refused.status_code == 400 and said in refused.text, refused.text
    assert client.patch(f"{API_PREFIX}/osimage/settings", json={"mirrors": ["redhat/9.6"]},
                        headers=headers).status_code == 422
    held = client.get(f"{API_PREFIX}/osimage/settings", headers=headers).json()
    assert held["concurrency"] == 2 and held["builder"] == "stub-fail"
    assert held["mirrors"] == {"redhat/9.6/arm64": ["/isos/Rocky-9.6-aarch64-dvd.iso"]}
    cleared = client.patch(f"{API_PREFIX}/osimage/settings", json={"mirrors": {}}, headers=headers)
    assert cleared.status_code == 200 and cleared.json()["mirrors"] == {}


def test_no_default_setting_names_an_address_to_fetch_code_from():
    """
    The books are fetched and run as root: where from is the installer's or the
    operator's to name, pinned, so no default of the plugin is a URL, and a build
    with no checkout and no address is refused saying how to configure one.
    """
    for key, value in backend.DEFAULT_SETTINGS.items():
        assert not str(value).startswith(("http://", "https://")), key
    assert backend.DEFAULT_SETTINGS["repo_url"] == ""


def test_a_daemon_that_never_built_lists_no_jobs(plugin, tmp_path):
    """
    A state root that is not there reads as no jobs, with the limit still answered,
    and a name asked of it is 404 rather than an error about the directory.
    """
    client, headers, commands = plugin
    assert not os.path.exists(tmp_path / "state")
    assert client.get(f"{API_PREFIX}/osimage", headers=headers).json() == {
        "jobs": [], "running": 0, "limit": 2}
    assert client.get(f"{API_PREFIX}/osimage/rocky9", headers=headers).status_code == 404


@pytest.mark.parametrize("verb, path", [("GET", "<name>"), ("POST", "<name>/actions/cancel")])
def test_every_name_route_answers_a_published_code_for_a_name_nobody_created(plugin, verb, path):
    """
    What the access suite does with a dummy name and an empty body: each route answers
    one of the codes its manifest publishes, for a name that was never created.
    """
    client, headers, commands = plugin
    declared = next(item for item in _manifest()["endpoints"]
                    if item["path"] == path and verb in item["methods"])
    answer = client.request(verb, f"{API_PREFIX}/osimage/{path.replace('<name>', 'e2e-x')}",
                            json={} if verb == "POST" else None, headers=headers)
    assert answer.status_code in declared["errors"], (answer.status_code, answer.text)
    assert answer.status_code == 404


def test_unplugging_the_plugin_takes_the_supervisor_with_it(rig, plugin):
    """
    The trigger is owned, so the plugin going takes it out of health.
    """
    loader.unload(rig, "osimage")
    assert "osimage_jobs" not in triggers.states()
    client, headers, _ = plugin
    assert client.get(f"{API_PREFIX}/osimage", headers=headers).status_code == 404
