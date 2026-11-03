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
The slurm plugin's change routes: only a person asks, Luna decides as that person through the
luna plugin, the body reaches the helper stepped down to its account, the helper's refusals come
back as trixd's classes, and the render shares the helper's lock without ever waiting for it.
"""

__author__      = "Alex Ninaber"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Alex Ninaber"
__status__      = "Development"

import fcntl
import json
import logging
import multiprocessing
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import trixd

from tests.unit.test_plugins import Ran
from trixd.common.errors import (BackendFailure, BadRequest, Conflict, Forbidden,
                                 ServiceUnavailable, TrixdError, Unauthenticated)

TOKEN = "trixd-token-of-admin"
PEOPLE = {TOKEN: "admin", "trixd-token-of-anna": "anna"}
VERBS = ("drain", "resume", "preview", "save", "restore", "apply")


def _request(token=TOKEN):
    """
    A stand-in HTTP request carrying a trixd token, or no header when the token is None.
    """
    return SimpleNamespace(headers={"x-access-tokens": token} if token is not None else {})


def _outcome(call, *args, **kwargs):
    """
    What the call answered, or the trixd error it raised.
    """
    try:
        return call(*args, **kwargs)
    except TrixdError as error:
        return error


class _AsPerson:
    """
    The plugin, each change asked with the person's request unless the test passes its own.
    """

    def __init__(self, plugin, request):
        """Takes the plugin and a function giving the request each change route is called with."""
        self._plugin, self._request = plugin, request

    def __getattr__(self, name):
        """
        The plugin's attribute; a change route gets the request added, unless the call names its
        own.
        """
        found = getattr(self._plugin, name)
        if name not in VERBS:
            return found
        return lambda *args, **kwargs: found(*args, **{"request": self._request(), **kwargs})


@pytest.fixture()
def slurm(rig, monkeypatch, tmp_path):
    """
    The plugin over files in tmp_path, the helper's runs recorded and answered from `said`, the
    person behind a trixd token from PEOPLE and Luna's answer to persons.may from `luna`.
    """
    from trixd.plugins.luna import persons
    from trixd.plugins.slurm import backend
    from trixd.utils import declarations

    folder = tmp_path / "slurm"
    folder.mkdir()
    declarations.write("slurm", "config", "", {
        **backend.DEFAULT_CONFIG,
        "nodes_file": str(folder / "slurm-nodes.conf"),
        "partitions_file": str(folder / "slurm-partitions.conf"),
        "gres_file": str(folder / "gres.conf")})
    ran, said = [], {"out": json.dumps({"ok": True, "code": 200, "names": ["node001"]})}

    def run(argv, timeout=None, check=False, secret_input=""):
        """
        The fake shell.run: records argv, timeout and the request read from secret_input, then
        answers what the test set.
        """
        ran.append({"argv": argv, "timeout": timeout, "request": json.loads(secret_input)})
        if isinstance(said.get("raise"), BaseException):
            raise said["raise"]
        return Ran(said["out"], said.get("code", 0), said.get("err", ""))

    monkeypatch.setattr(backend.shell, "run", run)
    monkeypatch.setattr(backend, "pwd", SimpleNamespace(getpwnam=lambda name: name))
    groups = {"admins": 4242}

    def getgrnam(name):
        """
        The fake grp.getgrnam over the test's groups; an unknown group raises KeyError as grp
        does.
        """
        if name not in groups:
            raise KeyError(name)
        return SimpleNamespace(gr_gid=groups[name])

    monkeypatch.setattr(backend, "grp", SimpleNamespace(getgrnam=getgrnam))
    monkeypatch.setattr(backend, "SETPRIV", sys.executable)
    monkeypatch.setattr(backend, "PRLIMIT", sys.executable)
    luna = {"refused": {}, "raise": None, "asked": []}

    def caller(headers):
        """persons.caller: the person a trixd token in PEOPLE names, None for any other."""
        name = PEOPLE.get(headers.get("x-access-tokens"))
        return persons.Person(name, None) if name else None

    def may(person, table, names, bit):
        """persons.may: records what was asked, and refuses what the test set for the table."""
        luna["asked"].append((person.name, table, list(names), bit))
        if luna["raise"] is not None:
            raise luna["raise"]
        return {name: why for name, why in luna["refused"].get(table, {}).items()
                if name in names}

    # The module the backend holds: a test that drops the luna plugin's modules leaves two.
    held = getattr(backend, "persons", persons)
    monkeypatch.setattr(held, "caller", caller)
    monkeypatch.setattr(held, "may", may)
    audited = []
    monkeypatch.setattr(backend.audit, "record",
                        lambda principal, action, object_name="", outcome="granted", detail="",
                        handle=None: audited.append({"principal": principal, "action": action,
                                                     "object": object_name,
                                                     "outcome": outcome, "detail": detail}))
    plugin = backend.Backend()
    return SimpleNamespace(backend=backend, plugin=_AsPerson(plugin, _request), bare=plugin,
                           ran=ran, said=said, folder=folder, luna=luna, audited=audited)


def _drain(**extra) -> dict:
    """The body of a drain of node001, with any field replaced."""
    return {"names": ["node001"], "reason": "fan", **extra}


def _write(slurm, **extra) -> dict:
    """The body of a save or a restore carrying this plugin's rules digest."""
    return {"texts": {}, "stamp": {}, "judged": [], "rules": slurm.backend.rules_digest(),
            **extra}


