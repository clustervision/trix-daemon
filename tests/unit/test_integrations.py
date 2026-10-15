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
Where the daemon meets something outside it: Luna, the peer controller, PAM, an
LDAP directory, obol and the slurm generator. Each is stood in for at its edge, so
the daemon's own code runs as it does on a controller.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
import requests

from tests.unit.test_plugins import Ran
from tests.unit.test_replication import _peer
from trixd.common import settings as configuration
from trixd.common.errors import BackendFailure, BadRequest, NotFound, ServiceUnavailable
from trixd.core.replication import sweep as replication
from trixd.core.replication import tables
from trixd.db import engine as database
from trixd.db import models
from trixd.plugins.luna import hashes
from trixd.plugins.luna.client import LunaClient


class Answer:
    """
    What requests would have answered: a status, and a body as JSON, text or nothing.
    """

    def __init__(self, status: int = 200, body=None, text: str = None) -> None:
        """
        Hold the status and the body.
        """
        self.status_code = status
        self.body = body
        self.text = text if text is not None else ("" if body is None else json.dumps(body))
        self.content = self.text.encode()

    def json(self):
        """
        The body, or the ValueError requests raises for one that is not JSON.
        """
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


class Session:
    """
    A requests session that answers from a script and remembers every call.
    """

    def __init__(self, *answers) -> None:
        """
        The answers, in the order the calls will take them.
        """
        self.answers = list(answers)
        self.calls = []

    def _next(self, method, url, kwargs):
        """
        Record the call and hand out the next answer, raising it if it is an error.
        """
        self.calls.append((method, url, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, url, **kwargs):
        """
        The token request.
        """
        return self._next("POST", url, kwargs)

    def request(self, method, url, **kwargs):
        """
        Every other request.
        """
        return self._next(method, url, kwargs)


def _luna(*answers) -> tuple:
    """
    A fresh client whose session answers from the script.
    """
    client = LunaClient()
    client.session = Session(*answers)
    return client, client.session


def test_trixd_signs_its_own_luna_token_and_reuses_it(rig):
    """
    No sign-in at Luna: trixd signs a token with luna.ini's secret, as Luna's administrator.
    """
    import jwt

    from tests.conftest import SHARED_SECRET

    client, session = _luna(Answer(body={"a": 1}), Answer(body={"b": 2}))
    assert client.get("config/a") == {"a": 1}
    assert client.get("/config/b") == {"b": 2}
    assert [call[0] for call in session.calls] == ["GET", "GET"], "nothing was posted to /token"
    assert session.calls[0][1] == "https://luna.test:7050/config/a", "luna.ini's address"
    assert session.calls[1][1].endswith("/config/b"), "a leading slash is not doubled"
    sent = {call[2]["headers"]["x-access-tokens"] for call in session.calls}
    assert len(sent) == 1, "one token for both calls"
    claims = jwt.decode(sent.pop(), SHARED_SECRET, algorithms=["HS256"])
    assert claims["id"] == 0


def test_a_refused_luna_token_is_signed_again_once(rig):
    """
    A 401 drops the token, signs a fresh one and repeats the request once.
    """
    client, session = _luna(Answer(401), Answer(body={"ok": True}))
    assert client.get("config/node") == {"ok": True}
    assert len(session.calls) == 2


def test_luna_answers_are_decoded_by_status(rig):
    """
    404 is nothing there, an empty body is None, text stays text, 500 is unavailable.
    """
    client, _ = _luna(Answer(404), Answer(200), Answer(200, text="plain words"), Answer(500))
    assert client.get("config/node/x") is None
    assert client.get("config/empty") is None
    assert client.get("config/text") == "plain words"
    with pytest.raises(ServiceUnavailable, match="answered 500"):
        client.get("config/broken")


def test_luna_being_away_is_a_503_not_a_traceback(rig, tmp_path, monkeypatch):
    """
    A refused connection, a timeout, and a luna.ini with no secret are all unavailable.
    """
    client, _ = _luna(requests.ConnectionError("refused"))
    with pytest.raises(ServiceUnavailable, match="not answering"):
        client.get("config/node")

    client, _ = _luna(requests.Timeout("slow"))
    with pytest.raises(ServiceUnavailable, match="not answering"):
        client.post("config/node/x", {"a": 1})

    bare = tmp_path / "bare-luna.ini"
    bare.write_text("[API]\nENDPOINT = luna.test:7050\n", encoding="utf-8")
    monkeypatch.setenv("TRIXD_LUNA_INI", str(bare))
    client, _ = _luna(Answer(body={}))
    with pytest.raises(ServiceUnavailable, match="no token for Luna"):
        client.get("config/node")


def test_lunas_envelopes_are_unwrapped(rig):
    """
    Each helper answers the objects themselves, and an empty answer is an empty dict.
    """
    client, _ = _luna(
        Answer(body={"config": {"node": {"node001": {"group": "compute"}}}}),
        Answer(body={"config": {"node": {"node001": {"group": "compute"}}}}),
        Answer(body={"config": {"cluster": {"security": True}}}),
        Answer(body={"config": {"group": {"compute": {}}}}),
        Answer(body={"config": {"network": {"cluster": {}}}}),
        Answer(body={"table": {"hashes": {"node": "abc"}}}),
        Answer(404))
    assert client.nodes() == {"node001": {"group": "compute"}}
    assert client.node("node001") == {"group": "compute"}
    assert client.enforcing_security() is True
    assert client.groups() == {"compute": {}}
    assert client.networks() == {"cluster": {}}
    assert client.table_hashes() == {"node": "abc"}
    assert client.nodes() == {}


def test_trixd_follows_luna_ini_when_it_changes(rig, tmp_path, monkeypatch):
    """
    A protocol, address or secret changed in luna.ini is what trixd uses next; an empty
    secret, as a fresh Luna install ships, does not replace a good one.
    """
    import os
    import time

    from trixd.plugins.luna import config as luna_config

    path = tmp_path / "moving-luna.ini"
    path.write_text("[API]\nENDPOINT = one:7050\nPROTOCOL = https\nSECRET_KEY = " + "a" * 32
                    + "\n", encoding="utf-8")
    monkeypatch.setenv("TRIXD_LUNA_INI", str(path))
    config = luna_config.current()
    assert (config.base, config.secret) == ("https://one:7050", "a" * 32)

    path.write_text("[API]\nENDPOINT = two:7050\nPROTOCOL = http\nSECRET_KEY = " + "b" * 32
                    + "\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))
    assert (config.base, config.secret) == ("http://two:7050", "b" * 32)

    path.write_text("[API]\nENDPOINT = two:7050\nPROTOCOL = gopher\nSECRET_KEY =\n",
                    encoding="utf-8")
    os.utime(path, (later + 5, later + 5))
    assert config.protocol == "https", "an unknown protocol falls back to https"
    assert config.secret == "b" * 32, "an empty secret never replaces a good one"


class FakeLuna:
    """
    Luna's table hashes, from a script.
    """

    def __init__(self, *answers) -> None:
        """
        One answer per pass; an exception is raised instead.
        """
        self.answers = list(answers)

    def table_hashes(self):
        """
        The next answer.
        """
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeRenders:
    """
    The render engine, reduced to what it was asked to wake.
    """

    def __init__(self) -> None:
        """
        Nothing woken yet.
        """
        self.woken = []

    def notify(self, item: str) -> list:
        """
        Remember the input and answer one render name for it.
        """
        self.woken.append(item)
        return [f"render-for-{item}"]


def _hashes(monkeypatch, *answers) -> tuple:
    """
    A fresh poller wired to a scripted Luna and a recording engine.
    """
    luna, renders = FakeLuna(*answers), FakeRenders()
    monkeypatch.setattr(hashes, "luna", lambda: luna)
    monkeypatch.setattr(hashes, "renders", lambda: renders)
    return hashes.LunaHashes(), renders


def test_the_first_hash_reading_is_a_baseline_and_wakes_nothing(rig, monkeypatch):
    """
    Then only the tables whose hash moved wake the renders that read them.
    """
    poller, renders = _hashes(monkeypatch, {"node": "1", "group": "1"},
                              {"node": "2", "group": "1"}, {"node": "2", "group": "1"})
    assert poller.once() == []
    assert renders.woken == []
    assert poller.once() == ["render-for-luna.node"]
    assert poller.once() == []
    assert renders.woken == ["luna.node"]
    assert poller.state()["tables_seen"] == 2
    assert poller.state()["answering"] is True


def test_an_empty_hash_answer_is_not_nothing_changed(rig, monkeypatch):
    """
    Luna answers the hashes on its HA master only; empty leaves the baseline alone.
    """
    poller, renders = _hashes(monkeypatch, {"node": "1"}, {}, {"node": "2"})
    poller.once()
    assert poller.once() == []
    assert poller.state()["answering"] is False
    assert poller.once() == ["render-for-luna.node"], "the change across the gap still counts"


def test_luna_failing_a_hash_pass_is_not_fatal(rig, monkeypatch):
    """
    The pass is skipped and the next one carries on.
    """
    poller, _ = _hashes(monkeypatch, requests.ConnectionError("away"), {"node": "1"})
    assert poller.once() == []
    assert poller.once() == []
    assert poller.seen == {"node": "1"}


def test_the_hash_baseline_and_poller_follow_the_interval(rig, monkeypatch):
    """
    0s switches both off; a positive interval takes a baseline and starts the thread.
    """
    poller, _ = _hashes(monkeypatch, {"node": "1"})
    assert configuration.current().triggers.luna_hashes == 0
    poller.baseline()
    assert poller.seen == {}
    assert poller.start() is False

    monkeypatch.setattr(configuration.current().triggers, "luna_hashes", 3600)
    poller.baseline()
    assert poller.seen == {"node": "1"}
    assert poller.start() is True
    poller.shutdown()
    poller.thread.join(timeout=5)
    assert not poller.thread.is_alive()


def test_a_peer_is_trusted_through_the_cluster_ca_only_when_it_is_there(rig, tmp_path,
                                                                     monkeypatch):
    """
    The CA file when present; otherwise the system store, which fails every pass.
    """
    sweep = replication.Sweep()
    monkeypatch.setattr(configuration.current().replication, "ca_file",
                        str(tmp_path / "missing-ca.crt"))
    assert sweep._trust() is True
    ca = tmp_path / "cluster-ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(configuration.current().replication, "ca_file", str(ca))
    assert sweep._trust() == str(ca)


def test_the_service_token_goes_to_a_peer_with_the_ca_check(rig, tmp_path, monkeypatch):
    """
    Over https, with the token header, verified against the cluster CA.
    """
    ca = tmp_path / "cluster-ca.crt"
    ca.write_text("x", encoding="utf-8")
    monkeypatch.setattr(configuration.current().replication, "ca_file", str(ca))
    seen = {}

    def get(url, **kwargs):
        """
        Record the call and answer.
        """
        seen.update(url=url, **kwargs)
        return Answer(body={"hashes": {}})

    monkeypatch.setattr(replication.requests, "get", get)
    assert replication.Sweep()._get("peer.cluster:8050", "hashes") == {"hashes": {}}
    assert seen["url"] == "https://peer.cluster:8050/api/v1/system/replication/hashes"
    assert seen["verify"] == str(ca)
    assert seen["headers"]["x-access-tokens"]

    monkeypatch.setattr(replication.requests, "get", lambda url, **kwargs: Answer(403))
    with pytest.raises(RuntimeError, match="answered 403"):
        replication.Sweep()._get("peer.cluster:8050", "hashes")


def test_a_pass_takes_what_differs_from_the_peer_and_then_nothing(rig, tmp_path, monkeypatch):
    """
    The hashes first, then only the tables that differ, merged in.
    """
    peer = _peer(tmp_path)
    peer.add(models.Principal(name="alice", role="manager", origin="peer"))
    peer.commit()

    def from_peer(self, address, path):
        """
        The peer's replication routes, answered from its database.
        """
        if path == "hashes":
            return {"hashes": tables.hashes(peer)}
        return {"documents": tables.export_table(peer, path.split("/", 1)[1])}

    monkeypatch.setattr(replication.Sweep, "_get", from_peer)
    sweep = replication.Sweep()
    try:
        first = sweep.pass_over("peer.cluster:8050")
        assert first["tables"] == ["principal"]
        assert first["changes"]["principal"]["added"] == 1
        assert sweep.pass_over("peer.cluster:8050")["tables"] == []
    finally:
        peer.close()
    handle = database.session()
    try:
        assert handle.query(models.Principal).filter_by(name="alice").one().role == "manager"
    finally:
        handle.close()


def test_a_peer_that_is_away_is_reported_and_skipped(rig, monkeypatch):
    """
    The pass records the error for that peer and the daemon carries on.
    """
    monkeypatch.setattr(configuration.current().replication, "peers", ["away.cluster:8050"])

    def away(self, address, path):
        """
        A peer that does not answer.
        """
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(replication.Sweep, "_get", away)
    sweep = replication.Sweep()
    results = sweep.once()
    assert results == [{"peer": "away.cluster:8050", "error": "no route to host"}]
    assert sweep.last["results"] == results


def test_the_sweep_runs_only_with_peers_and_stops_when_asked(rig, monkeypatch):
    """
    No peers, no thread; with one, a thread that ends on shutdown.
    """
    sweep = replication.Sweep()
    assert sweep.start() is False
    monkeypatch.setattr(configuration.current().replication, "peers", ["peer.cluster:8050"])
    monkeypatch.setattr(configuration.current().replication, "interval", 3600)
    assert sweep.start() is True
    sweep.shutdown()
    sweep.thread.join(timeout=5)
    assert not sweep.thread.is_alive()


def _pam(monkeypatch, accounts: dict, password: str = "right"):
    """
    The PAM source on a stand-in passwd database and PAM stack.
    """
    from trixd.core.security.sources import pam as pam_source

    def getpwnam(name):
        """
        The passwd entry, or KeyError as pwd raises.
        """
        if name not in accounts:
            raise KeyError(name)
        uid, shell = accounts[name]
        return SimpleNamespace(pw_name=name, pw_uid=uid, pw_shell=shell)

    services = []

    class Conversation:
        """
        One PAM conversation that accepts a single password.
        """

        reason = "Authentication failure"

        def authenticate(self, name, given, service="login"):
            """
            Remember the service asked for, and check the password.
            """
            services.append(service)
            return given == password

    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=getpwnam))
    monkeypatch.setitem(sys.modules, "pam", SimpleNamespace(pam=Conversation))
    monkeypatch.setattr(pam_source.Source, "_uid_min", lambda self: 1000)
    return pam_source.Source(), services


