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
The slurm plugin's change routes: only the trixd gate's account asks, the person is judged from
their own Luna token, the body reaches the helper stepped down to its account, the helper's refusals come back as trixd's classes, and the render
shares the helper's lock and never writes over a cut save.
"""

__author__      = "Alex Ninaber"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Alex Ninaber"
__status__      = "Development"

import fcntl
import json
import multiprocessing
import os
import sys
from types import SimpleNamespace

import pytest
import requests

from tests.unit.test_plugins import Ran
from trixd.common.errors import BackendFailure, BadRequest, Conflict, Forbidden, ServiceUnavailable

GATE = SimpleNamespace(name="trinityx-gate", kind="user", rootus=False)
ROOTUS = SimpleNamespace(name="root", kind="user", rootus=True)
TOKEN = "the-persons-luna-token"
VERBS = ("drain", "resume", "preview", "save", "restore", "apply")
ADMIN = {"user": "admin", "id": 1, "source": "pam", "admin": True, "usergroups": {},
         "hardware": []}


def _request(token=TOKEN):
    """
    A stand-in HTTP request carrying the person's Luna token in x-luna-token, or no header when
    the token is None.
    """
    return SimpleNamespace(headers={"x-luna-token": token} if token is not None else {})


class _Answer:
    def __init__(self, status, body):
        """Takes the HTTP status and the JSON body (None for an empty answer)."""
        self.status_code, self._body = status, body
        self.content = b"" if body is None else json.dumps(body).encode()

    def json(self):
        """The body parsed as JSON, as requests answers it."""
        return json.loads(self.content)


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
    The plugin over files in tmp_path, the helper's runs recorded and answered from `said`.
    """
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
    luna = {"whoami": (200, dict(ADMIN)), "config/node": (200, {"config": {"node": {
        n: {"name": n} for n in ("node001", "node002", "node003")}}}),
            "config/cluster": (200, {"config": {"cluster": {"name": "c",
                                                             "access": "rwxr-x---"}}}),
            "asked": []}

    def get(url, headers=None, timeout=None, verify=None, allow_redirects=True):
        """
        The fake requests.get against Luna: records what was asked and answers the test's status
        and body for the path, or raises what the test set.
        """
        path = url.split("https://luna.test/", 1)[1]
        luna["asked"].append({"path": path, "headers": headers, "timeout": timeout,
                              "verify": verify, "allow_redirects": allow_redirects})
        if isinstance(luna.get(path), BaseException):
            raise luna[path]
        return _Answer(*luna[path])

    monkeypatch.setattr(backend.requests, "get", get)
    monkeypatch.setattr(backend.luna_config, "current",
                        lambda: SimpleNamespace(base="https://luna.test", verify=True))
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
    """The body of a drain of node001 by admin, with any field replaced."""
    return {"person": "admin", "names": ["node001"], "judged": ["node001"], "reason": "fan",
            **extra}


@pytest.mark.parametrize("caller", [
    None,
    SimpleNamespace(name="harry", kind="user", rootus=False),
    SimpleNamespace(name="trinityx-gate", kind="service", rootus=False),
], ids=["nobody", "another-user", "a-service-token"])
def test_only_the_gates_account_or_rootus_asks_for_a_change(slurm, caller):
    """
    Every change route refuses with Forbidden any caller that is neither the gate's account nor
    rootus.
    """
    plugin = slurm.plugin
    for call in (lambda: plugin.drain(body=_drain(), caller=caller),
                 lambda: plugin.resume(body=_drain(), caller=caller),
                 lambda: plugin.preview(body={"person": "admin", "mode": "save"}, caller=caller),
                 lambda: plugin.save(body={"person": "admin", "rules": "x"}, caller=caller),
                 lambda: plugin.restore(body={"person": "admin", "rules": "x"}, caller=caller),
                 lambda: plugin.apply(body={"person": "admin"}, caller=caller)):
        with pytest.raises(Forbidden):
            call()
    assert slurm.ran == []
    plugin.drain(body=_drain(), caller=GATE)
    plugin.drain(body=_drain(), caller=ROOTUS)
    assert len(slurm.ran) == 2