def _calls(slurm, caller=None):
    """Every change route as a call taking the request to send, from the caller trixd's gate
    let through (None: one that is not rootus)."""
    return {
        "drain": lambda request: slurm.bare.drain(body=_drain(), caller=caller, request=request),
        "resume": lambda request: slurm.bare.resume(body={"names": ["node001"]}, caller=caller,
                                                    request=request),
        "preview": lambda request: slurm.bare.preview(body={"mode": "save"}, caller=caller,
                                                      request=request),
        "save": lambda request: slurm.bare.save(body=_write(slurm, judged=["node001"]),
                                                caller=caller, request=request),
        "restore": lambda request: slurm.bare.restore(body=_write(slurm), caller=caller,
                                                      request=request),
        "apply": lambda request: slurm.bare.apply(caller=caller, request=request),
    }


@pytest.mark.parametrize("request_", [None, _request(None), _request("trixd-token-of-rootus")],
                         ids=["no-request", "no-token", "not-a-person"])
def test_only_a_person_asks_for_a_change(slurm, request_):
    """
    Every change route refuses with Forbidden a request no person stands behind (a service, a
    node, a token Luna minted: persons.caller answers None for each), asking Luna nothing and
    running nothing; a person's request reaches the helper.
    """
    for verb, call in _calls(slurm).items():
        refused = _outcome(call, request_)
        assert isinstance(refused, Forbidden), (verb, refused)
        assert "made as a person, with their own trixd token" in str(refused), verb
    assert slurm.luna["asked"] == [] and slurm.ran == []
    for verb, call in _calls(slurm).items():
        assert not isinstance(_outcome(call, _request()), TrixdError), verb
    assert [run["argv"][-1] for run in slurm.ran] == list(VERBS)


def test_rootus_passes_as_it_passes_the_route_to_luna(slurm):
    """
    Rootus is no person Luna can be asked about, and passes every route here as it passes the
    route to Luna: each change runs under its name, Luna asked nothing.
    """
    rootus = SimpleNamespace(name="tester", kind="user", rootus=True)
    for verb, call in _calls(slurm, caller=rootus).items():
        assert not isinstance(_outcome(call, _request(None)), TrixdError), verb
    assert slurm.luna["asked"] == []
    assert slurm.ran[0]["request"].get("person") == "tester"
    assert {a["principal"] for a in slurm.audited} == {"tester"}


def test_luna_decides_as_the_person_before_the_helper_runs(slurm):
    """
    Drain and resume ask Luna for x on every node named; save asks w on the cluster and the
    hardware right (h) on each node whose State it changes; restore (judging none) and apply
    ask w on the cluster; preview asks nothing. A drain hands the helper the person's own name.
    """
    asked = {}
    for verb, call in _calls(slurm).items():
        slurm.luna["asked"].clear()
        assert not isinstance(_outcome(call, _request("trixd-token-of-anna")), TrixdError), verb
        asked[verb] = list(slurm.luna["asked"])
    node, cluster = ("anna", "node", ["node001"], "x"), ("anna", "cluster", ["cluster"], "w")
    hardware = ("anna", "node", ["node001"], "h")
    assert asked == {"drain": [node], "resume": [node], "preview": [],
                     "save": [cluster, hardware], "restore": [cluster], "apply": [cluster]}
    assert slurm.ran[0]["request"].get("person") == "anna"


def test_luna_s_refusal_reaches_the_person_in_its_words_and_nothing_runs(slurm):
    """
    One node refused refuses the whole drain in Luna's words; the cluster refused refuses save,
    restore and apply without asking about nodes; a node refused refuses only the save that
    changes its State.
    """
    not_x = "operating node node001 is not permitted: you may read it"
    not_w = "changing or removing cluster cluster is not permitted: you may read it"
    slurm.luna["refused"] = {"node": {"node001": not_x}}
    for verb in ("drain", "resume"):
        refused = _outcome(_calls(slurm)[verb], _request())
        assert isinstance(refused, Forbidden) and str(refused) == not_x, (verb, refused)
    refused = _outcome(_calls(slurm)["save"], _request())
    assert isinstance(refused, Forbidden) and str(refused) == not_x, refused
    assert slurm.ran == []
    for verb in ("restore", "apply"):
        assert not isinstance(_outcome(_calls(slurm)[verb], _request()), TrixdError), verb
    slurm.luna["refused"] = {"cluster": {"cluster": not_w}}
    for verb in ("save", "restore", "apply"):
        slurm.luna["asked"].clear()
        refused = _outcome(_calls(slurm)[verb], _request())
        assert isinstance(refused, Forbidden) and str(refused) == not_w, (verb, refused)
        assert [table for _who, table, _names, _bit in slurm.luna["asked"]] == ["cluster"], verb
    assert [run["argv"][-1] for run in slurm.ran] == ["restore", "apply"]


@pytest.mark.parametrize("raised", [ServiceUnavailable("Luna is not answering: refused"),
                                    Unauthenticated("sign in at Luna"), TrixdError("no _access")],
                         ids=["silent", "sign-in", "access-refused"])