def test_pam_signs_in_an_ordinary_account_with_its_password(rig, monkeypatch):
    """
    The right password, through the configured PAM service.
    """
    source, services = _pam(monkeypatch, {"carol": (1500, "/bin/bash")})
    assert source.authenticate("carol", "right") == (True, {"name": "carol"})
    assert services == [configuration.current().auth.pam_service]
    verdict, said = source.authenticate("carol", "wrong")
    assert verdict is False and "PAM refused carol" in said


def test_pam_knows_nobody_the_name_service_does_not(rig, monkeypatch):
    """
    Not known is None, so the chain asks the next source.
    """
    source, services = _pam(monkeypatch, {})
    verdict, _ = source.authenticate("ghost", "right")
    assert verdict is None
    assert services == [], "PAM was not even asked"
    assert source.resolve("ghost")[0] is False


def test_pam_resolves_a_person_and_not_a_service_account(rig, monkeypatch):
    """
    A low uid or a shell that cannot log in is a system account.
    """
    source, _ = _pam(monkeypatch, {"carol": (1500, "/bin/bash"), "named": (25, "/bin/bash"),
                                   "backup": (1600, "/usr/sbin/nologin")})
    assert source.resolve("carol") == (True, {"name": "carol"})
    assert source.resolve("named")[0] is False
    assert source.resolve("backup")[0] is False