def test_the_gate_account_is_the_plugins_setting(slurm):
    """
    The account allowed to ask is the plugin's gate_account setting: once it is changed, the old
    account is refused and the new one served.
    """
    from trixd.utils import declarations

    declarations.merge("slurm", "config", "", {"gate_account": "other-gate"},
                       slurm.backend.DEFAULT_CONFIG)
    with pytest.raises(Forbidden):
        slurm.plugin.drain(body=_drain(), caller=GATE)
    slurm.plugin.drain(body=_drain(), caller=SimpleNamespace(name="other-gate", kind="user",
                                                             rootus=False))


def test_the_nodes_judged_must_be_the_nodes_named(slurm):
    """
    A drain or resume whose judged list is not its names (repeats aside) is refused with Conflict
    and runs nothing.
    """
    with pytest.raises(Conflict):
        slurm.plugin.drain(body=_drain(judged=[]), caller=GATE)
    with pytest.raises(Conflict):
        slurm.plugin.resume(body=_drain(names=["node001", "node002"]), caller=GATE)
    assert slurm.ran == []
    slurm.plugin.resume(body=_drain(names=["node002", "node001", "node001"],
                                    judged=["node001", "node002"]), caller=GATE)
    assert len(slurm.ran) == 1


def test_a_write_carries_this_plugins_rules_digest(slurm):
    """
    A save or restore whose rules digest is not this plugin's is refused with Conflict; one
    carrying it reaches the helper with it.
    """
    body = {"person": "admin", "texts": {}, "stamp": {}, "judged": [], "rules": "0" * 64}
    for verb in ("save", "restore"):
        with pytest.raises(Conflict) as refused:
            getattr(slurm.plugin, verb)(body=body, caller=GATE)
        assert "rules" in str(refused.value)
    assert slurm.ran == []
    slurm.plugin.save(body={**body, "rules": slurm.backend.rules_digest()}, caller=GATE)
    assert slurm.ran[-1]["request"]["rules"] == slurm.backend.rules_digest()


def test_the_helper_runs_stepped_down_with_the_request_on_stdin(slurm):
    """
    The helper runs through setpriv as the helper's account with cleared groups, no new
    privileges, a reset environment and an address-space limit, with the request on standard
    input and the verb's timeout.
    """
    slurm.plugin.drain(body=_drain(), caller=GATE)
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
        **_drain()}


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
    digest = slurm.backend.rules_digest()
    write = {"person": "admin", "texts": {}, "stamp": {}, "judged": [], "rules": digest}
    slurm.plugin.save(body=write, caller=GATE)
    slurm.plugin.restore(body=write, caller=GATE)
    slurm.plugin.drain(body=_drain(), caller=GATE)
    slurm.plugin.resume(body=_drain(), caller=GATE)
    slurm.plugin.preview(body={"person": "admin", "mode": "save"}, caller=GATE)
    slurm.plugin.apply(body={"person": "admin"}, caller=GATE)
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


# The trixd gate's wait per verb, as the console's gate sets it (its own test holds
# its row timeouts to the same table).
GATE_WAITS = {"files": 8, "drain": 35, "resume": 35, "preview": 20, "save": 75, "restore": 75,
              "apply": 75}


def test_each_verbs_helper_waits_longer_than_the_work_inside_it():
    """Read from the constants, so a change to any of them is judged: the helper's own waits
    inside the plugin's `shell.run`, which with the plugin's Luna reads is inside the gate's."""
    from tests.unit.test_slurm_ops import helper
    from trixd.plugins.slurm import backend

    inner = {"files": helper.GRANT_WAIT,
             "drain": helper.GRANT_WAIT + helper.WAIT + helper.QUICK,
             "resume": helper.GRANT_WAIT + helper.WAIT + helper.QUICK,
             "preview": helper.WAIT, "save": helper.WAIT + 35, "restore": helper.WAIT + 35,
             "apply": helper.WAIT + helper.SLOW}
    assert set(backend.TIMEOUTS) == set(inner) == set(GATE_WAITS)
    for verb, work in inner.items():
        assert backend.TIMEOUTS[verb] > work, verb
    assert backend.RENDER_WAIT > backend.TIMEOUTS["save"]