def test_luna_not_answering_or_not_saying_is_raised_as_it_came_and_nothing_runs(slurm, raised):
    """
    Whatever persons.may raises (Luna away, the person to sign in again, Luna refusing to say
    what they hold, with its own status) reaches the caller as it came, and no helper runs.
    """
    slurm.luna["raise"] = raised
    for verb in ("drain", "resume", "save", "restore", "apply"):
        assert _outcome(_calls(slurm)[verb], _request()) is raised, verb
    assert slurm.ran == [] and len(slurm.luna["asked"]) == 5


def test_a_refusal_names_the_first_twenty_nodes_and_counts_the_rest(slurm):
    """
    Fifty nodes refused: Luna's sentence for the first twenty in name order, then how many more.
    """
    many = ["n%02d" % i for i in range(50)]
    slurm.luna["refused"] = {"node": {n: f"node {n} is not available" for n in many}}
    refused = _outcome(slurm.plugin.drain, body=_drain(names=many))
    assert isinstance(refused, Forbidden), refused
    assert str(refused).startswith("node n00 is not available; node n01 is not available")
    assert str(refused).endswith("node n19 is not available; and 30 more")


def test_the_helper_runs_stepped_down_with_the_request_on_stdin(slurm):
    """
    The helper runs through setpriv as the helper's account with cleared groups, no new
    privileges, a reset environment and an address-space limit, with the request on standard
    input and the verb's timeout.
    """
    said = _outcome(slurm.plugin.drain, body=_drain())
    assert not isinstance(said, TrixdError), said
    run = slurm.ran[-1]
    argv = run["argv"]
    assert argv[:9] == [sys.executable, "--reuid", "trinityx-slurm", "--regid", "trinityx-slurm",
                        "--clear-groups", "--no-new-privs", "--reset-env", sys.executable]
    assert argv[9:] == [f"--as={512 * 1024 * 1024}", "--", sys.executable, "-I", "-B", "-X",
                        "utf8", slurm.backend.HELPER, "drain"]
    assert run["timeout"] == slurm.backend.TIMEOUTS["drain"]
    assert run["request"] == {
        "files": {"nodes": str(slurm.folder / "slurm-nodes.conf"),
                  "partitions": str(slurm.folder / "slurm-partitions.conf"),
                  "gres": str(slurm.folder / "gres.conf")},
        "share_dir": str(slurm.folder / ".trinityx-slurm"),
        "state_dir": "/var/lib/trixd/slurm",
        "group": 4242,
        "person": "admin",
        **_drain()}


def test_the_helper_answers_over_the_argv_and_stdin_the_plugin_gives_it(slurm, monkeypatch):
    """
    Neither end faked: the argv after setpriv and prlimit runs helper.py as a process, the
    request on stdin, and its JSON answer comes back as the plugin's refusal. The files sit in a
    folder the running account owns, which the helper refuses whatever that account is.
    """
    def real(argv, timeout=None, check=False, secret_input=""):
        """shell.run for the helper's own argv: the python after `--`, run for real."""
        done = subprocess.run(argv[argv.index("--") + 1:], input=secret_input, text=True,
                              capture_output=True, timeout=timeout, check=False)
        return Ran(done.stdout, done.returncode, done.stderr)

    monkeypatch.setattr(slurm.backend.shell, "run", real)
    refused = _outcome(slurm.bare.files)
    assert isinstance(refused, ServiceUnavailable), refused
    assert f"the helper's account may change {os.path.realpath(slurm.folder)}:" in str(refused)


def test_no_body_key_overrides_the_plugins_own_values(slurm):
    """The schema drops unknown keys today; the plugin's own values going last is what holds if a
    schema ever names one of them."""
    planted = {"files": {"nodes": "/etc/shadow"}, "share_dir": "/tmp", "state_dir": "/tmp",
               "group": 0}
    slurm.backend.helper("drain", slurm.bare.config(), {**_drain(), **planted})
    sent = slurm.ran[-1]["request"]
    assert sent["files"]["nodes"] == str(slurm.folder / "slurm-nodes.conf")
    assert (sent["share_dir"], sent["state_dir"], sent["group"]) == (
        str(slurm.folder / ".trinityx-slurm"), "/var/lib/trixd/slurm", 4242)


def test_only_a_save_and_a_restore_run_with_the_files_group(slurm, monkeypatch):
    """
    Only a save and a restore run the helper with the Slurm files' group; every other verb runs
    without it.
    """
    for verb, call in _calls(slurm).items():
        call(_request())
    slurm.plugin.files()
    given = {run["argv"][-1]: run["argv"][5:7] for run in slurm.ran}
    assert given == {"save": ["--groups", "4242"], "restore": ["--groups", "4242"],
                     "drain": ["--clear-groups", "--no-new-privs"],
                     "resume": ["--clear-groups", "--no-new-privs"],
                     "preview": ["--clear-groups", "--no-new-privs"],
                     "apply": ["--clear-groups", "--no-new-privs"],
                     "files": ["--clear-groups", "--no-new-privs"]}
    assert all(run["argv"][7 if run["argv"][5] == "--groups" else 6] == "--no-new-privs"
               for run in slurm.ran)
    from trixd.utils import declarations

    declarations.merge("slurm", "config", "", {"admin_group": "wheel"},
                       slurm.backend.DEFAULT_CONFIG)
    with pytest.raises(ServiceUnavailable) as refused:
        slurm.plugin.files()
    assert "there is no group wheel" in str(refused.value)
    assert len(slurm.ran) == 7