def _ldap(monkeypatch):
    """
    The LDAP source against ldap3's own offline directory, with one person and a reader.
    """
    import ldap3

    from trixd.core.security.sources import ldap as ldap_source

    auth = configuration.current().auth
    monkeypatch.setattr(auth, "ldap_uri", "ldaps://directory.cluster")
    monkeypatch.setattr(auth, "ldap_base", "ou=People,dc=cluster")
    monkeypatch.setattr(auth, "ldap_bind_dn", "cn=reader,dc=cluster")
    monkeypatch.setattr(auth, "ldap_bind_password", "readpw")

    server = ldap3.Server("directory.cluster")
    setup = ldap3.Connection(server, client_strategy=ldap3.MOCK_SYNC)
    setup.strategy.add_entry("cn=reader,dc=cluster", {"cn": "reader", "userPassword": "readpw"})
    setup.strategy.add_entry("uid=carol,ou=People,dc=cluster",
                             {"uid": "carol", "userPassword": "right",
                              "objectClass": ["inetOrgPerson"]})

    stand_in = ModuleType("ldap3")
    stand_in.NONE = ldap3.NONE
    stand_in.utils = ldap3.utils
    stand_in.core = ldap3.core
    stand_in.Server = lambda uri, get_info=None: server
    stand_in.Connection = lambda srv, **kwargs: ldap3.Connection(
        srv, client_strategy=ldap3.MOCK_SYNC, **kwargs)

    source = ldap_source.Source()
    source.ldap3 = stand_in
    return source