def test_the_plugins_luna_reads_and_its_helper_fit_in_the_gates_wait(slurm):
    """Counted, not written down: each verb's Luna reads at the most (a save that changes a
    State reads the nodes too), each up to LUNA_WAIT's connect and answer, then the helper."""
    slurm.luna["whoami"] = (200, _person(groups={"lab": "admin"}, hardware=["lab"]))
    slurm.luna["config/cluster"] = (200, {"config": {"cluster": {
        "usergroups": "lab", "access": "rwxrwx---"}}})
    slurm.luna["config/node"] = (200, {"config": {"node": {
        "node001": {"usergroups": "lab", "access": "rwxrwx---"}}}})
    per_read = sum(slurm.backend.LUNA_WAIT)
    for verb, call in _calls(slurm).items():
        slurm.luna["asked"].clear()
        call(request=_request())
        spent = len(slurm.luna["asked"]) * per_read + slurm.backend.TIMEOUTS[verb]
        assert spent < GATE_WAITS[verb], (verb, len(slurm.luna["asked"]), spent)


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
        slurm.plugin.apply(body={"person": "admin"}, caller=GATE)
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
        slurm.plugin.apply(body={"person": "admin"}, caller=GATE)
    assert str(refused.value) == "the Slurm helper did not answer apply; the reason is in the journal"


def test_a_helper_that_does_not_finish_leaves_the_outcome_unknown(slurm):
    """A write whose helper does not finish is a BackendFailure saying the outcome is unknown."""
    slurm.said["raise"] = BackendFailure("/usr/bin/setpriv did not finish within 45s")
    with pytest.raises(BackendFailure) as refused:
        slurm.plugin.save(body={"person": "admin", "texts": {}, "stamp": {}, "judged": [],
                                "rules": slurm.backend.rules_digest()}, caller=GATE)
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
    slurm.plugin.preview(body={"person": "admin", "mode": "restore", "stamp": {}, "texts": None},
                         caller=GATE)
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


def test_the_render_waits_for_a_save_then_fails_loud(slurm, monkeypatch):
    """
    The render waits RENDER_WAIT for a save holding the lock, then fails with BackendFailure
    instead of writing.
    """
    share = slurm.folder / ".trinityx-slurm"
    share.mkdir()
    monkeypatch.setattr(slurm.backend, "RENDER_WAIT", 0.3)
    ctx = multiprocessing.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(share / "lock"), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        with pytest.raises(BackendFailure) as refused:
            slurm.plugin.render_config()
        assert "held the slurm files" in str(refused.value)
    finally:
        done.set()
        holder.join(10)


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


# -- the person, judged from their own Luna token -----------------------------------------------


def _person(user="anna", groups=None, hardware=()):
    """
    Luna's whoami answer for a person who is not admin, with the given user groups and hardware
    groups.
    """
    return {"user": user, "id": 7, "source": "pam", "admin": False,
            "usergroups": groups or {"lab": "operator"}, "hardware": list(hardware)}


def _calls(slurm, person="anna"):
    """Every change route as a call by the given person, taking the request to send."""
    digest = slurm.backend.rules_digest()
    return {
        "drain": lambda **k: slurm.bare.drain(body=_drain(person=person), caller=GATE, **k),
        "resume": lambda **k: slurm.bare.resume(body=_drain(person=person), caller=GATE, **k),
        "preview": lambda **k: slurm.bare.preview(body={"person": person, "mode": "save"},
                                                  caller=GATE, **k),
        "save": lambda **k: slurm.bare.save(body={"person": person, "texts": {}, "stamp": {},
                                                  "judged": ["node001"], "rules": digest},
                                            caller=GATE, **k),
        "restore": lambda **k: slurm.bare.restore(body={"person": person, "stamp": {},
                                                        "judged": [], "rules": digest},
                                                  caller=GATE, **k),
        "apply": lambda **k: slurm.bare.apply(body={"person": person}, caller=GATE, **k),
    }