def test_each_verbs_helper_waits_longer_than_the_work_inside_it():
    """Read from the constants, so a change to any of them is judged: the helper's own waits
    inside the plugin's `shell.run`."""
    from tests.unit.test_slurm_ops import helper
    from trixd.plugins.slurm import backend

    inner = {"files": helper.GRANT_WAIT,
             "drain": helper.GRANT_WAIT + helper.WAIT + helper.QUICK,
             "resume": helper.GRANT_WAIT + helper.WAIT + helper.QUICK,
             "preview": helper.WAIT, "save": helper.WAIT + 35, "restore": helper.WAIT + 35,
             "apply": helper.WAIT + helper.SLOW}
    assert set(backend.TIMEOUTS) == set(inner)
    for verb, work in inner.items():
        assert backend.TIMEOUTS[verb] > work, verb


@pytest.mark.parametrize("code, raised", [(400, BadRequest), (403, Forbidden), (409, Conflict),
                                          (502, BackendFailure), (503, ServiceUnavailable),
                                          (418, BackendFailure)])
def test_the_helpers_refusal_is_raised_as_its_class_with_its_words(slurm, code, raised):
    """
    A helper refusal is raised as the trixd error of its HTTP status, carrying the helper's
    sentence.
    """
    slurm.said["out"] = json.dumps({"ok": False, "code": code, "message": "said so"})
    with pytest.raises(raised) as refused:
        slurm.plugin.apply()
    assert str(refused.value) == "said so"


def test_an_answer_carries_no_envelope(slurm):
    """A helper answer is returned without its ok and code fields."""
    slurm.said["out"] = json.dumps({"ok": True, "code": 200, "stamp": {"nodes": "x"}})
    assert slurm.plugin.files() == {"stamp": {"nodes": "x"}}


@pytest.mark.parametrize("ran", [
    {"out": "not json"}, {"out": "", "code": 1, "err": "Traceback"}, {"out": "[1]"},
    {"out": json.dumps({"ok": True}), "code": 1},
], ids=["not-json", "crashed", "not-an-object", "exit-code"])
def test_a_helper_that_does_not_answer_is_502(slurm, ran):
    """A helper that exits without a JSON answer is a BackendFailure pointing at the journal."""
    slurm.said.update(ran)
    with pytest.raises(BackendFailure) as refused:
        slurm.plugin.apply()
    assert str(refused.value) == "the Slurm helper did not answer apply; the reason is in the journal"


def test_a_helper_that_does_not_finish_leaves_the_outcome_unknown(slurm):
    """A write whose helper does not finish is a BackendFailure saying the outcome is unknown."""
    slurm.said["raise"] = BackendFailure("/usr/bin/setpriv did not finish within 45s")
    with pytest.raises(BackendFailure) as refused:
        slurm.plugin.save(body=_write(slurm))
    assert "the outcome is unknown, reload" in str(refused.value)


def test_without_the_account_or_the_tools_nothing_runs(slurm, monkeypatch):
    """
    Without the helper's account or the tools it runs through, the plugin answers
    ServiceUnavailable and runs nothing.
    """
    def gone(name):
        """A getpwnam for an account that does not exist."""
        raise KeyError(name)

    monkeypatch.setattr(slurm.backend, "pwd", SimpleNamespace(getpwnam=gone))
    with pytest.raises(ServiceUnavailable) as refused:
        slurm.plugin.files()
    assert "no account trinityx-slurm" in str(refused.value)
    monkeypatch.setattr(slurm.backend, "pwd", SimpleNamespace(getpwnam=lambda name: name))
    monkeypatch.setattr(slurm.backend, "PRLIMIT", "/nonexistent/prlimit")
    with pytest.raises(ServiceUnavailable):
        slurm.plugin.files()
    assert slurm.ran == []


def test_preview_and_writes_send_no_empty_optional(slurm):
    """A field the page sent as null is not passed on to the helper."""
    slurm.plugin.preview(body={"mode": "restore", "stamp": {}, "texts": None})
    assert "texts" not in slurm.ran[-1]["request"]


# -- the render beside the helper ---------------------------------------------------------------


def test_the_render_writes_nothing_over_a_cut_save(slurm):
    """
    The render refuses with BackendFailure while a cut save's mark is in the share, so it never
    writes over files a save left half written.
    """
    share = slurm.folder / ".trinityx-slurm"
    (share / "pending").mkdir(parents=True)
    (share / "pending" / "paths.json").write_text("[]")
    with pytest.raises(BackendFailure) as refused:
        slurm.plugin.render_config()
    assert "cut short" in str(refused.value)