def test_ldap_signs_in_by_the_persons_own_bind(rig, monkeypatch):
    """
    Found by the reader's search, then checked by binding as the person.
    """
    source = _ldap(monkeypatch)
    assert source.authenticate("carol", "right") == (True, {"name": "carol"})
    verdict, said = source.authenticate("carol", "wrong")
    assert verdict is False and "refused the password" in said
    assert source.authenticate("carol", "")[0] is False, "an empty password is no bind"


def test_ldap_knows_nobody_outside_the_directory(rig, monkeypatch):
    """
    Not in the directory is not known, and a wildcard is searched for as a character.
    """
    source = _ldap(monkeypatch)
    assert source.authenticate("ghost", "right")[0] is None
    assert source.authenticate("*", "right")[0] is None, "the wildcard matched somebody"
    assert source.resolve("carol") == (True, {"name": "carol"})
    assert source.resolve("ghost")[0] is False


def test_ldap_without_a_uri_or_base_refuses_to_start(rig):
    """
    The chain skips a source that cannot work; this is how it learns that.
    """
    from trixd.core.security.sources import ldap as ldap_source

    with pytest.raises(ValueError, match="URI and BASE"):
        ldap_source.Source()


def _obol(monkeypatch, existing: set) -> list:
    """
    obol, standing in: show answers for the names in existing, add and delete change
    it, and every call is recorded.
    """
    from trixd.plugins.directory import backend as directory

    calls = []

    def run(argv, *args, **kwargs):
        """
        Record the call; show fails for a name that does not exist.
        """
        calls.append(list(argv))
        verb = argv[2] if argv[1] != "-J" else argv[3]
        name = argv[3] if argv[1] != "-J" else argv[4]
        if verb == "show" and name not in existing:
            return Ran("", 1, "not found")
        if verb == "add":
            existing.add(name)
        if verb == "delete":
            existing.discard(name)
        return Ran("{}")

    monkeypatch.setattr(directory.shell, "run", run)
    monkeypatch.setattr(directory.shell, "run_json", lambda argv, *a, **k: {"uid": argv[-1]})
    return calls