@pytest.mark.parametrize("verb", VERBS)
def test_the_plugin_judges_the_person_from_their_luna_token(slurm, verb):
    """
    Each route refuses with Forbidden without a token or with one Luna refuses, asking Luna
    nothing without a token, and runs only for the person Luna's whoami names.
    """
    call = _calls(slurm)[verb]
    slurm.luna["whoami"] = (200, _person())
    for request, why in ((None, "no Luna sign-in"), (_request(None), "no Luna sign-in"),
                         (_request(""), "no Luna sign-in")):
        with pytest.raises(Forbidden) as refused:
            call(request=request)
        assert why in str(refused.value)
    assert slurm.luna["asked"] == []
    slurm.luna["whoami"] = (401, {"message": "token expired"})
    with pytest.raises(Forbidden) as refused:
        call(request=_request())
    assert "sign in again" in str(refused.value)
    slurm.luna["whoami"] = (200, _person(user="harry"))
    with pytest.raises(Forbidden) as refused:
        call(request=_request())
    assert str(refused.value) == "the Luna sign-in is harry's, not anna's"
    slurm.luna["whoami"] = (200, {"message": "no user"})
    with pytest.raises(Forbidden):
        call(request=_request())
    slurm.luna["whoami"] = requests.ConnectionError("refused")
    with pytest.raises(ServiceUnavailable) as refused:
        call(request=_request())
    assert str(refused.value) == "Luna did not answer whoami: nothing was changed"
    assert slurm.ran == []
    for asked in slurm.luna["asked"]:
        assert asked["headers"] == {"x-access-tokens": TOKEN}
        assert asked["timeout"] == slurm.backend.LUNA_WAIT
        assert asked["verify"] is True and asked["allow_redirects"] is False


@pytest.mark.parametrize("path, answer", [
    (path, answer) for path in ("config/node", "config/cluster", "whoami")
    for answer in ("timeout", "500", "not json")] + [("whoami", "a bad id")])
def test_luna_silent_or_strange_on_any_read_is_503_and_nothing_runs(slurm, path, answer):
    """
    Luna timing out, failing or answering something that is not the expected JSON on any read is
    ServiceUnavailable and no helper runs.
    """
    slurm.luna["whoami"] = (200, _person(groups={"lab": "admin"}, hardware=["lab"]))
    slurm.luna["config/cluster"] = (200, {"config": {"cluster": {
        "usergroups": "lab", "access": "rwxrwx---"}}})
    if answer == "a bad id":
        slurm.luna[path] = (200, _person() | {"id": "seven"})
    else:
        slurm.luna[path] = {"timeout": requests.Timeout("slow"),
                            "500": (500, {"message": "down"}),
                            "not json": (200, ["a", "list"])}[answer]
    verbs = {"config/node": ("drain", "save"), "config/cluster": ("save", "apply"),
             "whoami": ("drain", "save", "apply")}[path]
    for verb in verbs:
        with pytest.raises(ServiceUnavailable):
            _calls(slurm)[verb](request=_request())
    assert slurm.ran == []


def test_drain_and_resume_need_x_on_every_node_named(slurm):
    """
    A drain or resume needs Luna's x on every node named; one node without it refuses the whole
    request, naming it.
    """
    slurm.luna["whoami"] = (200, _person())
    shared = {"usergroups": "lab", "access": "rwxr-x---"}
    slurm.luna["config/node"] = (200, {"config": {"node": {
        "node001": shared, "node002": {"usergroups": "other", "access": "rwxr-x---"}}}})
    both = _drain(person="anna", names=["node001", "node002"], judged=["node001", "node002"])
    for verb in ("drain", "resume"):
        with pytest.raises(Forbidden) as refused:
            getattr(slurm.plugin, verb)(body=both, caller=GATE)
        assert str(refused.value) == ("not permitted for 1 of 2 nodes: node002 (node node002 "
                                      "is not available)")
    assert slurm.ran == []
    slurm.luna["config/node"] = (404, {"message": "no nodes"})
    with pytest.raises(Forbidden) as refused:
        slurm.plugin.drain(body=_drain(person="anna"), caller=GATE)
    assert "node001 (node node001 is not available)" in str(refused.value)
    slurm.luna["config/node"] = (200, {"config": {"node": {"node001": shared}}})
    slurm.plugin.drain(body=_drain(person="anna"), caller=GATE)
    slurm.plugin.resume(body=_drain(person="anna"), caller=GATE)
    assert [run["argv"][-1] for run in slurm.ran] == ["drain", "resume"]
    slurm.luna["whoami"] = (200, _person(groups={"lab": "reader"}))
    with pytest.raises(Forbidden) as refused:
        slurm.plugin.drain(body=_drain(person="anna"), caller=GATE)
    assert "operating node node001 is not permitted: you may read it (reader role)" in \
        str(refused.value)
    assert len(slurm.ran) == 2