def _hold(path: str, ready, done) -> None:
    """
    Holds the share's lock file in another process until told to let go, setting ready once it
    holds it.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    fcntl.lockf(fd, fcntl.LOCK_EX)
    ready.set()
    done.wait(10)


def _render_path() -> str:
    """The path of the render slurm's manifest declares: the engine's `again` takes any key, so a
    key that names no render is held to this one rather than taken in silence."""
    from trixd.core.plugins.manifest import parse, read_yaml

    path = os.path.join(os.path.dirname(trixd.__file__), "plugins", "slurm", "manifest.yaml")
    (endpoint,) = [e for e in parse(read_yaml(path)).endpoints if e.render is not None]
    return endpoint.path


def test_a_held_lock_turns_the_render_away_and_it_runs_again(slurm):
    """
    Every render runs under the engine's one lock, so the slurm render never waits for the
    helper's: held by another process, the render writes nothing, says so, and has the engine
    run it once more RETRY seconds later.
    """
    from trixd.core.render.engine import current as renders

    share = slurm.folder / ".trinityx-slurm"
    share.mkdir()
    ctx = multiprocessing.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(share / "lock"), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        with pytest.raises(BackendFailure) as refused:
            slurm.plugin.render_config()
        assert "a Slurm change holds the slurm files" in str(refused.value)
        assert set(renders().trailing) == {f"slurm/{_render_path()}"}
    finally:
        done.set()
        holder.join(10)


class _Said(logging.Handler):
    """
    The level and the message of every record a logger emits.
    """

    def __init__(self) -> None:
        """
        Nothing said yet.
        """
        super().__init__(logging.DEBUG)
        self.said = []

    def emit(self, record) -> None:
        """
        Keep the level and the message.
        """
        self.said.append((record.levelname, record.getMessage()))


@pytest.fixture
def turned_away(slurm, monkeypatch):
    """
    The slurm render in the engine with a document already served, the engine's log and clock
    the test's, and hold() and let_go() for another process holding the share's lock.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render import engine

    share = slurm.folder / ".trinityx-slurm"
    share.mkdir()
    item = engine.Render("slurm", "renders/config", Render(inputs=["luna.node"]),
                         slurm.plugin.render_config)
    item.document, item.rendered_at = {"nodes": 1, "files": []}, 1.0
    monkeypatch.setitem(engine.current().renders, item.key, item)
    clock = [1000.0]
    monkeypatch.setattr(engine, "time", SimpleNamespace(time=time.time,
                                                        monotonic=lambda: clock[0]))
    said = _Said()
    engine.LOGGER.addHandler(said)
    monkeypatch.setattr(engine.LOGGER, "level", logging.DEBUG)
    ctx = multiprocessing.get_context("fork")
    holders = []

    def hold() -> None:
        """Another process takes the share's lock and keeps it until let_go."""
        ready, done = ctx.Event(), ctx.Event()
        holder = ctx.Process(target=_hold, args=(str(share / "lock"), ready, done))
        holder.start()
        holders.append((holder, done))
        assert ready.wait(10)

    def let_go() -> None:
        """Every holder lets go of the lock and is gone."""
        while holders:
            holder, done = holders.pop()
            done.set()
            holder.join(10)

    def levels() -> list:
        """What the engine said about this render, by level."""
        return [level for level, message in said.said
                if message.startswith((f"render {item.key} ", f"rendered {item.key}"))]

    try:
        yield SimpleNamespace(item=item, clock=clock, hold=hold, let_go=let_go,
                              levels=levels, engine=engine.current())
    finally:
        let_go()
        engine.LOGGER.removeHandler(said)
        timer = engine.current().trailing.pop(item.key, None)
        if timer is not None:
            timer.cancel()


def test_a_render_a_held_lock_turns_away_is_not_a_failure(turned_away):
    """
    A save holds the lock as a matter of course, so the render it turns away says so at INFO and
    leaves the render as it was: no ERROR, no error for health to report, the previous document
    still served, and one more run asked for.
    """
    item, engine = turned_away.item, turned_away.engine
    turned_away.hold()
    assert engine.run(item) is False
    assert turned_away.levels() == ["INFO"], turned_away.levels()
    assert item.last_error == ""
    assert [one["error"] for one in engine.listing() if one["render"] == item.key] == [""]
    assert engine.reader("slurm", "renders/config")() == {"nodes": 1, "files": []}
    assert item.key in engine.trailing


def test_a_lock_held_past_a_minute_is_warned_of_once(turned_away):
    """
    A lock that never frees must not go quiet at INFO: past a minute of being turned away the
    render says so once as a WARNING, and every retry after that is INFO again.
    """
    turned_away.hold()
    for now in (1000.0, 1030.0, 1061.0, 1070.0):
        turned_away.clock[0] = now
        turned_away.engine.run(turned_away.item)
    assert turned_away.levels() == ["INFO", "INFO", "WARNING", "INFO"], turned_away.levels()


def test_a_render_that_lands_starts_the_minute_again(turned_away, slurm, monkeypatch):
    """
    The minute counts from the first render turned away since the last one that rendered, so a
    lock freed in between is a new wait and not the old one carried on.
    """
    monkeypatch.setattr(slurm.backend.Backend, "_render",
                        lambda self, settings: {"nodes": 0, "files": []})
    turned_away.hold()
    turned_away.engine.run(turned_away.item)
    turned_away.let_go()
    turned_away.clock[0] = 1030.0
    assert turned_away.engine.run(turned_away.item) is True
    turned_away.hold()
    for now in (1040.0, 1070.0, 1101.0):
        turned_away.clock[0] = now
        turned_away.engine.run(turned_away.item)
    assert turned_away.levels() == ["INFO", "INFO", "INFO", "INFO", "WARNING"], \
        turned_away.levels()