def test_a_new_user_reaches_obol_as_flags(rig, monkeypatch):
    """
    Only the fields sent, a list joined with commas, and the name from the body.
    """
    from trixd.plugins.directory.backend import Backend

    calls = _obol(monkeypatch, set())
    Backend().create_user({"username": "dave", "uid": 1600, "groups": ["hpc", "gpu"],
                           "shell": None})
    added = [call for call in calls if "add" in call][0]
    assert added == ["obol", "user", "add", "dave", "--uid", "1600", "--groups", "hpc,gpu"]


def test_the_directory_refuses_what_obol_would_get_wrong(rig, monkeypatch):
    """
    No name, a name taken, an empty change and a missing record are all refused first.
    """
    from trixd.plugins.directory.backend import Backend

    _obol(monkeypatch, {"carol", "hpc"})
    with pytest.raises(BadRequest, match="username is required"):
        Backend().create_user({})
    with pytest.raises(BadRequest, match="already exists"):
        Backend().create_user({"username": "carol"})
    with pytest.raises(BadRequest, match="nothing to change"):
        Backend().update_user("carol", {"unknown": "x"})
    with pytest.raises(NotFound):
        Backend().delete_user("ghost")
    with pytest.raises(BadRequest, match="groupname is required"):
        Backend().create_group({})
    with pytest.raises(BadRequest, match="already exists"):
        Backend().create_group({"groupname": "hpc"})
    with pytest.raises(NotFound):
        Backend().update_group("ghost", {"gid": 5})
    with pytest.raises(NotFound):
        Backend().delete_group("ghost")