def test_save_restore_and_apply_need_w_on_the_cluster_and_states_the_hardware_right(slurm):
    """
    Save, restore and apply need Luna's w on the cluster, and a save that changes a node's State
    also needs the hardware right on that node; the refusal says what the person may do.
    """
    calls = _calls(slurm)
    slurm.luna["whoami"] = (200, _person(groups={"lab": "manager"}))
    slurm.luna["config/cluster"] = (200, {"config": {"cluster": {
        "usergroups": "lab", "access": "rwxr-x---"}}})
    for verb in ("save", "restore", "apply"):
        with pytest.raises(Forbidden) as refused:
            calls[verb](request=_request())
        assert str(refused.value) == ("changing or removing cluster is not permitted: you may "
                                      "read and operate it (manager role)")
    slurm.luna["config/cluster"] = (200, {"config": {"cluster": {
        "usergroups": "lab", "access": "rwxrwx---"}}})
    slurm.luna["config/node"] = (200, {"config": {"node": {
        "node001": {"usergroups": "lab", "access": "rwxrwx---"}}}})
    with pytest.raises(Forbidden) as refused:
        calls["save"](request=_request())
    assert str(refused.value) == ("not permitted for 1 of 1 nodes: node001 (changing its State "
                                  "needs the hardware right)")
    assert slurm.ran == []
    calls["restore"](request=_request())   # judged nothing: no node read needed
    calls["apply"](request=_request())
    slurm.luna["whoami"] = (200, _person(groups={"lab": "manager"}, hardware=["lab"]))
    calls["save"](request=_request())
    assert [run["argv"][-1] for run in slurm.ran] == ["restore", "apply", "save"]


def test_the_token_reaches_no_helper_and_no_log(slurm, caplog):
    """
    The person's Luna token is never in a helper request nor in any log line, also when Luna's
    error message carries it.
    """
    caplog.set_level("DEBUG")
    for verb, call in _calls(slurm, person="admin").items():
        call(request=_request())
    slurm.luna["whoami"] = requests.ConnectionError(f"refused with {TOKEN}")
    with pytest.raises(ServiceUnavailable):
        _calls(slurm, person="admin")["apply"](request=_request())
    assert len(slurm.ran) == len(VERBS)
    assert all(TOKEN not in json.dumps(run) for run in slurm.ran)
    assert TOKEN not in caplog.text


# -- the token-path critic (PLAN-r140a section 16, F1-F9) ----------------------------------------------


def test_the_persons_token_is_checked_against_the_cluster_ca_when_one_is_set(slurm, tmp_path):
    """F1: luna_ca, where set, is the CA Luna's certificate is checked against; unset or "",
    luna.ini's VERIFY_CERTIFICATE decides (True in the fixture)."""
    from trixd.utils import declarations

    slurm.plugin.drain(body=_drain(), caller=GATE)
    assert {a["verify"] for a in slurm.luna["asked"]} == {True}
    ca = tmp_path / "ca.crt"
    ca.write_text("a CA")
    declarations.merge("slurm", "config", "", {"luna_ca": str(ca)}, slurm.backend.DEFAULT_CONFIG)
    slurm.luna["asked"].clear()
    slurm.plugin.drain(body=_drain(), caller=GATE)
    assert slurm.luna["asked"] and {a["verify"] for a in slurm.luna["asked"]} == {str(ca)}
    declarations.merge("slurm", "config", "", {"luna_ca": ""}, slurm.backend.DEFAULT_CONFIG)
    slurm.luna["asked"].clear()
    slurm.plugin.drain(body=_drain(), caller=GATE)
    assert {a["verify"] for a in slurm.luna["asked"]} == {True}