def test_a_lock_the_render_cannot_open_is_a_render_failure(slurm, monkeypatch):
    """A share that squashes root refuses root's open: the render says so as a BackendFailure
    and writes nothing, never an unhandled PermissionError."""
    (slurm.folder / ".trinityx-slurm").mkdir()

    def refused(*args, **kwargs):
        """An os.open that is refused with EACCES."""
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(slurm.backend.os, "open", refused)
    with pytest.raises(BackendFailure) as raised:
        slurm.plugin.render_config()
    assert "cannot take the lock" in str(raised.value) and "Permission denied" in str(raised.value)


def test_without_the_share_the_render_takes_no_lock(slurm, monkeypatch):
    """
    Where no share directory exists (the helper was never installed) the render takes no lock and
    creates no directory.
    """
    entered = []
    with slurm.backend.render_lock(slurm.plugin.config()):
        entered.append(True)
    assert entered and not (slurm.folder / ".trinityx-slurm").exists()


def test_a_reconfigure_that_does_not_finish_puts_the_blocks_back(rig, monkeypatch, tmp_path):
    """
    A silent scontrol raised past the rollback and left the new blocks on the disk; it is read
    as a refusal now, so the previous blocks go back as they do for one slurm refused.
    """
    from tests.unit.test_slurm_reconfigure import _held, _slurm
    from trixd.plugins.slurm import backend

    _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path)
    backend.Backend().render_config()
    kept = _held(tmp_path)
    _slurm(monkeypatch, {"node001": {"group": "gpu"}}, tmp_path, content="two")
    calls = []

    def silent_once(argv, **kwargs):
        """A shell.run whose first scontrol does not finish; the second answers."""
        calls.append(argv)
        if len(calls) == 1:
            raise BackendFailure("/usr/bin/scontrol did not finish within 60s")
        return Ran()

    monkeypatch.setattr(backend.shell, "run", silent_once)
    refused = ""
    try:
        backend.Backend().render_config()
    except BackendFailure as error:
        refused = str(error)
    assert _held(tmp_path) == kept
    assert "did not finish within 60s" in refused and len(calls) == 2


# -- over HTTP, as the console reaches it -------------------------------------------------------


def test_a_person_drains_over_http_as_their_trixd_role_allows(rig, client, headers, monkeypatch,
                                                              tmp_path):
    """
    The route as a client reaches it: the person's own trixd token, the manifest's schema and
    minimum (operator) read against the person's trixd role, Luna asked as them, the helper
    handed their name. A reader is refused before anything runs; rootus passes, Luna not asked.
    """
    from trixd.core.plugins import loader
    from trixd.core.security import tokens
    from trixd.plugins.luna import persons
    from trixd.plugins.slurm.backend import DEFAULT_CONFIG
    from trixd.utils import declarations

    folder = tmp_path / "slurm"
    declarations.write("slurm", "config", "", {**DEFAULT_CONFIG,
                                               "nodes_file": str(folder / "slurm-nodes.conf")})
    assert loader.load(rig, "slurm").state == "loaded"
    loaded = sys.modules["trixd_plugin_slurm.backend"]
    ran, asked = [], []
    monkeypatch.setattr(loaded.shell, "run", lambda argv, timeout=None, check=False,
                        secret_input="": (ran.append(json.loads(secret_input)), Ran(json.dumps(
                            {"ok": True, "code": 200, "names": ["node001"]})))[1])
    monkeypatch.setattr(loaded, "pwd", SimpleNamespace(getpwnam=lambda name: name))
    monkeypatch.setattr(loaded, "grp", SimpleNamespace(getgrnam=lambda name: SimpleNamespace(
        gr_gid=4242)))
    monkeypatch.setattr(loaded, "SETPRIV", sys.executable)
    monkeypatch.setattr(loaded, "PRLIMIT", sys.executable)
    monkeypatch.setattr(getattr(loaded, "persons", persons), "may",
                        lambda person, table, names, bit: (
                            asked.append((person.name, table, list(names), bit)), {})[1])
    for name, role in (("anna", "operator"), ("rita", "reader")):
        made = client.put(f"/api/v1/identity/users/{name}", json={"role": role}, headers=headers)
        assert made.status_code == 200, made.text
    drain = "/api/v1/slurm/nodes/actions/drain"
    body = {"names": ["node001"], "reason": "fan"}

    answer = client.post(drain, json=body,
                         headers={"x-access-tokens": tokens.mint("anna", source="pam")})
    assert answer.status_code == 200, answer.text
    assert asked == [("anna", "node", ["node001"], "x")], asked
    assert [(run.get("person"), run.get("names")) for run in ran] == [("anna", ["node001"])]
    refused = client.post(drain, json=body,
                          headers={"x-access-tokens": tokens.mint("rita", source="pam")})
    assert refused.status_code == 403 and "operator" in refused.text, refused.text
    shapeless = client.post(drain, json={"names": "node001", "reason": "fan"},
                            headers={"x-access-tokens": tokens.mint("anna", source="pam")})
    assert shapeless.status_code == 422, shapeless.text
    assert client.post(drain, json=body, headers=headers).status_code == 200
    assert [run.get("person") for run in ran] == ["anna", "tester"] and len(asked) == 1