def test_the_directory_changes_and_removes_what_exists(rig, monkeypatch):
    """
    modify with the changed fields, delete by name, and the group's members as users.
    """
    from trixd.plugins.directory.backend import Backend

    calls = _obol(monkeypatch, {"carol", "hpc", "dave"})
    Backend().update_user("carol", {"shell": "/bin/zsh"})
    Backend().delete_user("carol")
    Backend().create_group({"groupname": "gpu", "members": ["carol", "dave"]})
    Backend().update_group("hpc", {"gid": 5000})
    assert Backend().delete_group("hpc") == {"message": "group hpc deleted"}
    writes = [call for call in calls if call[2] in ("add", "modify", "delete")]
    assert writes == [
        ["obol", "user", "modify", "carol", "--shell", "/bin/zsh"],
        ["obol", "user", "delete", "carol"],
        ["obol", "group", "add", "gpu", "--users", "carol,dave"],
        ["obol", "group", "modify", "hpc", "--gid", "5000"],
        ["obol", "group", "delete", "hpc"],
    ]


def _slurm(monkeypatch, nodes: dict, wrote: bool = True, reconfigured: int = 0) -> dict:
    """
    The slurm plugin on a stand-in generator package, Luna and scontrol.
    """
    from trixd.plugins.slurm import backend as slurm

    seen = {}

    class Generate:
        """
        trinityx_config_slurm's entry point, recording what it was given.
        """

        def all_configs(self, nodes, configs, manager):
            """
            Remember the call and report success or failure.
            """
            seen.update(nodes=nodes, configs=configs, manager=manager)
            return wrote

    package = ModuleType("trinityx_config_slurm")
    package.Generate = Generate
    monkeypatch.setitem(sys.modules, "trinityx_config_slurm", package)
    monkeypatch.setattr(slurm, "luna", lambda: SimpleNamespace(nodes=lambda: nodes))
    monkeypatch.setattr(slurm.shell, "run", lambda argv, **kwargs: (
        seen.setdefault("ran", argv), Ran("", reconfigured, "boom"))[1])
    return seen