def test_a_luna_ca_that_is_not_a_file_is_503_naming_it(slurm, tmp_path, monkeypatch):
    """Review 3 (1): a CA file that is not there was an OSError past the RequestException
    catch, a 500 on every change; it is a 503 naming the setting, before any read, and an
    OSError from requests is a 503 too."""
    from trixd.utils import declarations

    gone = str(tmp_path / "no-such-ca.crt")
    declarations.merge("slurm", "config", "", {"luna_ca": gone}, slurm.backend.DEFAULT_CONFIG)
    for verb, call in _calls(slurm, person="admin").items():
        with pytest.raises(ServiceUnavailable) as said:
            call(request=_request())
        assert f"luna_ca names {gone}" in str(said.value), verb
    assert slurm.luna["asked"] == [] and slurm.ran == []
    declarations.merge("slurm", "config", "", {"luna_ca": ""}, slurm.backend.DEFAULT_CONFIG)
    slurm.luna["whoami"] = OSError("Could not find a suitable TLS CA certificate bundle")
    with pytest.raises(ServiceUnavailable) as said:
        slurm.plugin.drain(body=_drain(), caller=GATE)
    assert str(said.value) == "Luna did not answer whoami: nothing was changed"


@pytest.mark.parametrize("base, refused", [
    ("http://luna.test", True), ("http://10.141.255.254:7050", True),
    ("http://localhost:7050", False), ("http://127.0.0.1:7050", False), ("http://[::1]:7050", False),
    ("https://luna.test", False)])
def test_no_sign_in_goes_over_plain_http_off_this_host(slurm, monkeypatch, base, refused):
    """F1: plain http to Luna carries the person's whole Luna in clear; only the loopback is
    spared, and the refusal comes before any read."""
    monkeypatch.setattr(slurm.backend.luna_config, "current",
                        lambda: SimpleNamespace(base=base, verify=False))
    monkeypatch.setattr(slurm.backend.requests, "get",
                        lambda url, **kw: (slurm.luna["asked"].append(url),
                                           _Answer(*slurm.luna[url.split("/", 3)[3]]))[1])
    if refused:
        with pytest.raises(ServiceUnavailable) as said:
            slurm.plugin.drain(body=_drain(), caller=GATE)
        assert "plain http" in str(said.value)
        assert slurm.luna["asked"] == [] and slurm.ran == []
    else:
        slurm.plugin.drain(body=_drain(), caller=GATE)
        assert len(slurm.ran) == 1


def test_a_row_without_an_access_column_is_refused_for_anyone_but_an_admin(slurm):
    """F2: the shared ladder holds a row without `access` fully; nothing stands behind the
    plugin, so here such a row is 503 for a non-admin, and nothing runs."""
    slurm.luna["whoami"] = (200, _person(groups={"lab": "admin"}, hardware=["lab"]))
    calls = _calls(slurm)
    for access in ({}, {"access": None}, {"access": ""}):  # review 3 (3): null and "" too
        slurm.luna["config/cluster"] = (200, {"config": {"cluster": {"name": "c", **access}}})
        slurm.luna["config/node"] = (200, {"config": {"node": {"node001": {"name": "node001",
                                                                           **access}}}})
        for verb in ("drain", "resume", "apply", "save"):
            with pytest.raises(ServiceUnavailable) as said:
                calls[verb](request=_request())
            assert "carries no access column" in str(said.value)
    assert slurm.ran == []
    slurm.luna["whoami"] = (200, dict(ADMIN))
    for verb in ("drain", "apply"):
        _calls(slurm, person="admin")[verb](request=_request())
    assert len(slurm.ran) == 2


def test_a_role_this_copy_does_not_know_is_a_sentence_not_a_500(slurm):
    """F3: rbac.refusal ranked roles by ROLE_CAPS.index, which raised on a role it does not
    know; the cap of an unknown role is nothing, and the sentence names it."""
    slurm.luna["whoami"] = (200, _person(groups={"lab": "auditor"}))
    slurm.luna["config/node"] = (200, {"config": {"node": {
        "node001": {"usergroups": "lab", "access": "rwxr-xr--"}}}})
    with pytest.raises(Forbidden) as refused:
        slurm.plugin.drain(body=_drain(person="anna"), caller=GATE)
    assert str(refused.value) == ("not permitted for 1 of 1 nodes: node001 (node node001 is not "
                                  "available)")
    slurm.luna["config/node"] = (200, {"config": {"node": {
        "node001": {"usergroups": "lab,ops", "access": "rwxrwxr--"}}}})
    slurm.luna["whoami"] = (200, _person(groups={"lab": "auditor", "ops": "reader"}))
    with pytest.raises(Forbidden) as refused:
        slurm.plugin.drain(body=_drain(person="anna"), caller=GATE)
    assert "(reader role)" in str(refused.value)
    assert slurm.ran == []