# -- the audit and the bounds -------------------------------------------------------------------


def test_the_person_is_in_trixds_audit_with_what_they_changed(slurm):
    """
    Each change is recorded under the person's name with what it changed; a refusal by Luna is
    recorded as refused, under the same name.
    """
    for verb, call in _calls(slurm).items():
        assert not isinstance(_outcome(call, _request()), TrixdError), verb
    assert [(a["principal"], a["action"], a["object"], a["outcome"]) for a in slurm.audited] == [
        ("admin", "slurm drain", "node001", "granted"),
        ("admin", "slurm resume", "node001", "granted"),
        ("admin", "slurm save", "cluster; State: node001", "granted"),
        ("admin", "slurm restore", "cluster", "granted"),
        ("admin", "slurm apply", "cluster", "granted")]
    slurm.audited.clear()
    slurm.luna["refused"] = {"node": {"node001": "node node001 is not available"}}
    _outcome(slurm.bare.drain, body=_drain(), request=_request("trixd-token-of-anna"))
    assert [(a["principal"], a["action"], a["object"], a["outcome"], a["detail"])
            for a in slurm.audited] == [("anna", "slurm drain", "node001", "refused",
                                         "node node001 is not available")]


def test_the_names_are_bounded_before_luna_is_asked(slurm):
    """
    1 to 4096 node names, each a node name: anything else is a BadRequest and Luna is not
    asked.
    """
    for names in ([], ["node%04d" % i for i in range(4097)], ["node[1-2]"], ["a,b"], [7]):
        refused = _outcome(slurm.plugin.drain, body=_drain(names=names))
        assert isinstance(refused, BadRequest), (names[:2], refused)
    assert slurm.luna["asked"] == [] and slurm.ran == []


def test_a_saves_judged_list_is_bounded_before_luna_is_asked(slurm):
    """A save's or a restore's `judged` is held to the same bounds as drain's names (it may be
    empty) before Luna is asked."""
    for judged in (["n%05d" % i for i in range(4097)], ["node[1-2]"], [7]):
        for verb in ("save", "restore"):
            refused = _outcome(getattr(slurm.plugin, verb), body=_write(slurm, judged=judged))
            assert isinstance(refused, BadRequest), (verb, refused)
            assert str(refused).startswith("judged: the list of 0 to 4096")
    assert slurm.luna["asked"] == [] and slurm.ran == []


def test_a_change_whose_outcome_is_unknown_is_audited_as_such(slurm):
    """A helper that did not finish may have drained the nodes; trixd's audit names the person
    with the outcome unknown, and a helper's refusal as refused."""
    slurm.said["raise"] = BackendFailure("/usr/bin/setpriv did not finish within 15s")
    assert isinstance(_outcome(slurm.plugin.drain, body=_drain()), BackendFailure)
    slurm.said.pop("raise")
    slurm.said["out"] = json.dumps({"ok": False, "code": 409, "message": "another save"})
    assert isinstance(_outcome(slurm.plugin.resume, body=_drain()), Conflict)
    assert [(a["principal"], a["action"], a["object"], a["outcome"]) for a in slurm.audited] == [
        ("admin", "slurm drain", "node001", "unknown"),
        ("admin", "slurm resume", "node001", "refused")]
    assert "did not finish" in slurm.audited[0]["detail"]


@pytest.mark.parametrize("verb", ["save", "restore"])
def test_a_save_names_in_the_audit_whose_state_it_changed(slurm, verb):
    """A save's or a restore's audit names the nodes whose State Luna judged, granted, refused
    by the helper and unknown alike, as a drain names its nodes."""
    write = getattr(slurm.plugin, verb)
    judged = ["node004", "node002", "node004"]
    assert not isinstance(_outcome(write, body=_write(slurm, judged=judged)), TrixdError)
    slurm.said["out"] = json.dumps({"ok": False, "code": 409, "message": "another save"})
    assert isinstance(_outcome(write, body=_write(slurm, judged=judged)), Conflict)
    slurm.said["raise"] = BackendFailure("/usr/bin/setpriv did not finish within 15s")
    assert isinstance(_outcome(write, body=_write(slurm, judged=judged)), BackendFailure)
    assert [(a["object"], a["outcome"]) for a in slurm.audited] == [
        ("cluster; State: node002,node004", outcome)
        for outcome in ("granted", "refused", "unknown")]


def test_a_save_that_changes_no_state_names_only_the_cluster(slurm):
    """With nothing judged, a save's audit names the cluster alone, as an apply's does."""
    assert not isinstance(_outcome(slurm.plugin.save, body=_write(slurm)), TrixdError)
    assert not isinstance(_outcome(slurm.plugin.apply), TrixdError)
    assert [(a["action"], a["object"]) for a in slurm.audited] == [
        ("slurm save", "cluster"), ("slurm apply", "cluster")]