def test_slurm_renders_luna_nodes_through_the_generator(rig, monkeypatch):
    """
    Sorted node and group pairs, the three files, then scontrol reconfigure.
    """
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, {"node002": {"group": "gpu"}, "node001": {"group": None}})
    answer = Backend().render_config()
    assert seen["nodes"] == [{"name": "node001", "group": ""}, {"name": "node002", "group": "gpu"}]
    assert set(seen["configs"]) == {"nodes", "partitions", "gres"}
    assert seen["manager"] == "TrinityX"
    assert seen["ran"] == ["/usr/bin/scontrol", "reconfigure"]
    assert answer["nodes"] == 2 and answer["reconfigured"] is True


def test_slurm_writes_nothing_without_nodes_and_says_when_it_failed(rig, monkeypatch):
    """
    No nodes is not an error; a failing generator is; a failing reconfigure is reported.
    """
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, {})
    assert Backend().render_config() == {"nodes": 0, "files": []}
    assert "nodes" not in seen, "the generator ran with no nodes"

    _slurm(monkeypatch, {"node001": {"group": "a"}}, wrote=False)
    with pytest.raises(BackendFailure, match="generator reported a failure"):
        Backend().render_config()

    _slurm(monkeypatch, {"node001": {"group": "a"}}, reconfigured=1)
    assert Backend().render_config()["reconfigured"] is False


def test_slurm_without_its_generator_says_so(rig, monkeypatch):
    """
    A controller without slurm's package gets a named failure, not an ImportError.
    """
    from trixd.plugins.slurm import backend as slurm

    monkeypatch.setitem(sys.modules, "trinityx_config_slurm", None)
    monkeypatch.setattr(slurm, "luna", lambda: SimpleNamespace(nodes=lambda: {"n": {}}))
    with pytest.raises(BackendFailure, match="not installed on this controller"):
        slurm.Backend().render_config()


def _gated() -> "TestClient":
    """
    Three routes behind the gate alone: one for a node token, two for user roles.
    """
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from trixd.common import errors
    from trixd.core.security.gate import guard

    application = FastAPI()
    errors.register(application)

    @application.patch("/hw-rules/{node}")
    def own_rules(node: str, caller=Depends(guard(token_kind="node"))) -> dict:
        """
        What a node's hook calls for itself.
        """
        return {"node": node, "caller": caller.name}

    @application.get("/look")
    def look(caller=Depends(guard("none"))) -> dict:
        """
        Anyone signed in.
        """
        return {"caller": caller.name}

    @application.patch("/change")
    def change(caller=Depends(guard("manager"))) -> dict:
        """
        A manager or above.
        """
        return {"caller": caller.name}

    return TestClient(application)


def test_a_node_token_is_good_for_its_own_node_only(rig):
    """
    node001's token opens node001's route, by short name or FQDN, and no other node's.
    """
    from trixd.core.security import tokens

    client = _gated()
    own = {"x-access-tokens": tokens.mint("node001", kind="node", node="node001")}
    assert client.patch("/hw-rules/node001", headers=own).status_code == 200
    assert client.patch("/hw-rules/node001.cluster", headers=own).status_code == 200
    refused = client.patch("/hw-rules/node002", headers=own)
    assert refused.status_code == 403
    assert "not valid for this node" in refused.text


def test_a_node_route_takes_no_user_token_and_a_user_route_no_node_token(rig, token):
    """
    The two kinds never stand in for each other, rootus included.
    """
    from trixd.core.security import tokens

    client = _gated()
    as_rootus = client.patch("/hw-rules/node001", headers={"x-access-tokens": token})
    assert as_rootus.status_code == 403 and "node token only" in as_rootus.text
    node = {"x-access-tokens": tokens.mint("node001", kind="node", node="node001")}
    assert client.get("/look", headers=node).status_code == 403