def test_the_verified_person_is_in_trixds_audit_never_the_token(slurm):
    """F4: trixd's own row names the gate's account; the plugin records the person it verified,
    what they changed, and a refusal by Luna's ladder."""
    for verb, call in _calls(slurm, person="admin").items():
        call(request=_request())
    granted = [(a["principal"], a["action"], a["object"]) for a in slurm.audited]
    assert granted == [("admin", "slurm drain", "node001"), ("admin", "slurm resume", "node001"),
                       ("admin", "slurm save", "cluster"), ("admin", "slurm restore", "cluster"),
                       ("admin", "slurm apply", "cluster")]
    assert all(a["outcome"] == "granted" and a["detail"] == "via trinityx-gate"
               for a in slurm.audited)
    slurm.audited.clear()
    slurm.luna["whoami"] = (200, _person(groups={"lab": "reader"}))
    slurm.luna["config/node"] = (200, {"config": {"node": {
        "node001": {"usergroups": "lab", "access": "rwxr-x---"}}}})
    with pytest.raises(Forbidden):
        slurm.plugin.drain(body=_drain(person="anna"), caller=GATE)
    slurm.luna["whoami"] = (200, _person(user="harry"))
    with pytest.raises(Forbidden):
        slurm.plugin.drain(body=_drain(person="anna"), caller=GATE)
    assert [(a["principal"], a["action"], a["outcome"]) for a in slurm.audited] == [
        ("anna", "slurm drain", "refused"), ("harry", "slurm sign-in", "refused")]
    assert TOKEN not in json.dumps(slurm.audited)


@pytest.mark.parametrize("person", ["Anna.Smith", "Anna Smith", "1anna", "a" * 64])
def test_a_person_luna_knows_is_one_the_schema_takes(person):
    """F5: the person pattern is Luna's user grammar (letters of either case, digits, _ . - and
    an inner blank), not a unix name's; the whoami equality is the real guard."""
    import re

    import yaml

    from trixd.plugins.slurm import backend

    with open(os.path.join(os.path.dirname(backend.__file__), "manifest.yaml"),
              encoding="utf-8") as handle:
        pattern = yaml.safe_load(handle)["schemas"]["drain"]["person"]["pattern"]
    assert re.search(pattern, person)
    for refused in (" anna", "anna ", "", "a" * 65, "an/na", "[anna]"):
        assert not re.search(pattern, refused), refused


@pytest.mark.parametrize("person", ["Anna Smith", "1anna", "a" * 64, "deptmgr1"])
def test_a_drain_by_a_person_luna_knows_reads_back_as_theirs(person):
    """F5: the rules read the person back by the manifest's pattern, so a drain the gate wrote
    for any person Luna knows is shown as theirs, not as the gate account's."""
    import yaml

    from trixd.plugins.slurm import backend, rules

    with open(os.path.join(os.path.dirname(backend.__file__), "manifest.yaml"),
              encoding="utf-8") as handle:
        pattern = yaml.safe_load(handle)["schemas"]["drain"]["person"]["pattern"]
    assert pattern == "^%s$" % rules.PERSON.replace("(?:", "(")
    said = rules.authored(person, "bad DIMM")
    assert rules.author_of(said, rules.GATE_ACCOUNT) == (person, "bad DIMM")
    assert rules.author_of(said, "alex") == ("alex", said)


@pytest.mark.parametrize("status, body", [(403, {"message": "provision token"}),
                                          (404, {"message": "no such user"}),
                                          (401, None), (401, "<html>proxy</html>")])