def test_a_long_judged_list_is_cut_in_the_audit_and_the_rest_counted(slurm):
    """The audit's 200 characters name as many nodes as fit and count the rest, so the row
    still says how many States the save changed."""
    judged = ["node%04d" % i for i in range(4096)]
    assert not isinstance(_outcome(slurm.plugin.save, body=_write(slurm, judged=judged)),
                          TrixdError)
    said = slurm.audited[-1]["object"]
    assert said.startswith("cluster; State: node0000,node0001,") and len(said) <= 200, said
    named, more = said[len("cluster; State: "):].split("; and ")
    assert more == f"{4096 - len(named.split(','))} more", said
    assert named.split(",") == judged[:len(named.split(","))]


def test_a_cut_saves_put_back_files_reach_the_answer(slurm):
    """The helper's 409 after a cut save names the files it put back; the plugin passes them on
    in its sentence."""
    slurm.said["out"] = json.dumps({"ok": False, "code": 409, "put_back": ["/etc/slurm/gres.conf"],
                                    "message": "a save was cut short and its files are put back"})
    refused = _outcome(slurm.plugin.restore, body=_write(slurm))
    assert isinstance(refused, Conflict), refused
    assert str(refused) == ("a save was cut short and its files are put back (put back: "
                            "/etc/slurm/gres.conf)")


def _recorded_render(renders) -> list:
    """The slurm render registered under its manifest's key, recording each run after that."""
    ran = []
    renders().register("slurm", _render_path(),
                       SimpleNamespace(inputs=["luna.node", "luna.group"], coalesce=2),
                       lambda: ran.append(True) or {"nodes": 0, "files": []})
    ran.clear()  # a render registered after the reconcile runs once on its own
    return ran


@pytest.mark.parametrize("verb", ["save", "restore"])
def test_a_cut_saves_put_back_files_get_one_render(slurm, verb):
    """The render refused while a cut save's mark stood; once the helper has put the files back,
    the plugin asks that render once, so the files do not wait for the next node change."""
    from trixd.core.render.engine import current as renders

    ran = _recorded_render(renders)
    slurm.said["out"] = json.dumps({"ok": False, "code": 409, "put_back": ["/etc/slurm/gres.conf"],
                                    "message": "a save was cut short and its files are put back"})
    with renders().lock:  # the asked render waits here, so what was asked can be read
        assert isinstance(_outcome(getattr(slurm.plugin, verb), body=_write(slurm)), Conflict)
        assert set(renders().trailing) == {f"slurm/{_render_path()}"}
    for _ in range(100):
        if ran:
            break
        time.sleep(0.05)
    time.sleep(0.2)  # long enough for a second run to show
    assert ran == [True] and not renders().trailing


@pytest.mark.parametrize("answer", [
    {"ok": True, "code": 200, "stamp": {}},
    {"ok": False, "code": 409, "message": "nodes changed since this page loaded it"}],
    ids=["written", "refused"])
@pytest.mark.parametrize("verb", ["save", "restore"])
def test_a_save_or_restore_with_nothing_put_back_asks_no_render(slurm, verb, answer):
    """Only putting a cut save back asks a render: a save or a restore that wrote, or one refused
    with nothing put back, leaves the render to the node changes that ask for it."""
    from trixd.core.render.engine import current as renders

    ran = _recorded_render(renders)
    slurm.said["out"] = json.dumps(answer)
    _outcome(getattr(slurm.plugin, verb), body=_write(slurm))
    time.sleep(0.2)
    assert not renders().trailing and ran == []


@pytest.mark.parametrize("person", ["Anna Smith", "1anna", "a" * 64, "deptmgr1"])
def test_a_drain_by_a_person_luna_knows_reads_back_as_theirs(person):
    """The rules read the person back from a reason the helper's account wrote, so a drain made
    for any person Luna knows is shown as theirs, not as the helper account's."""
    from trixd.plugins.slurm import rules

    said = rules.authored(person, "bad DIMM")
    assert rules.author_of(said, rules.ACCOUNT) == (person, "bad DIMM")
    assert rules.author_of(said, "alex") == ("alex", said)


# -- the package file -----------------------------------------------------------------------------


def test_the_package_file_names_its_steps_and_proves_its_checklist():
    """
    package.yaml holds exactly the keys the installer reads and the plugin's own account and
    memory limit; every preflight and step command is an argv with an absolute program; and each
    checklist item names a test that exists.
    """
    import yaml

    from trixd.plugins.slurm import backend

    folder = os.path.dirname(backend.__file__)
    with open(os.path.join(folder, "package.yaml"), encoding="ascii") as handle:
        package = yaml.safe_load(handle)
    assert set(package) == {"name", "version", "description", "depends", "runs_as", "creates",
                            "preflight", "steps", "checklist"}
    assert package["name"] == backend.PLUGIN
    assert package["runs_as"]["helper"]["account"] == backend.ACCOUNT
    assert package["runs_as"]["helper"]["memory"] == backend.MEMORY
    assert set(package["steps"]) == {"install", "start", "stop", "remove"}
    for argv in package["preflight"] + [a for step in package["steps"].values() for a in step]:
        assert isinstance(argv, list) and all(isinstance(word, str) for word in argv), argv
        assert os.path.isabs(argv[0]), argv
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    for item in package["checklist"]:
        path, name = item["test"].split("::")
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            assert f"def {name}(" in handle.read(), item["test"]