def test_the_gate_reads_either_header_and_refuses_no_token(rig, token):
    """
    x-access-tokens or Authorization: Bearer; nothing, or nonsense, is 401.
    """
    client = _gated()
    assert client.get("/look", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/look", headers={"Authorization": f"Basic {token}"}).status_code == 401
    assert client.get("/look").status_code == 401
    assert client.get("/look", headers={"x-access-tokens": "not.a.token"}).status_code == 401


def test_a_subject_nobody_knows_is_refused_and_the_peer_is_not(rig):
    """
    A signed token for a name no source resolves is 401; the peer's service token,
    which names no user, passes a manager route.
    """
    from trixd.core.security import tokens

    client = _gated()
    ghost = {"x-access-tokens": tokens.mint("ghost-of-nobody")}
    assert client.get("/look", headers=ghost).status_code == 401
    peer = {"x-access-tokens": tokens.service_token()}
    answer = client.patch("/change", headers=peer)
    assert answer.status_code == 200, answer.text


def test_a_services_state_is_what_systemd_says(rig, monkeypatch):
    """
    is-active and is-enabled for each catalogued unit, and unknown when they are silent.
    """
    from trixd.plugins.services import backend as services

    def systemctl(argv, **kwargs):
        """
        dirsrv@local is running and enabled; anything else says nothing.
        """
        if argv[-1] == "dirsrv@local":
            return Ran("active" if argv[1] == "is-active" else "enabled")
        return Ran("", 3)

    monkeypatch.setattr(services.shell, "run", systemctl)
    listed = services.Backend().list_services()["services"]
    assert listed["dirsrv"] == {"active": "active", "enabled": "enabled"}
    assert listed["slurmctld"] == {"active": "unknown", "enabled": "unknown"}
    assert services.Backend().act("dirsrv", "STATUS")["unit"] == "dirsrv@local"
    with pytest.raises(NotFound):
        services.Backend().status("sshd")


def test_a_queued_service_action_runs_the_configured_command(rig, monkeypatch):
    """
    The worker runs COMMAND action unit, and a failure carries what the command said.
    """
    from trixd.plugins.services import backend as services

    ran = []
    monkeypatch.setattr(services.shell, "run",
                        lambda argv, **kwargs: (ran.append(argv), Ran("", 0))[1])
    assert services.run_task("restart", "slurmctld") == "restart slurmctld accepted"
    assert ran == [["/usr/bin/systemctl", "restart", "slurmctld"]]
    monkeypatch.setattr(services.shell, "run", lambda argv, **kwargs: Ran("", 1, "unit masked"))
    with pytest.raises(BackendFailure, match="unit masked"):
        services.run_task("start", "slurmctld")


def test_genders_says_why_it_wrote_nothing(rig, monkeypatch):
    """
    No nodes is nothing to do; no managed block, or no package, is a named failure.
    """
    from trixd.plugins.genders import backend as genders

    monkeypatch.setattr("trixd.plugins.slurm.backend.luna",
                        lambda: SimpleNamespace(nodes=lambda: {}))
    assert genders.Backend().render_genders() == {"nodes": 0, "file": "/etc/genders"}

    monkeypatch.setattr("trixd.plugins.slurm.backend.luna",
                        lambda: SimpleNamespace(nodes=lambda: {"node001": {"group": "a"}}))
    package = ModuleType("trinityx_config_genders")
    package.Generate = lambda: SimpleNamespace(Genders=lambda nodes: False)
    monkeypatch.setitem(sys.modules, "trinityx_config_genders", package)
    with pytest.raises(BackendFailure, match="no TrinityX managed block"):
        genders.Backend().render_genders()

    monkeypatch.setitem(sys.modules, "trinityx_config_genders", None)
    with pytest.raises(BackendFailure, match="not installed on this controller"):
        genders.Backend().render_genders()