def test_a_luna_that_no_longer_knows_the_sign_in_is_forbidden_not_down(slurm, status, body):
    """F7: a disabled or deleted user (404), a token of another scope (403) and a 401 with any
    body say 'sign in again', never 'Luna did not answer'."""
    class Raw(_Answer):
        def __init__(self, status, text):
            """Takes the HTTP status and a body text that is not JSON."""
            self.status_code, self.content = status, text.encode()

    slurm.luna["whoami"] = Raw(status, body) if isinstance(body, str) else (status, body)
    get = slurm.backend.requests.get

    def answer(url, **kw):
        """Answers whoami with the raw answer the test set, any other path through the fake get."""
        found = slurm.luna["whoami"]
        return found if isinstance(found, Raw) and url.endswith("whoami") else get(url, **kw)

    slurm.backend.requests.get = answer
    try:
        with pytest.raises(Forbidden) as refused:
            slurm.plugin.drain(body=_drain(), caller=GATE)
    finally:
        slurm.backend.requests.get = get
    assert str(refused.value) == "Luna no longer knows this sign-in: sign in again"
    assert slurm.ran == []


def test_the_names_are_bounded_before_any_luna_read(slurm):
    """F8: 1 to 4096 node names, each a node name, checked before whoami; a refusal names the
    first twenty and counts the rest."""
    for names in ([], ["node%04d" % i for i in range(4097)], ["node[1-2]"], ["a,b"], [7]):
        with pytest.raises(BadRequest):
            slurm.plugin.drain(body=_drain(names=names, judged=names), caller=GATE)
    assert slurm.luna["asked"] == [] and slurm.ran == []
    many = ["n%02d" % i for i in range(50)]
    slurm.luna["whoami"] = (200, _person())
    slurm.luna["config/node"] = (200, {"config": {"node": {}}})
    with pytest.raises(Forbidden) as refused:
        slurm.plugin.drain(body=_drain(person="anna", names=many, judged=many), caller=GATE)
    said = str(refused.value)
    assert said.startswith("not permitted for 50 of 50 nodes: n00 (node n00 is not available)")
    assert said.endswith("n19 (node n19 is not available); and 30 more")


def test_a_saves_judged_list_is_bounded_before_any_luna_read(slurm):
    """Review 3 (4): a save's or a restore's `judged` is held to the same bounds as drain's
    names (it may be empty) before whoami."""
    digest = slurm.backend.rules_digest()
    for judged in (["n%05d" % i for i in range(4097)], ["node[1-2]"], [7]):
        for verb in ("save", "restore"):
            with pytest.raises(BadRequest) as said:
                getattr(slurm.plugin, verb)(body={"person": "admin", "texts": {}, "stamp": {},
                                                  "judged": judged, "rules": digest},
                                            caller=GATE)
            assert str(said.value).startswith("judged: the list of 0 to 4096")
    assert slurm.luna["asked"] == [] and slurm.ran == []


def test_a_change_whose_outcome_is_unknown_is_audited_as_such(slurm):
    """Review 3 (6): a helper that did not finish may have drained the nodes; trixd's audit
    names the person with the outcome unknown, and a helper's refusal as refused."""
    slurm.said["raise"] = BackendFailure("/usr/bin/setpriv did not finish within 15s")
    with pytest.raises(BackendFailure):
        slurm.plugin.drain(body=_drain(), caller=GATE)
    slurm.said.pop("raise")
    slurm.said["out"] = json.dumps({"ok": False, "code": 409, "message": "another save"})
    with pytest.raises(Conflict):
        slurm.plugin.resume(body=_drain(), caller=GATE)
    assert [(a["principal"], a["action"], a["object"], a["outcome"]) for a in slurm.audited] == [
        ("admin", "slurm drain", "node001", "unknown"),
        ("admin", "slurm resume", "node001", "refused")]
    assert "did not finish" in slurm.audited[0]["detail"]


def test_a_cut_saves_put_back_files_reach_the_answer(slurm):
    """!1021 review 3: the helper's 409 after a cut save names the files it put back; the plugin
    passes them on in its sentence."""
    slurm.said["out"] = json.dumps({"ok": False, "code": 409, "put_back": ["/etc/slurm/gres.conf"],
                                    "message": "a save was cut short and its files are put back"})
    with pytest.raises(Conflict) as refused:
        _calls(slurm, person="admin")["restore"](request=_request())
    assert str(refused.value) == ("a save was cut short and its files are put back (put back: "
                                  "/etc/slurm/gres.conf)")
