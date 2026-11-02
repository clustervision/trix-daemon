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
import os
import stat
import sys
from types import ModuleType, SimpleNamespace

import jwt
import pytest
import requests
from fastapi.testclient import TestClient

from tests.unit.test_plugins import Ran
from tests.unit.test_replication import _peer
from trixd.common import settings as configuration
from trixd.common.errors import (BackendFailure, BadRequest, NotFound, ServiceUnavailable,
                                 Unauthenticated)
from trixd.core.replication import sweep as replication
from trixd.core.replication import tables
from trixd.db import engine as database
from trixd.db import models
from trixd.plugins.luna import config as luna_config
from trixd.plugins.luna import credentials, hashes
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


@pytest.fixture(autouse=True)
def _no_token_held():
    """
    The token is held for the process, so one test must not answer the next one's call.
    """
    credentials._held, credentials._held_for = "", ()
    yield
    credentials._held, credentials._held_for = "", ()


def _luna_ini(tmp_path, **api) -> str:
    """
    A luna.ini carrying only what the test puts in its [API] section.
    """
    path = tmp_path / "luna.ini"
    body = "".join(f"{key.upper()} = {value}\n" for key, value in api.items())
    path.write_text(f"[API]\nENDPOINT = luna.test:7050\nPROTOCOL = https\n{body}",
                    encoding="utf-8")
    return str(path)


def _cached(tmp_path, monkeypatch, token: str) -> str:
    """
    A token already in the cache file, with the cache pointed at the scratch directory.
    """
    path = tmp_path / "cache" / "token"
    path.parent.mkdir(exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setenv(credentials.TOKEN_FILE_ENV, str(path))
    return str(path)


def _signed(secret: str, seconds: int = 600) -> str:
    """
    A token that looks like one Luna issued, expiring when the test says.
    """
    from datetime import datetime, timedelta, timezone

    return jwt.encode({"id": 0, "exp": datetime.now(timezone.utc) + timedelta(seconds=seconds)},
                      secret, algorithm="HS256")


def test_the_luna_credentials_come_from_lunas_own_ini(rig, tmp_path, monkeypatch):
    """
    The account is read from Luna's file, which is the only place it is written.
    """
    monkeypatch.setenv("TRIXD_LUNA_INI", _luna_ini(tmp_path, username="luna", password="s3cret"))
    assert credentials.get_credentials() == ("luna", "s3cret")


def test_an_ini_with_no_account_says_which_file_and_which_keys(rig, tmp_path, monkeypatch):
    """
    The refusal names the file to edit, because that is the whole of the fix.
    """
    path = _luna_ini(tmp_path)
    monkeypatch.setenv("TRIXD_LUNA_INI", path)
    with pytest.raises(Unauthenticated, match=r"has no \[API\] USERNAME and PASSWORD"):
        credentials.get_credentials()


def test_the_cached_token_is_used_before_anything_is_minted(rig, tmp_path, monkeypatch):
    """
    A usable token on disk costs neither a signature nor a request.
    """
    from tests.conftest import SHARED_SECRET

    monkeypatch.setenv("TRIXD_LUNA_INI",
                       _luna_ini(tmp_path, username="luna", password="p", secret_key=SHARED_SECRET))
    kept = _signed(SHARED_SECRET)
    _cached(tmp_path, monkeypatch, kept)
    assert credentials.get_token() == kept


def test_an_expired_or_corrupt_cached_token_is_ignored(rig, tmp_path, monkeypatch):
    """
    Neither is an error: the token is minted again, and the bad one is replaced.
    """
    from tests.conftest import SHARED_SECRET

    monkeypatch.setenv("TRIXD_LUNA_INI",
                       _luna_ini(tmp_path, username="luna", password="p", secret_key=SHARED_SECRET))
    for useless in (_signed(SHARED_SECRET, seconds=-10), "not a token at all"):
        _cached(tmp_path, monkeypatch, useless)
        credentials._held, credentials._held_for = "", ()
        fresh = credentials.get_token()
        assert fresh != useless
        assert jwt.decode(fresh, SHARED_SECRET, algorithms=["HS256"])["id"] == 0


def test_the_secret_is_preferred_over_signing_in(rig, tmp_path, monkeypatch):
    """
    Signing costs nothing and a sign-in costs a request, so a secret wins and Luna is
    never asked.
    """
    from tests.conftest import SHARED_SECRET

    monkeypatch.setenv("TRIXD_LUNA_INI",
                       _luna_ini(tmp_path, username="luna", password="p", secret_key=SHARED_SECRET))
    _cached(tmp_path, monkeypatch, "")
    asked = []
    monkeypatch.setattr(credentials.requests, "post",
                        lambda *args, **kwargs: asked.append(args) or Answer(body={}))
    token = credentials.get_token()
    assert asked == [], "Luna was asked for a token although luna.ini holds the secret"
    assert jwt.decode(token, SHARED_SECRET, algorithms=["HS256"])["id"] == 0


def test_with_no_secret_trixd_signs_in_and_keeps_what_it_got(rig, tmp_path, monkeypatch):
    """
    The last resort: the account from luna.ini, one POST to Luna, and the answer cached
    so a restart does not ask again. The file is the owner's alone.
    """
    monkeypatch.setenv("TRIXD_LUNA_INI", _luna_ini(tmp_path, username="luna", password="p"))
    # Not created here: the directory trixd makes itself is the one it promises 0700 for.
    path = str(tmp_path / "fresh" / "token")
    monkeypatch.setenv(credentials.TOKEN_FILE_ENV, path)
    issued = _signed("l" * 32)
    sent = {}
    monkeypatch.setattr(credentials.requests, "post", lambda url, **kwargs: (
        sent.update(url=url, json=kwargs.get("json")), Answer(201, body={"token": issued}))[1])

    assert credentials.get_token() == issued
    assert sent["url"].endswith("/token")
    assert sent["json"] == {"username": "luna", "password": "p"}
    with open(path, encoding="utf-8") as handle:
        assert handle.read().strip() == issued
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


def test_the_token_cache_tightens_a_directory_that_was_already_there(rig, tmp_path,
                                                                     monkeypatch):
    """
    The test above lets trixd create the directory, and says so - that was the only case the
    0700 promise held for. makedirs applies its mode when it creates and not otherwise, and
    the Luna command line creates this directory first on most boxes, so in practice the
    0600 file sat in whatever permissions were already there.
    """
    monkeypatch.setenv("TRIXD_LUNA_INI", _luna_ini(tmp_path, username="luna", password="p"))
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o777)
    path = str(loose / "token")
    monkeypatch.setenv(credentials.TOKEN_FILE_ENV, path)
    issued = _signed("l" * 32)
    monkeypatch.setattr(credentials.requests, "post",
                        lambda url, **kwargs: Answer(201, body={"token": issued}))

    assert credentials.get_token() == issued
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(loose).st_mode) == 0o700, \
            "a directory that already existed was left as it was found"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_luna_away_with_nothing_held_is_unavailable_not_a_wrong_token(rig, tmp_path, monkeypatch):
    """
    A render answers 503 and records the failure; it never renders from a token it
    could not get.
    """
    monkeypatch.setenv("TRIXD_LUNA_INI", _luna_ini(tmp_path, username="luna", password="p"))
    monkeypatch.setenv(credentials.TOKEN_FILE_ENV, str(tmp_path / "absent" / "token"))
    monkeypatch.setattr(credentials.requests, "post",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            requests.ConnectionError("refused")))
    with pytest.raises(ServiceUnavailable, match="not answering"):
        credentials.get_token()


def test_a_luna_ini_naming_another_luna_drops_the_held_token(rig, tmp_path, monkeypatch):
    """
    A token for one Luna is the wrong token for another, however long it has left.
    """
    from tests.conftest import SHARED_SECRET

    monkeypatch.setenv("TRIXD_LUNA_INI",
                       _luna_ini(tmp_path, username="luna", password="p", secret_key=SHARED_SECRET))
    _cached(tmp_path, monkeypatch, "")
    first = credentials.get_token()

    other = tmp_path / "other.ini"
    other.write_text(f"[API]\nENDPOINT = elsewhere:7050\nPROTOCOL = https\n"
                     f"SECRET_KEY = {'f' * 32}\n", encoding="utf-8")
    monkeypatch.setenv("TRIXD_LUNA_INI", str(other))
    assert credentials.get_token() != first


def test_the_personal_luna_ini_is_read_before_the_controllers(tmp_path, monkeypatch):
    """
    The order luna2-cli uses: whoever runs this may have logged in as themselves.
    """
    monkeypatch.delenv("TRIXD_LUNA_INI", raising=False)
    home = tmp_path / "home"
    (home / ".luna").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert luna_config.chosen_path() == luna_config.DEFAULT_LUNA_INI, "none yet, so the controller's"

    personal = home / ".luna" / "luna.ini"
    personal.write_text("[API]\nENDPOINT = mine:7050\n", encoding="utf-8")
    assert luna_config.chosen_path() == str(personal)
    monkeypatch.setenv("TRIXD_LUNA_INI", "/named/explicitly.ini")
    assert luna_config.chosen_path() == "/named/explicitly.ini", "the environment still wins"


def test_a_token_for_another_account_is_signed_in_for_and_never_cached(rig, tmp_path,
                                                                       monkeypatch):
    """
    The cache holds the daemon's own token. A token for somebody else must not land in
    it, or the next caller asking for the daemon's gets a stranger's.
    """
    from tests.conftest import SHARED_SECRET

    monkeypatch.setenv("TRIXD_LUNA_INI",
                       _luna_ini(tmp_path, username="luna", password="p", secret_key=SHARED_SECRET))
    path = str(tmp_path / "own" / "token")
    monkeypatch.setenv(credentials.TOKEN_FILE_ENV, path)
    mine = credentials.get_token()

    theirs = _signed("x" * 32)
    sent = {}
    monkeypatch.setattr(credentials.requests, "post", lambda url, **kwargs: (
        sent.update(json=kwargs.get("json")), Answer(201, body={"token": theirs}))[1])

    assert credentials.get_token_for("alice", "her-password") == theirs
    assert sent["json"] == {"username": "alice", "password": "her-password"}
    assert credentials.get_token() == mine, "the held token is still the daemon's own"
    assert not os.path.exists(path), "a token for another account was written to the cache"


def test_a_token_for_another_account_needs_both_halves(rig, tmp_path, monkeypatch):
    """
    Signing in with half an account is a refusal here, not a request to Luna.
    """
    monkeypatch.setenv("TRIXD_LUNA_INI", _luna_ini(tmp_path, username="luna", password="p"))
    asked = []
    monkeypatch.setattr(credentials.requests, "post",
                        lambda *args, **kwargs: asked.append(args) or Answer(body={}))
    for username, password in (("", "p"), ("alice", "")):
        with pytest.raises(Unauthenticated, match="username and a password"):
            credentials.get_token_for(username, password)
    assert asked == [], "Luna was asked although the account was incomplete"


def test_a_refused_account_is_unavailable_and_names_the_file(rig, tmp_path, monkeypatch):
    """
    Luna saying no is not this daemon's failure, but the caller still cannot proceed.
    """
    monkeypatch.setenv("TRIXD_LUNA_INI", _luna_ini(tmp_path, username="luna", password="p"))
    monkeypatch.setattr(credentials.requests, "post", lambda *args, **kwargs: Answer(401))
    with pytest.raises(ServiceUnavailable, match="refused the account"):
        credentials.get_token_for("alice", "wrong")


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


def test_an_unreadable_cluster_answer_means_security_is_enforced(rig):
    """
    The flag decides whether a node has to present a measurement, so an answer that cannot
    be read must not read as "off". A 404, an empty body and a row whose shape has moved all
    arrive as an empty row; before this they turned attestation off cluster-wide and said
    nothing, while a transport failure - the louder case - already raised instead.
    """
    client, _ = _luna(
        Answer(body={"config": {"cluster": {"security": False}}}),
        Answer(body={"config": {"cluster": {"security": True}}}),
        Answer(404),
        Answer(body={}),
        Answer(body={"config": {"cluster": {"name": "trinity"}}}))

    assert client.enforcing_security() is False, "Luna said off and meant it"
    assert client.enforcing_security() is True, "Luna said on"
    assert client.enforcing_security() is True, "a 404 read as not enforcing"
    assert client.enforcing_security() is True, "an empty body read as not enforcing"
    assert client.enforcing_security() is True, "a row with no flag read as not enforcing"


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
        self.retries = 0

    def notify(self, item: str) -> list:
        """
        Remember the input and answer one render name for it.
        """
        self.woken.append(item)
        return [f"render-for-{item}"]

    def retry(self) -> list:
        """
        Nothing has failed in this stub, so a pass puts nothing back; the ask is counted.
        """
        self.retries += 1
        return []


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


def test_a_render_that_failed_is_produced_again_on_the_next_pass(rig, monkeypatch):
    """
    Nothing drove a failed render again, and what that costs is the whole defect: a Luna
    away during the start-up reconcile fails every render of it, so `renders/targets` 404s
    on every http_sd poll - new nodes never scraped, deleted ones scraped for ever, and no
    targets at all after Prometheus restarts. It did not heal: the change that would have
    woken them is spent either way, because the poller takes its first good reading as a
    baseline and a failing pass that spotted a moved digest overwrites `seen` regardless.

    Asserted with no change notified at all. A pass that woke the render on a moved digest
    would be proving the ordinary path instead of the retry.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    attempts = []

    def produce():
        """
        Luna away for the first attempt, answering afterwards.
        """
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("luna is away")
        return {"nodes": ["node001"]}

    luna = FakeLuna({"node": "1"}, {"node": "1"})
    monkeypatch.setattr(hashes, "luna", lambda: luna)
    poller = hashes.LunaHashes()

    engine = renders()
    # This render alone, so the reconcile below counts this producer and the retry is not
    # also sweeping the shipped renders out to a Luna that no unit test has.
    engine.forget_all()
    engine.register("retried", "renders/thing",
                    Render(inputs=["luna.node"], coalesce="0s"), produce)
    assert engine.run_all() == 0, \
        "the start-up reconcile was meant to fail, so nothing below is proven"
    item = engine.renders["retried/renders/thing"]
    assert item.document is None and item.last_error, \
        "the render is not in the failed state this is about"

    assert poller.once() == [], "the first reading is a baseline and wakes nothing"
    assert poller.once() == [], "no digest moved, so no change may be notified"
    assert len(attempts) == 2, \
        f"the failed render was never produced again: {len(attempts)} attempt(s)"
    assert item.document == {"nodes": ["node001"]}, \
        f"produced again, but it serves nothing: {item.last_error!r}"


def test_a_render_that_failed_is_produced_again_where_luna_answers_no_hashes(rig,
                                                                            monkeypatch):
    """
    Luna answers that route on the HA master only, so on the passive controller of a pair
    every pass reads an empty dict: nothing is failing and nothing changed, and the fallback
    reports itself inert. A render that failed there is owed exactly as much as on the
    master, so the empty answer is the pass that has to put it back.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    attempts = []

    def produce():
        """
        Luna away for the first attempt, answering afterwards.
        """
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("luna is away")
        return {"nodes": ["node001"]}

    luna = FakeLuna({})
    monkeypatch.setattr(hashes, "luna", lambda: luna)
    poller = hashes.LunaHashes()

    engine = renders()
    # This render alone, so the reconcile below counts this producer and the retry is not
    # also sweeping the shipped renders out to a Luna that no unit test has.
    engine.forget_all()
    engine.register("passive", "renders/thing",
                    Render(inputs=["luna.node"], coalesce="0s"), produce)
    assert engine.run_all() == 0, \
        "the start-up reconcile was meant to fail, so nothing below is proven"
    item = engine.renders["passive/renders/thing"]
    assert item.document is None and item.last_error, \
        "the render is not in the failed state this is about"

    assert poller.once() == [], "an empty answer is never a change"
    assert (poller.failing, poller.seen) == (False, {}), \
        "Luna answering emptily is not what was driven, so the passive shape is unproven"
    assert len(attempts) == 2, \
        f"the failed render was never produced again: {len(attempts)} attempt(s)"
    assert item.document == {"nodes": ["node001"]}, \
        f"produced again, but it serves nothing: {item.last_error!r}"
    assert not item.last_error, "a render that is serving still carries its failure"


def test_a_retry_produces_what_the_engine_holds_and_not_what_it_sampled(rig):
    """
    The retry sweeps the renders it read before the first producer ran, and a reload
    landing inside that sweep replaces them. Producing from the sampled object then runs
    code that has been unplugged, after its replacement has already written, so what is
    left on disk is the old render's - which is the reason notify and the trailing timer
    look their render up again under the lock, stated in a comment beside each.

    A retry pass is where this is reachable for long enough to matter: it is one producer
    after another against a Luna that may be slow, and the reload is an ordinary enable,
    disable or reload of a plugin.

    The preconditions are asserted too, because the negative half is satisfied by a retry
    that produced nothing at all.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render.engine import current as renders

    spec = Render(inputs=["luna.node"], coalesce="0s")
    ran, swapped, reconciling = [], [], [1]

    def new_second():
        """
        The second render as the reload leaves it, still unable to produce.
        """
        ran.append("new")
        raise RuntimeError("luna is away")

    def old_second():
        """
        The second render as it was before the reload. Nothing may call this again.
        """
        ran.append("old")
        raise RuntimeError("luna is away")

    def first():
        """
        The first render of the pass: away for the reconcile, and on the retry it succeeds
        and takes the reload with it.
        """
        ran.append("first")
        if reconciling:
            reconciling.clear()
            raise RuntimeError("luna is away")
        if not swapped:
            swapped.append(1)
            engine.replace("reloaded", [("renders/second", spec, new_second)])
        return {"nodes": []}

    engine = renders()
    engine.forget_all()
    engine.register("reloaded", "renders/first", spec, first)
    engine.register("reloaded", "renders/second", spec, old_second)
    assert engine.run_all() == 0, \
        "both renders were meant to fail, so there is nothing for a retry to put back"
    ran.clear()

    engine.retry()
    assert ran[:1] == ["first"], \
        f"the pass never reached the producer that reloads, so nothing is proven: {ran}"
    assert swapped and engine.renders["reloaded/renders/second"].producer is new_second, \
        "the reload did not take, so the sampled render is still the one held"
    assert "old" not in ran, \
        f"the retry produced from a render the reload had replaced: {ran}"


def test_a_failed_render_is_put_back_on_every_pass_that_reached_luna(rig, monkeypatch):
    """
    Recovery rests on the retry standing above every way a pass can end, so it is asserted
    over all of them: no hashes at all, the baseline reading, no digest moved, a digest
    moved. A pass that never reached Luna is the one pass that owes nothing.
    """
    poller, renders = _hashes(monkeypatch, {}, {"node": "1"}, {"node": "1"},
                              {"node": "2"}, requests.ConnectionError("away"))
    for reached in range(1, 5):
        poller.once()
        assert renders.retries == reached, \
            f"pass {reached} reached Luna and put nothing back"
    assert (poller.seen, renders.woken) == ({"node": "2"}, ["luna.node"]), \
        "the four passes did not end the four ways this is asserted over"

    poller.once()
    assert renders.retries == 4, "a pass that never reached Luna has nothing to put back"


def test_the_hash_baseline_and_poller_follow_the_interval(rig, monkeypatch):
    """
    0s switches both off; a positive interval takes a baseline and starts the thread.
    """
    poller, _ = _hashes(monkeypatch, {"node": "1"})
    assert hashes.INTERVAL == 0, "the rig switches the fallback off"
    poller.baseline()
    assert poller.seen == {}
    assert poller.start() is False

    monkeypatch.setattr(hashes, "INTERVAL", 3600)
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


def test_the_peer_signs_us_in_and_the_ca_is_checked(rig, tmp_path, monkeypatch):
    """
    Over https, with the token the peer issued, verified against the cluster CA. The
    password goes with that request, so the certificate check is what makes it safe.
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

    signed_in = []
    monkeypatch.setattr(replication, "_tokens", {})
    monkeypatch.setattr(replication.requests, "post", lambda url, **kwargs: (
        signed_in.append((url, kwargs.get("verify"))),
        Answer(201, body={"token": _signed("s" * 32)}))[1])
    monkeypatch.setattr(replication.requests, "get", get)
    assert replication.Sweep()._get("peer.cluster:7070", "hashes") == {"hashes": {}}
    assert signed_in[0][0] == "https://peer.cluster:7070/api/v1/auth/token"
    assert signed_in[0][1] == str(ca), "the peer's certificate is checked before the password"
    assert seen["url"] == "https://peer.cluster:7070/api/v1/system/replication/hashes"
    assert seen["verify"] == str(ca)
    assert seen["headers"]["x-access-tokens"]

    # A refusal that a fresh token does not cure: signed in again, refused again, raised.
    monkeypatch.setattr(replication.requests, "get", lambda url, **kwargs: Answer(403))
    with pytest.raises(RuntimeError, match="answered 403"):
        replication.Sweep()._get("peer.cluster:7070", "hashes")
    assert len(signed_in) == 2, "the refusal was retried once with a new token"


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
        first = sweep.pass_over("peer.cluster:7070")
        assert first["tables"] == ["principal"]
        assert first["changes"]["principal"]["added"] == 1
        assert sweep.pass_over("peer.cluster:7070")["tables"] == []
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
    monkeypatch.setattr(configuration.current().replication, "peers", ["away.cluster:7070"])

    def away(self, address, path):
        """
        A peer that does not answer.
        """
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(replication.Sweep, "_get", away)
    sweep = replication.Sweep()
    results = sweep.once()
    assert results == [{"peer": "away.cluster:7070", "error": "no route to host"}]
    assert sweep.last["results"] == results


def test_the_sweep_runs_only_with_peers_and_stops_when_asked(rig, monkeypatch):
    """
    No peers, no thread; with one, a thread that ends on shutdown.
    """
    sweep = replication.Sweep()
    assert sweep.start() is False
    monkeypatch.setattr(configuration.current().replication, "peers", ["peer.cluster:7070"])
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


def test_a_password_does_not_reach_obol_as_an_argument(rig, monkeypatch):
    """
    An argument sits in /proc for the life of the call, readable by any local user - and a
    controller has interactive logins, with /trinity/home exported. obol will take a prompt
    instead, so the value goes down a pipe and never into the process table.

    The masking table beside the runner is the evidence this value was always known to be
    secret: it covered the log and left the argument list alone.
    """
    from trixd.plugins.directory import backend as directory
    from trixd.plugins.directory.backend import Backend

    seen = []
    existing = set()

    def run(argv, *args, **kwargs):
        """
        Record the call with whatever was handed to standard input, and behave as obol
        does either side of the add: show fails until the user is there.
        """
        seen.append((list(argv), kwargs.get("secret_input")))
        verb = argv[2] if argv[1] != "-J" else argv[3]
        name = argv[3] if argv[1] != "-J" else argv[4]
        if verb == "show" and name not in existing:
            return Ran("", 1, "not found")
        if verb == "add":
            existing.add(name)
        return Ran("{}")

    monkeypatch.setattr(directory.shell, "run", run)
    monkeypatch.setattr(directory.shell, "run_json", lambda argv, *a, **k: {"uid": argv[-1]})
    Backend().create_user({"username": "dave", "password": "a long passphrase", "uid": 1600})

    added_argv, secret = [entry for entry in seen if "add" in entry[0]][0]
    assert "a long passphrase" not in added_argv, "the password went into the argument list"
    assert "--password" not in added_argv, "obol was still told to take it as an argument"
    assert "--prompt-password" in added_argv, "obol was not asked to prompt for it"
    assert secret == "a long passphrase\n", "the password was not handed over on stdin"
    assert "--uid" in added_argv and "1600" in added_argv, "the other fields stopped arriving"


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


def _slurm(monkeypatch, nodes: dict, tmp_path, wrote: bool = True, reconfigured: int = 0,
           content: str = "one") -> dict:
    """
    The slurm plugin on a stand-in generator package, Luna and scontrol.

    The stand-in writes the files it is handed, as the real generator does. It did not, and
    that mattered once the reconfigure became conditional on their content: a generator that
    writes nothing leaves them unchanged for ever, so a test built on it would have pinned
    the opposite of what happens in production.
    """
    from trixd.plugins.slurm import backend as slurm
    from trixd.utils import declarations

    seen = {}
    declarations.write("slurm", "config", "", {
        **slurm.DEFAULT_CONFIG,
        "nodes_file": str(tmp_path / "slurm-nodes.conf"),
        "partitions_file": str(tmp_path / "slurm-partitions.conf"),
        "gres_file": str(tmp_path / "slurm-gres.conf")})

    class Generate:
        """
        trinityx_config_slurm's entry point, recording what it was given.
        """

        def all_configs(self, nodes, configs, manager):
            """
            Remember the call, write what it was asked to, and report success or failure.
            """
            seen.update(nodes=nodes, configs=configs, manager=manager)
            if wrote:
                for path in configs.values():
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(content)
            return wrote

    package = ModuleType("trinityx_config_slurm")
    package.Generate = Generate
    monkeypatch.setitem(sys.modules, "trinityx_config_slurm", package)
    monkeypatch.setattr(slurm, "luna", lambda: SimpleNamespace(nodes=lambda: nodes))
    monkeypatch.setattr(slurm.shell, "run", lambda argv, **kwargs: (
        seen.setdefault("ran", argv), Ran("", reconfigured, "boom"))[1])
    return seen


def test_slurm_renders_luna_nodes_through_the_generator(rig, monkeypatch, tmp_path):
    """
    Sorted node and group pairs, the three files, then scontrol reconfigure.
    """
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, {"node002": {"group": "gpu"}, "node001": {"group": "compute"}},
                  tmp_path)
    answer = Backend().render_config()
    assert seen["nodes"] == [{"name": "node001", "group": "compute"},
                             {"name": "node002", "group": "gpu"}]
    assert set(seen["configs"]) == {"nodes", "partitions", "gres"}
    assert seen["manager"] == "TrinityX"
    assert seen["ran"] == ["/usr/bin/scontrol", "reconfigure"]
    assert answer["nodes"] == 2 and answer["reconfigured"] is True


def test_slurm_is_not_reconfigured_when_its_files_did_not_change(rig, monkeypatch,
                                                                 tmp_path):
    """
    scontrol reconfigure ran on every pass. At start-up that is the one thing slow enough
    to put the whole start over the lifespan timeout, and on a boot storm it ran once per
    node event for files that had not moved. The Prometheus plugin already reloads only for
    a change; this is the same rule for the one call that restarts something.
    """
    from trixd.plugins.slurm.backend import Backend

    nodes = {"node001": {"group": "a"}}
    seen = _slurm(monkeypatch, nodes, tmp_path)

    first = Backend().render_config()
    assert first["reconfigured"] is True, "the first pass wrote the files and must reconfigure"
    assert seen["ran"] == ["/usr/bin/scontrol", "reconfigure"]

    # The same nodes again, so the generator writes the same bytes. The behaviour is
    # asserted before the reported value, and through get(): a build without the fix has
    # no such key, and reaching for it would error in place of asserting, which reads as
    # proof and is not.
    seen.pop("ran")
    again = Backend().render_config()
    assert "ran" not in seen, "slurm was reconfigured for files that had not changed"
    assert again.get("reconfigured") is False, again
    assert again.get("changed") is False, again

    # And a real change still reconfigures, so this is not simply switched off.
    _slurm(monkeypatch, nodes, tmp_path, content="two")
    moved = Backend().render_config()
    assert moved.get("reconfigured") is True, moved
    assert moved.get("changed") is True, moved


def test_slurm_writes_nothing_without_nodes_and_says_when_it_failed(rig, monkeypatch,
                                                                     tmp_path):
    """
    No nodes is not an error; a failing generator is; a reconfigure slurm refuses fails
    the render, because the blocks it refused are put back first.
    """
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, {}, tmp_path)
    assert Backend().render_config() == {"nodes": 0, "files": []}
    assert "nodes" not in seen, "the generator ran with no nodes"

    _slurm(monkeypatch, {"node001": {"group": "a"}}, tmp_path, wrote=False)
    with pytest.raises(BackendFailure, match="generator reported a failure"):
        Backend().render_config()

    _slurm(monkeypatch, {"node001": {"group": "a"}}, tmp_path, reconfigured=1)
    with pytest.raises(BackendFailure, match="reconfigure refused the slurm blocks"):
        Backend().render_config()


def test_slurm_without_its_generator_says_so(rig, monkeypatch):
    """
    A controller without slurm's package gets a named failure, not an ImportError.
    """
    from trixd.plugins.slurm import backend as slurm

    monkeypatch.setitem(sys.modules, "trinityx_config_slurm", None)
    # With a group: a node without one is left out before the generator is reached, as an
    # empty Luna already was, and this test is about the package rather than the group.
    monkeypatch.setattr(slurm, "luna",
                        lambda: SimpleNamespace(nodes=lambda: {"n": {"group": "a"}}))
    with pytest.raises(BackendFailure, match="not installed on this controller"):
        slurm.Backend().render_config()


def _gated() -> TestClient:
    """
    Three routes behind the gate alone: one for a node token, two for user roles.
    """
    from fastapi import Depends, FastAPI

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


def test_a_node_call_the_gate_allows_is_in_the_trail(rig):
    """
    A node token refused on another node's route was recorded; one that was allowed was not,
    so the trail held only the node calls that failed. A node's own token writes that node's
    hardware rules, which includes switching its alerting off, and that has to be answerable
    for - "the gate refused nobody" and "nothing happened" looked identical.
    """
    from trixd.core.security import audit, tokens

    client = _gated()
    own = {"x-access-tokens": tokens.mint("node001", kind="node", node="node001")}
    assert client.patch("/hw-rules/node001", headers=own).status_code == 200
    assert client.patch("/hw-rules/node002", headers=own).status_code == 403

    rows = [row for row in audit.read(limit=50) if row["object"].startswith("node00")]
    granted = [row for row in rows if row["outcome"] == "granted"]
    refused = [row for row in rows if row["outcome"] == "refused"]
    assert granted, "an allowed node call left nothing in the trail"
    assert granted[0]["object"] == "node001", granted[0]
    assert granted[0]["principal"] == "node001", granted[0]
    assert refused, "the refusal stopped being recorded"


def test_a_subject_nobody_knows_is_refused_whatever_kind_the_token_claims(rig):
    """
    A signed token for a name no source resolves is 401, and claiming to be a service
    does not change that. It used to: naming no user passed every check, including a
    manager route, so anything that read one of those headers was an administrator here.
    """
    from trixd.core.security import tokens

    client = _gated()
    ghost = {"x-access-tokens": tokens.mint("ghost-of-nobody")}
    assert client.get("/look", headers=ghost).status_code == 401
    service = {"x-access-tokens": tokens.mint("ghost-of-nobody", kind="service")}
    assert client.get("/look", headers=service).status_code == 401
    answer = client.patch("/change", headers=service)
    assert answer.status_code == 401, answer.text


def test_a_service_token_naming_the_ini_account_is_refused(rig):
    """
    The one that matters, and the one the first attempt missed.

    This daemon mints a service token for Luna and signs it with the secret both hold, and
    the subject it names is DAEMON_NAME - which is exactly what the installer writes as this
    daemon's own ini account. So removing the kind's role bypass changed nothing on a real
    install: the token passed as rootus by the ordinary route instead, and whatever read
    that header on its way to Luna still held everything, the replication export and every
    principal's password digest with it.

    The subject is taken from the settings rather than written here, because the whole
    defect was that the two happen to be the same name.
    """
    from trixd.common.settings import current as settings
    from trixd.core.security import tokens

    client = _gated()
    account = settings().api.username
    assert account, "the rig has no ini account, so this test proves nothing"
    theirs = {"x-access-tokens": tokens.mint(account, kind="service")}

    assert client.get("/look", headers=theirs).status_code == 401, \
        "a service token naming the ini account was accepted"
    answer = client.patch("/change", headers=theirs)
    assert answer.status_code == 401, \
        f"a service token naming the ini account reached a manager route: {answer.text}"

    # The same account as an ordinary user token still works, so this refuses the kind and
    # not the account.
    mine = {"x-access-tokens": tokens.mint(account)}
    assert client.patch("/change", headers=mine).status_code == 200


def test_a_services_state_is_what_systemd_says(rig, monkeypatch):
    """
    A unit's ActiveState and UnitFileState, and unknown for one systemd answered nothing for.
    """
    from trixd.plugins.services import backend as services

    def systemctl(argv, **kwargs):
        """
        dirsrv@local is running and enabled; systemd answers for nothing else.
        """
        if "dirsrv@local" not in argv:
            return Ran("", 3)
        return Ran("Id=dirsrv@local.service\nNames=dirsrv@local.service\n"
                   "LoadState=loaded\nActiveState=active\nUnitFileState=enabled")

    monkeypatch.setattr(services.shell, "run", systemctl)
    listed = services.Backend().list_services()["services"]
    assert listed["dirsrv"] == {"active": "active", "enabled": "enabled"}
    assert listed["slurmctld"] == {"active": "unknown", "enabled": "unknown"}
    assert services.Backend().act("dirsrv", "STATUS")["unit"] == "dirsrv@local"
    with pytest.raises(NotFound):
        services.Backend().status("sshd")


def test_a_listing_asks_systemd_once_and_maps_each_record_by_name(rig, monkeypatch):
    """
    One `systemctl show` for the whole catalogue rather than is-active and is-enabled per
    unit: 200 services were 400 subprocesses, each with its own minute of timeout, taken one
    after another on the thread holding the request.

    Mapped by the name in the record and never by position, which is why `show` is the right
    command: `systemctl is-enabled` over three units whose middle one is unknown prints one
    state line for the whole call, so a positional read hands one service another's state
    and nothing says it happened. The records here come back in a different order from the
    one they were asked in, so a positional read cannot pass.

    Counted rather than timed, so it says the same thing on a loaded box.
    """
    from trixd.plugins.services import backend as services

    # What systemd answers for three units whose middle one it has no unit file for. Every
    # unit's state differs, so a mapping that slipped cannot come out right by accident.
    shown = ("Id=gamma.service\nNames=gamma.service\nLoadState=loaded\n"
             "ActiveState=failed\nUnitFileState=disabled\n"
             "\n"
             "Id=alpha.service\nNames=alpha.service\nLoadState=loaded\n"
             "ActiveState=active\nUnitFileState=enabled\n"
             "\n"
             "Id=beta.service\nNames=beta.service\nLoadState=not-found\n"
             "ActiveState=inactive\nUnitFileState=")
    ran = []

    def systemctl(argv, **kwargs):
        """
        Record the call and answer as one show over the whole list does.
        """
        ran.append(argv)
        return Ran(shown)

    monkeypatch.setattr(services.shell, "run", systemctl)
    monkeypatch.setattr(services.Backend, "_units",
                        lambda self: {"a": "alpha", "b": "beta", "c": "gamma"})

    listed = services.Backend().list_services()["services"]

    assert len(ran) == 1, f"{len(ran)} subprocesses to list 3 services: {ran}"
    assert listed["a"] == {"active": "active", "enabled": "enabled"}, listed
    assert listed["c"] == {"active": "failed", "enabled": "disabled"}, listed
    # A unit systemd has no unit file for is not a unit that is stopped, and the pair of
    # commands this replaces could not tell them apart: both read inactive and unknown.
    assert listed["b"] == {"active": "not-found", "enabled": "not-found"}, listed
    assert ran[0][1] == "show", ran[0]
    assert ran[0][-3:] == ["alpha", "beta", "gamma"], ran[0]


def test_the_worker_checks_the_action_and_the_catalogue_itself(rig, monkeypatch):
    """
    `act` checks both before it queues, but the worker is reachable without going through
    it: `POST /system/notify` dispatches into the worker by the plugin's name alone, and a
    catalogue can be rewritten between an action being queued and the worker reaching it.
    So the checks are in the worker, beside the command they are protecting.
    """
    from trixd.plugins.services import backend as services

    ran = []
    monkeypatch.setattr(services.shell, "run",
                        lambda argv, **kwargs: ran.append(argv) or Ran("", 0))

    with pytest.raises(NotFound):
        services.run_task("restart", "sshd")
    assert ran == [], f"a unit outside the catalogue reached the command: {ran}"

    with pytest.raises(BadRequest):
        services.run_task("mask", "slurmctld")
    assert ran == [], f"an action outside the list reached the command: {ran}"

    assert services.run_task("restart", "slurmctld") == "restart slurmctld accepted"
    assert ran == [["/usr/bin/systemctl", "restart", "slurmctld"]], ran


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


def test_a_node_with_no_group_is_left_out_of_the_slurm_configuration(rig, monkeypatch, tmp_path):
    """
    A node Luna holds with no group renders as `NodeSet= Nodes=<node>` and `PartitionName=
    Nodes=`, which slurm will not load - and scontrol reconfigure is run over it, so one
    node created before its group was assigned took the node and partition configuration of
    every other node with it. A node without a group is ordinary, so this is left out and
    named rather than refusing the write for the nodes that do have one.
    """
    from trixd.plugins.slurm import backend as slurm
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, {"node001": {"group": "compute"},
                                "node002": {"group": None},
                                "node003": {"group": ""}}, tmp_path)
    answer = Backend().render_config()

    assert seen["nodes"] == [{"name": "node001", "group": "compute"}], seen["nodes"]
    assert answer["nodes"] == 1, answer
    assert answer["skipped"] == ["node002", "node003"], answer
    assert answer["changed"] is True, "the node that does have a group was not configured"

    # node_set is the genders plugin's too, and that render skips such a node for its own
    # reason - so the skipping belongs to each render and not to the shared reader.
    assert slurm.node_set() == [{"name": "node001", "group": "compute"},
                                {"name": "node002", "group": ""},
                                {"name": "node003", "group": ""}], \
        "the shared node list filtered a node, which is each render's own decision"


def test_every_node_without_a_group_writes_nothing_and_says_why(rig, monkeypatch, tmp_path):
    """
    Nothing to configure slurm with, which is not the same as no nodes: the generator is not
    called at all rather than handed an empty set, and the names are in the answer.
    """
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, {"node001": {"group": None}, "node002": {"group": ""}},
                  tmp_path)
    answer = Backend().render_config()

    assert answer == {"nodes": 0, "files": [], "skipped": ["node001", "node002"]}, answer
    assert "nodes" not in seen, "the generator was called with nothing to write"
    assert "ran" not in seen, "scontrol was reconfigured over a configuration never written"


def _genders(monkeypatch, nodes: dict) -> list:
    """
    Luna holding `nodes`, and a stand-in generator recording the set it was handed.
    """
    monkeypatch.setattr("trixd.plugins.slurm.backend.luna",
                        lambda: SimpleNamespace(nodes=lambda: nodes))
    handed: list = []
    package = ModuleType("trinityx_config_genders")
    package.Generate = lambda: SimpleNamespace(
        Genders=lambda given: (handed.extend(given), True)[1])
    monkeypatch.setitem(sys.modules, "trinityx_config_genders", package)
    return handed


def test_a_node_with_no_group_is_left_out_of_the_genders_file(rig, monkeypatch):
    """
    A node Luna holds with no group renders as `<node> all,`, and libgenders refuses an empty
    attribute for the whole file rather than for the line, so one such node makes the genders
    file unloadable for every pdsh on the cluster. Left out and named, as slurm's render does.
    """
    from trixd.plugins.genders.backend import Backend

    handed = _genders(monkeypatch, {"node001": {"group": "compute"},
                                    "node002": {"group": None},
                                    "node003": {"group": ""}})
    answer = Backend().render_genders()

    # True on either build, and first: no node reached the generator without a group, so no
    # line it writes can carry the empty attribute libgenders refuses.
    assert [one["name"] for one in handed] == ["node001"], handed
    assert all(one["group"] for one in handed), handed
    assert answer["nodes"] == 1, answer
    assert answer.get("skipped") == ["node002", "node003"], answer


def test_every_node_without_a_group_writes_no_genders_file_and_says_why(rig, monkeypatch):
    """
    Nothing to write, which is not the same as no nodes: the generator is not handed an empty
    set, which would empty the managed block, and the names are in the answer.
    """
    from trixd.plugins.genders.backend import Backend

    handed = _genders(monkeypatch, {"node001": {"group": None}, "node002": {"group": ""}})
    answer = Backend().render_genders()

    assert handed == [], "the generator was handed a set that would empty the genders file"
    assert answer["nodes"] == 0, answer
    assert answer.get("skipped") == ["node001", "node002"], answer


def test_listing_the_services_reads_the_catalogue_once(rig, monkeypatch):
    """
    The catalogue was read once for the ordering and again for every service in it, and each
    read is its own database session and query - so a listing cost one query per service for
    an answer that cannot change during the call. One read, whatever the catalogue holds.

    Counted rather than timed, so it says the same thing on a loaded box.
    """
    from trixd.plugins.services import backend as services

    reads = []

    def counted(self) -> dict:
        """
        The catalogue, remembering that it was asked for.
        """
        reads.append(1)
        return {f"svc{n}": f"svc{n}.service" for n in range(12)}

    monkeypatch.setattr(services.Backend, "_units", counted)
    monkeypatch.setattr(services.shell, "run",
                        lambda argv, **kwargs: Ran("active", 0, ""))

    answer = services.Backend().list_services()

    assert len(answer["services"]) == 12, answer
    assert len(reads) == 1, f"the catalogue was read {len(reads)} times for 12 services"


def test_a_hash_poller_whose_passes_fail_does_not_report_itself_answering(rig, monkeypatch):
    """
    The poller surviving a failed pass is deliberate and stays. What was wrong is that
    nothing said the pass had failed: `answering` stayed true and `last_pass` went on
    advancing, so a Luna erroring on every pass published the same state as one being read
    successfully - on the unauthenticated health route, which is what a monitor acts on.

    Note the direction. A passive controller, where an empty answer is normal and harmless,
    correctly reports false. It was the broken case that read as healthy.
    """
    poller, _ = _hashes(monkeypatch, {"node": "1"}, requests.ConnectionError("away"),
                        {"node": "1"})

    assert poller.once() == [], "the first reading is a baseline"
    assert poller.state()["answering"] is True, \
        "a good pass does not report answering, so this proves nothing"

    assert poller.once() == [], "a failed pass is skipped, not fatal"
    assert poller.state()["answering"] is False, \
        "every pass is failing and it still reports itself answering"
    assert poller.seen == {"node": "1"}, "a failed pass threw away the baseline"

    assert poller.once() == [], "the poller did not carry on after the failure"
    assert poller.state()["answering"] is True, "it never recovers once a pass has failed"


def test_a_luna_node_answer_this_build_cannot_read_is_not_an_empty_cluster(rig):
    """
    Luna answers config/node 404 when it holds no nodes, so nothing to read is genuinely an
    empty cluster and stays one. An answer that is there in a shape this build does not read
    is a different thing: read as "no nodes" it emptied the genders file and the slurm blocks
    for every node at once, while the render recorded a healthy pass and reported nodes: 0.
    """
    empty = (Answer(404), Answer(body={"config": {"node": {}}}))
    unreadable = {
        "a renamed envelope": Answer(body={"config": {"nodes": {"node001": {}}}}),
        "a list where the map was": Answer(body={"config": {"node": ["node001"]}}),
        "another object's envelope": Answer(body={"monitor": {"status": {}}}),
        "a body that is not JSON": Answer(text="service unavailable"),
    }

    client, _ = _luna(*empty)
    assert client.nodes() == {}, "a cluster with no nodes has to keep reading as empty"
    assert client.nodes() == {}, "an envelope holding no node is still an empty cluster"

    for why, answer in unreadable.items():
        client, _ = _luna(answer)
        with pytest.raises(ServiceUnavailable, match="cannot read"):
            client.nodes()
        assert client.session.calls, f"{why} was never asked for"


def test_the_renders_keep_their_files_when_lunas_node_answer_cannot_be_read(rig, monkeypatch,
                                                                           tmp_path):
    """
    The two renders built from the shared node list treat an unreadable answer as the
    Prometheus render already does - they fail, so the engine keeps the previous result and
    what is on disk - while a genuinely empty cluster still writes nothing and answers
    nodes: 0 exactly as it did.
    """
    from trixd.plugins.genders import backend as genders
    from trixd.plugins.slurm import backend as slurm

    wrote = []
    package = ModuleType("trinityx_config_genders")
    package.Generate = lambda: SimpleNamespace(
        Genders=lambda nodes: wrote.append(nodes) or True)
    monkeypatch.setitem(sys.modules, "trinityx_config_genders", package)
    seen = _slurm(monkeypatch, {}, tmp_path)

    def answering(answer):
        """
        Both plugins reading a real client whose session answers this, twice.
        """
        client, _ = _luna(answer, answer)
        monkeypatch.setattr(slurm, "luna", lambda: client)

    # Luna's own answer for a cluster holding no node, which is a 404.
    answering(Answer(404))
    assert slurm.Backend().render_config() == {"nodes": 0, "files": []}
    assert genders.Backend().render_genders() == {"nodes": 0, "file": "/etc/genders"}
    assert "nodes" not in seen, "the slurm generator ran for a cluster with no nodes"
    assert wrote == [], "the genders generator ran for a cluster with no nodes"

    # The same two renders over an answer whose shape has moved.
    answering(Answer(body={"config": {"nodes": {"node001": {"group": "a"}}}}))
    with pytest.raises(ServiceUnavailable, match="cannot read"):
        slurm.Backend().render_config()
    with pytest.raises(ServiceUnavailable, match="cannot read"):
        genders.Backend().render_genders()
    assert "nodes" not in seen, "the slurm generator was handed an answer nothing could read"
    assert wrote == [], "the genders file was rewritten from an answer nothing could read"


def test_a_node_name_is_encoded_into_lunas_path(rig):
    """
    The node sign-in route takes the name from an unauthenticated caller and this is where it
    reaches Luna. requests leaves ?, # and / in a path alone, and Starlette has already
    turned %2f back into / by the time the route matched, so the name is quoted here.
    """
    wanted = {"node001": "node001", "a/b": "a%2Fb", "x?secret=1": "x%3Fsecret%3D1",
              "y#frag": "y%23frag", "z%2fw": "z%252fw"}
    client, session = _luna(*[Answer(body={"config": {"node": {}}})] * len(wanted))

    for name, quoted in wanted.items():
        client.node(name)
        assert session.calls[-1][1] == f"https://luna.test:7050/config/node/{quoted}", name


# A person signed in to trixd reaches Luna through the route to Luna with their own Luna
# token, kept in their home as the Luna command line keeps it, and no password is written.
# Anything the change added is reached through _new or hasattr, so on a build without it
# these fail on what Luna received rather than on a missing name.

PERSON: str = "harry"
PERSON_PASSWORD: str = "harry-password"
DELEGATE: str = "trixd-portal"
DELEGATE_PASSWORD: str = "portal-password"


def _new(module: str):
    """
    A module the change added, or None on a build that has none.
    """
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError:
        return None


def _luna_minted(name: str, seconds: int = 300) -> str:
    """
    A token as Luna mints one for a person: an id and an expiry, nothing else.
    """
    from datetime import datetime, timedelta, timezone

    expiry = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return jwt.encode({"id": 7000 + len(name), "exp": expiry, "for": name}, "luna-secret",
                      algorithm="HS256")


class _Luna:
    """
    Luna's /token, delegation included, and every other route: who signed in, and which
    token each call carried.
    """

    def __init__(self, monkeypatch, people: dict) -> None:
        """
        Stand in for both ends the plugin reaches Luna by.
        """
        self.people = people
        self.signed = []
        self.seen = []
        self.issued = []
        self.refused = set()
        self.posted_to = []
        self.sent_to = []
        monkeypatch.setattr(credentials.requests, "post", self.post)
        monkeypatch.setattr("trixd_plugin_luna.backend.requests.request", self.request)

    def post(self, url, json=None, **kwargs):
        """
        POST /token: a person with the right password, or the delegate for a known person.
        """
        self.posted_to.append(url)
        self.signed.append(dict(json))
        name, behalf = json.get("username"), json.get("on_behalf_of")
        if self.people.get(name) != json.get("password"):
            return Answer(401, {"message": "Invalid username or password"})
        if behalf and (name != DELEGATE or behalf not in self.people):
            return Answer(403, {"message": f"{name} may not obtain a token for {behalf}"})
        self.issued.append(_luna_minted(behalf or name))
        return Answer(201, {"token": self.issued[-1]})

    def request(self, method, url, headers=None, **kwargs):
        """
        Any other call: answered, with the token it carried remembered.
        """
        self.sent_to.append(url)
        if method == "POST" and url.endswith("/token"):
            self.issued.append(_luna_minted(PERSON))
            body = {"token": self.issued[-1]}
            return SimpleNamespace(content=json.dumps(body).encode(), status_code=201,
                                   headers={"content-type": "application/json"})
        self.seen.append((headers or {}).get("x-access-tokens"))
        if self.seen[-1] in self.refused:
            return SimpleNamespace(content=b'{"message": "Token is invalid"}', status_code=401,
                                   headers={"content-type": "application/json"})
        return SimpleNamespace(content=b'{"answered": true}', status_code=200,
                               headers={"content-type": "application/json"})


def _delegate(monkeypatch, username: str = DELEGATE, password: str = DELEGATE_PASSWORD,
              endpoint: str = ""):
    """
    [DELEGATE:LUNA] as trixd.ini would set it, read by this build's own loader, where this
    build reads such sections.
    """
    import tempfile

    current = configuration.current()
    if not hasattr(current, "delegates"):
        return
    with tempfile.TemporaryDirectory() as folder:
        ini = os.path.join(folder, "trixd.ini")
        with open(ini, "w", encoding="utf-8") as handle:
            handle.write(f"[DELEGATE:LUNA]\nUSERNAME = {username}\nPASSWORD = {password}\n"
                         f"ENDPOINT = {endpoint}\n")
        monkeypatch.setitem(current.delegates, "LUNA", configuration.load(ini).delegates["LUNA"])


def _person(client, headers, tmp_path, monkeypatch, source: str = "pam"):
    """
    harry: a principal here, a home of his own under tmp_path, and a trixd token naming the
    source he signed in through.
    """
    from trixd.core.security import tokens

    made = client.put(f"/api/v1/identity/users/{PERSON}", json={}, headers=headers)
    assert made.status_code == 200, made.text
    home = tmp_path / "home" / PERSON
    home.mkdir(parents=True)
    entry = SimpleNamespace(pw_name=PERSON, pw_dir=str(home), pw_uid=os.getuid(),
                            pw_gid=os.getgid())
    persons = _new("trixd.plugins.luna.persons")
    if persons is not None:
        monkeypatch.setattr(persons, "_home", lambda name, came: (
            entry if name == PERSON and came in ("pam", "ldap") else None))
    return SimpleNamespace(home=home,
                           headers={"x-access-tokens": tokens.mint(PERSON, source=source)})


def _luna_folder(home, **files) -> None:
    """
    ~/.luna as the command line leaves it, holding the files given.
    """
    folder = home / ".luna"
    folder.mkdir(mode=0o700, exist_ok=True)
    for name, text in files.items():
        (folder / name.replace("_", ".")).write_text(text, encoding="utf-8")


def _login(name: str, password: str) -> str:
    """
    ~/.luna/luna.ini as luna access login writes it.
    """
    return f"[API]\nusername = {name}\npassword = {password}\n"


def _no_password_in(home, *passwords) -> None:
    """
    No file anywhere under the home carries any of these passwords.
    """
    for path in home.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            for password in passwords:
                assert password not in text, f"{path} holds a password"


posix_only = pytest.mark.skipif(os.name != "posix", reason="homes, owners and modes are POSIX")


def test_only_pam_and_ldap_people_with_an_ordinary_account_spelt_exactly_have_a_home(
        monkeypatch):
    """
    The home rule: a source that vouches for an operating-system account, an account that
    could log in itself, spelt as the person signed in, and never uid 0.
    """
    persons = _new("trixd.plugins.luna.persons")
    assert persons is not None, "nothing decides which people have a home here"
    from trixd.core.security.sources import pam as pam_source

    accounts = {"harry": ("harry", 1001), "root": ("root", 0), "Harry": ("harry", 1001)}

    class Source:
        """
        The pam source's own test, answered from the table above.
        """

        def resolve(self, name):
            """
            Known when the table has the name.
            """
            return (name in accounts), {"name": name}

    import pwd
    monkeypatch.setattr(pam_source, "Source", Source)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(
        pw_name=accounts[name][0], pw_uid=accounts[name][1], pw_gid=100,
        pw_dir=f"/home/{accounts[name][0]}"))

    assert persons._home("harry", "pam").pw_dir == "/home/harry"
    assert persons._home("harry", "ldap").pw_dir == "/home/harry"
    for name, source in (("harry", "local"), ("harry", "oidc"), ("harry", "ini"),
                         ("root", "pam"), ("svc", "pam"), ("Harry", "ldap")):
        assert persons._home(name, source) is None, (name, source)


@posix_only
def test_a_person_s_luna_token_in_their_home_is_what_luna_receives(
        client, headers, tmp_path, monkeypatch):
    """
    harry sends his trixd token; Luna receives the token in ~harry/.luna/token instead,
    and nothing is signed in for.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD})
    kept = _luna_minted(PERSON)
    _luna_folder(harry.home, token=kept)

    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert luna.seen == [kept], "Luna was not handed harry's own Luna token"
    assert answer.status_code == 200, answer.text
    assert luna.signed == []


@posix_only
def test_with_his_own_luna_ini_trixd_signs_in_as_the_command_line_renews(
        client, headers, tmp_path, monkeypatch):
    """
    No usable token: harry's own ~/.luna/luna.ini signs in, Luna receives what it answered,
    and that lands in ~/.luna/token as the command line writes it, with luna.ini untouched.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD})
    _luna_folder(harry.home, luna_ini=_login(PERSON, PERSON_PASSWORD))

    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert luna.signed == [{"username": PERSON, "password": PERSON_PASSWORD}], luna.signed
    assert luna.seen == luna.issued[-1:], "Luna was not handed the token it just issued"
    assert answer.status_code == 200, answer.text
    token = harry.home / ".luna" / "token"
    assert token.read_bytes() == luna.issued[-1].encode()
    assert stat.S_IMODE(token.stat().st_mode) == 0o600 and token.stat().st_uid == os.getuid()
    assert (harry.home / ".luna" / "luna.ini").read_text(encoding="utf-8") == _login(
        PERSON, PERSON_PASSWORD)

    client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert len(luna.signed) == 1, "the kept token was not used the second time"


@posix_only
def test_with_nothing_of_his_own_the_delegate_signs_him_in_and_no_password_is_written(
        client, headers, tmp_path, monkeypatch):
    """
    No token and no luna.ini: the delegate account signs harry in at Luna on his behalf,
    Luna receives harry's token, it is kept in ~/.luna/token, and no luna.ini or any
    password is written into his home.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)

    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert luna.signed == [{"username": DELEGATE, "password": DELEGATE_PASSWORD,
                            "on_behalf_of": PERSON}], luna.signed
    assert luna.seen == luna.issued[-1:], "Luna was not handed the token issued for harry"
    assert answer.status_code == 200, answer.text
    token = harry.home / ".luna" / "token"
    assert token.read_bytes() == luna.issued[-1].encode()
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE((harry.home / ".luna").stat().st_mode) == 0o700
    assert not (harry.home / ".luna" / "luna.ini").exists()
    assert not (harry.home / ".trixd").exists()
    _no_password_in(harry.home, PERSON_PASSWORD, DELEGATE_PASSWORD)


@posix_only
def test_a_luna_ini_naming_somebody_else_is_not_used(client, headers, tmp_path,
                                                       monkeypatch):
    """
    ~harry/.luna/luna.ini naming bob is treated as no file, said once: the delegate signs
    harry in, and bob's password is never tried from here.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, "bob": "bob-password",
                               DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)
    _luna_folder(harry.home, luna_ini=_login("bob", "bob-password"))

    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert [asked["username"] for asked in luna.signed] == [DELEGATE], luna.signed
    assert answer.status_code == 200, answer.text


@posix_only
def test_with_no_delegate_configured_the_client_is_asked_and_it_is_said_once(
        client, headers, tmp_path, monkeypatch):
    """
    Nothing of harry's own and no [DELEGATE:LUNA]: a 401 asking for Luna credentials, Luna
    never asked, and the missing account named in the log once, not per call.
    """
    from tests.unit.test_daemon import _listening, _said
    from trixd.common import logging as trixlog

    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD})
    _delegate(monkeypatch, "", "")
    with _listening(trixlog.get()) as records:
        first = client.get("/api/v1/luna/whoami", headers=harry.headers)
        second = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert first.status_code == second.status_code == 401, (first.text, second.text)
    assert "sign in at Luna" in first.json()["message"], first.text
    assert luna.signed == [] and luna.seen == []
    said = [record for record in records if "DELEGATE:LUNA" in record.getMessage()]
    assert len(said) == 1, _said(records)


@posix_only
def test_his_refused_luna_ini_is_not_offered_again_until_it_changes(
        client, headers, tmp_path, monkeypatch):
    """
    Luna turning harry's own luna.ini down is a 401 asking for Luna credentials, and the
    same file is not offered again until it changes.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD})
    _luna_folder(harry.home, luna_ini=_login(PERSON, "stale"))

    first = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert first.status_code == 401, first.text
    assert "sign in at Luna" in first.json()["message"], first.text
    second = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert second.status_code == 401, second.text
    assert len(luna.signed) == 1, "refused credentials were offered again"

    _luna_folder(harry.home, luna_ini=_login(PERSON, PERSON_PASSWORD) + "\n")
    os.utime(harry.home / ".luna" / "luna.ini", ns=(1, 1))
    assert client.get("/api/v1/luna/whoami", headers=harry.headers).status_code == 200
    assert len(luna.signed) == 2


@posix_only
def test_a_refused_delegated_sign_in_waits_out_the_cool_off(client, headers, tmp_path,
                                                            monkeypatch):
    """
    Luna refusing the delegate for harry is a 401, and harry's next calls do not ask Luna
    again until the cool-off has passed: a wrong delegate password is not a login attempt
    per call.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch, DELEGATE, "wrong")

    first = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert first.status_code == 401, first.text
    assert client.get("/api/v1/luna/whoami", headers=harry.headers).status_code == 401
    assert len(luna.signed) == 1, "the refused delegate was asked again at once"

    persons = _new("trixd.plugins.luna.persons")
    monkeypatch.setattr(persons.time, "monotonic",
                        lambda: 10 ** 9)
    client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert len(luna.signed) == 2, "the cool-off never ended"


@posix_only
def test_a_home_that_cannot_keep_the_token_holds_it_and_says_so_once(
        client, headers, tmp_path, monkeypatch):
    """
    ~/.luna cannot be written: the call still goes through as harry with the token held
    here, the next call uses it without signing in again, and the log says so once.
    """
    from tests.unit.test_daemon import _listening, _said
    from trixd.common import logging as trixlog

    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)
    (harry.home / ".luna").write_text("not a folder", encoding="utf-8")

    with _listening(trixlog.get()) as records:
        first = client.get("/api/v1/luna/whoami", headers=harry.headers)
        second = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert luna.seen == luna.issued[-1:] * 2, "the calls did not reach Luna as harry"
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert len(luna.signed) == 1, "the held token was not used for the second call"
    warnings = [record for record in records if record.levelname == "WARNING"
                and PERSON in record.getMessage()]
    assert len(warnings) == 1, _said(records)


@posix_only
def test_a_home_that_does_not_answer_is_a_503_for_that_person_alone(
        client, headers, token, tmp_path, monkeypatch):
    """
    Every turn at the homes taken: harry's call answers 503 within the wait, and the ini
    account's call to Luna, which touches no home, still goes through.
    """
    import threading

    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {})
    persons = _new("trixd.plugins.luna.persons")
    if persons is not None:
        taken = threading.BoundedSemaphore(1)
        taken.acquire()
        monkeypatch.setattr(persons, "HOME_CALLS", taken)
        monkeypatch.setattr(persons, "HOME_WAIT", 0.05)

    stuck = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert stuck.status_code == 503, stuck.text
    assert "harry" in stuck.json()["message"], stuck.text
    assert client.get("/api/v1/luna/whoami", headers={"x-access-tokens": token}
                      ).status_code == 200
    assert luna.seen == [token]


@posix_only
def test_signing_in_at_luna_with_a_trixd_token_keeps_the_token_and_never_returns_it(
        client, headers, tmp_path, monkeypatch):
    """
    POST /luna/token from harry, holding only his trixd token: 201 with no token in the
    answer, the token kept in ~/.luna and his password nowhere; another name is a 403.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, "sally": "sally-password"})

    answer = client.post("/api/v1/luna/token", headers=harry.headers,
                         json={"username": PERSON, "password": PERSON_PASSWORD})
    assert "token" not in answer.json(), "the Luna token went back to the client"
    assert answer.status_code == 201, answer.text
    assert (harry.home / ".luna" / "token").read_bytes() == luna.issued[-1].encode()
    assert not (harry.home / ".luna" / "luna.ini").exists()
    _no_password_in(harry.home, PERSON_PASSWORD)

    other = client.post("/api/v1/luna/token", headers=harry.headers,
                        json={"username": "sally", "password": "sally-password"})
    assert other.status_code == 403, other.text


@posix_only
def test_links_in_the_home_are_never_read_through(client, headers, tmp_path, monkeypatch):
    """
    ~/.luna/luna.ini linked to Luna's own ini, ~/.luna/token hard-linked to another file,
    and ~/.luna linked to somebody else's folder: none is used as harry's, Luna receives
    no sign-in with what they hold, and nothing is written through them.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {"luna": "admin-password"})
    _delegate(monkeypatch, "", "")
    controller = tmp_path / "controller-luna.ini"
    controller.write_text(_login("luna", "admin-password"), encoding="utf-8")
    other = tmp_path / "other-token"
    other.write_text(_luna_minted("root"), encoding="utf-8")
    _luna_folder(harry.home)
    (harry.home / ".luna" / "luna.ini").symlink_to(controller)
    os.link(other, harry.home / ".luna" / "token")

    linked = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert luna.signed == [], "a login behind a link was signed in with"
    assert luna.seen == [], "a token behind a hard link was handed to Luna as harry's"
    assert linked.status_code == 401, linked.text

    elsewhere = tmp_path / "root-luna"
    elsewhere.mkdir()
    planted = _luna_minted("root")
    (elsewhere / "token").write_text(planted, encoding="utf-8")
    for path in (harry.home / ".luna").iterdir():
        path.unlink()
    (harry.home / ".luna").rmdir()
    (harry.home / ".luna").symlink_to(elsewhere, target_is_directory=True)
    folder = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert planted not in luna.seen, "a token behind a linked folder was handed to Luna"
    assert folder.status_code == 401, folder.text
    assert (elsewhere / "token").read_text(encoding="utf-8") == planted


@posix_only
def test_a_person_never_falls_through_to_the_daemons_own_token(
        client, headers, tmp_path, monkeypatch):
    """
    With nothing in his home and no delegate, harry gets C - never the daemon's own token,
    which is Luna's administrator.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD})
    _delegate(monkeypatch, "", "")

    def daemons_own(*args, **kwargs):
        """
        The daemon's own token: a person's call must never ask for it.
        """
        raise AssertionError("a person's call asked for the daemon's own Luna token")

    monkeypatch.setattr(credentials, "get_token", daemons_own)
    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert luna.seen == [], "Luna was called for harry with nothing of his"
    assert answer.status_code == 401, answer.text


@posix_only
def test_what_is_not_a_person_s_trixd_token_goes_to_luna_as_sent(
        client, headers, token, tmp_path, monkeypatch):
    """
    The ini account's token, one Luna minted, a node's, the daemon's own service token
    and none at all reach Luna exactly as they were sent.
    """
    from trixd.core.security import tokens

    _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {})
    sent = [token, _luna_minted("anna"), tokens.mint("node001", kind="node"),
            credentials._self_signed()]
    for each in sent:
        client.get("/api/v1/luna/whoami", headers={"x-access-tokens": each})
    client.get("/api/v1/luna/whoami")
    assert luna.seen == sent + [None]


@posix_only
def test_a_kept_token_luna_refuses_is_signed_in_again_and_the_call_sent_once_more(
        client, headers, tmp_path, monkeypatch):
    """
    ~/.luna/token signed by the other controller's Luna: refused here, so the delegate signs
    harry in once, the same request goes again with the new token, and that is what is kept.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)
    stale = _luna_minted(PERSON, seconds=200)
    luna.refused.add(stale)
    _luna_folder(harry.home, token=stale)
    answering, bodies = luna.request, []

    def answering_and_keeping_the_body(method, url, headers=None, **kwargs):
        """
        Luna's answer, with the body each send carried remembered.
        """
        bodies.append(kwargs.get("data"))
        return answering(method, url, headers=headers, **kwargs)

    monkeypatch.setattr("trixd_plugin_luna.backend.requests.request",
                        answering_and_keeping_the_body)
    sent = b'{"config": {"node": {"node001": {}}}}'
    answer = client.post("/api/v1/luna/node/node001", headers=harry.headers, content=sent)
    assert [asked["username"] for asked in luna.signed] == [DELEGATE], luna.signed
    assert luna.seen == [stale, luna.issued[-1]], "the call was not sent again, once"
    assert answer.status_code == 200, answer.text
    assert (harry.home / ".luna" / "token").read_text(encoding="utf-8") == luna.issued[-1]
    assert bodies == [sent, sent], bodies


@posix_only
def test_a_call_is_sent_again_once_only_and_then_the_client_is_asked(
        client, headers, tmp_path, monkeypatch):
    """
    Luna refusing the fresh token as well: one sign-in, two sends, and C.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)
    stale = _luna_minted(PERSON, seconds=200)
    luna.refused.add(stale)
    luna.refused.add(_luna_minted(PERSON))
    _luna_folder(harry.home, token=stale)

    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert len(luna.signed) == 1, luna.signed
    assert len(luna.seen) == 2, luna.seen
    assert answer.status_code == 401, answer.text
    assert "sign in at Luna with POST /api/v1/luna/token" in answer.text, answer.text


@posix_only
def test_a_token_written_meanwhile_is_not_removed_and_is_what_is_sent_again(
        client, headers, tmp_path, monkeypatch):
    """
    Another call keeps a new token while Luna is refusing the stale one: that file stays,
    and the call is sent again with it rather than signing in once more.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)
    stale, meanwhile = _luna_minted(PERSON, seconds=200), _luna_minted(PERSON, seconds=250)
    luna.refused.add(stale)
    _luna_folder(harry.home, token=stale)
    answering = luna.request

    def refusing_while_another_call_keeps(method, url, headers=None, **kwargs):
        """
        Luna's answer, with the other call's token landing in the home as it is given.
        """
        if (headers or {}).get("x-access-tokens") == stale:
            kept = harry.home / ".luna" / ".token.other"
            kept.write_text(meanwhile, encoding="utf-8")
            os.replace(kept, harry.home / ".luna" / "token")
        return answering(method, url, headers=headers, **kwargs)

    monkeypatch.setattr("trixd_plugin_luna.backend.requests.request",
                        refusing_while_another_call_keeps)
    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert luna.seen == [stale, meanwhile], "the call was not sent again with the kept token"
    assert (harry.home / ".luna" / "token").read_text(encoding="utf-8") == meanwhile
    assert luna.signed == [], luna.signed
    assert answer.status_code == 200, answer.text


@posix_only
def test_a_refusal_of_what_is_not_a_person_s_call_goes_back_as_luna_said_it(
        client, headers, tmp_path, monkeypatch):
    """
    A token Luna minted, sent as it is and refused: Luna's 401 unchanged, sent once, and
    nobody signed in for.
    """
    _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)
    sent = _luna_minted("anna")
    luna.refused.add(sent)

    answer = client.get("/api/v1/luna/whoami", headers={"x-access-tokens": sent})
    assert luna.seen == [sent] and luna.signed == [], (luna.seen, luna.signed)
    assert answer.status_code == 401
    assert answer.json() == {"message": "Token is invalid"}


FLOATING: str = "luna-float.test:7050"
LOCAL_BASE: str = "https://luna.test:7050"
FLOATING_BASE: str = f"https://{FLOATING}"


@posix_only
def test_a_person_s_call_and_delegated_sign_in_go_to_the_delegate_endpoint(
        client, headers, tmp_path, monkeypatch):
    """
    [DELEGATE:LUNA] ENDPOINT set: harry is signed in there and his call goes there, with
    luna.ini's protocol, so the token he holds comes from the Luna that verifies it.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch, endpoint=FLOATING)

    answer = client.get("/api/v1/luna/whoami?a=1", headers=harry.headers)
    assert luna.posted_to == [f"{FLOATING_BASE}/token"], luna.posted_to
    assert luna.sent_to == [f"{FLOATING_BASE}/whoami?a=1"], luna.sent_to
    assert answer.status_code == 200, answer.text


@posix_only
def test_a_person_s_own_sign_ins_go_to_the_delegate_endpoint(client, headers, tmp_path,
                                                             monkeypatch):
    """
    Both ways a person signs in as themselves, their own ~/.luna/luna.ini and POST
    /api/v1/luna/token, are asked of the Luna at [DELEGATE:LUNA] ENDPOINT.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch, endpoint=FLOATING)
    _luna_folder(harry.home, luna_ini=_login(PERSON, PERSON_PASSWORD))

    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert answer.status_code == 200, answer.text
    signed = client.post("/api/v1/luna/token", headers=harry.headers,
                         json={"username": PERSON, "password": PERSON_PASSWORD})
    assert signed.status_code == 201, signed.text
    assert [asked["username"] for asked in luna.signed] == [PERSON, PERSON], luna.signed
    assert luna.posted_to == [f"{FLOATING_BASE}/token"] * 2, luna.posted_to


@posix_only
def test_without_a_delegate_endpoint_a_person_s_calls_go_where_luna_ini_says(
        client, headers, tmp_path, monkeypatch):
    """
    No ENDPOINT, or an empty one: the delegated sign-in and the call go to luna.ini's
    address, as they did before trixd.ini could name one.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)

    answer = client.get("/api/v1/luna/whoami", headers=harry.headers)
    assert answer.status_code == 200, answer.text
    assert luna.posted_to == [f"{LOCAL_BASE}/token"], luna.posted_to
    assert luna.sent_to == [f"{LOCAL_BASE}/whoami"], luna.sent_to


def test_trixd_s_own_calls_stay_where_luna_ini_says_with_a_delegate_endpoint(
        rig, client, tmp_path, monkeypatch):
    """
    ENDPOINT is for a person's calls only: trixd's client, a token sent as it is, and the
    ini account's sign-in all still go to luna.ini's address.
    """
    _delegate(monkeypatch, endpoint=FLOATING)
    own, session = _luna(Answer(body={"config": {"node": {}}}))
    own.node("node001")
    assert session.calls[-1][1] == f"{LOCAL_BASE}/config/node/node001"

    luna = _Luna(monkeypatch, {"luna": "p"})
    answer = client.get("/api/v1/luna/whoami", headers={"x-access-tokens": _luna_minted("anna")})
    assert answer.status_code == 200, answer.text
    assert luna.sent_to == [f"{LOCAL_BASE}/whoami"], luna.sent_to

    # A luna.ini with no secret of its own, so the ini account signs in.
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("TRIXD_LUNA_INI", _luna_ini(plain, username="luna", password="p"))
    monkeypatch.setenv(credentials.TOKEN_FILE_ENV, str(tmp_path / "cache" / "token"))
    credentials.get_token(renew=True)
    assert luna.posted_to == [f"{LOCAL_BASE}/token"], luna.posted_to


def test_a_token_held_for_a_person_is_not_presented_at_another_endpoint(
        client, headers, tmp_path, monkeypatch):
    """
    A person with no home here has their token held in memory. Once ENDPOINT names another
    Luna, that token is not offered there: the delegate signs them in again.
    """
    nobody = _person(client, headers, tmp_path, monkeypatch, source="local")
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch, endpoint=FLOATING)
    client.get("/api/v1/luna/whoami", headers=nobody.headers)
    client.get("/api/v1/luna/whoami", headers=nobody.headers)
    assert len(luna.signed) == 1, "the held token was not used at the same endpoint"

    _delegate(monkeypatch, endpoint="luna-other.test:7050")
    answer = client.get("/api/v1/luna/whoami", headers=nobody.headers)
    assert len(luna.signed) == 2, "a token held for one Luna was presented to another"
    assert luna.seen[-1] == luna.issued[-1], luna.seen
    assert luna.posted_to[-1] == "https://luna-other.test:7050/token", luna.posted_to
    assert answer.status_code == 200, answer.text


def _person_calls(client, headers, tmp_path, monkeypatch, **access):
    """
    harry as a plugin holds him, Luna faked, and what his GET .../_access answers.
    """
    harry = _person(client, headers, tmp_path, monkeypatch)
    luna = _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch, endpoint=FLOATING)
    answering = luna.request

    def holding(method, url, headers=None, **kwargs):
        """
        Luna's answer, with what harry holds at his own _access.
        """
        if url.endswith(f"/config/user/{PERSON}/_access"):
            luna.sent_to.append(url)
            luna.seen.append((headers or {}).get("x-access-tokens"))
            body = {"config": {"user": {PERSON: {"access": access}}}}
            return SimpleNamespace(content=json.dumps(body).encode(), status_code=200,
                                   headers={"content-type": "application/json"})
        return answering(method, url, headers=headers, **kwargs)

    monkeypatch.setattr("trixd_plugin_luna.backend.requests.request", holding)
    persons = _new("trixd.plugins.luna.persons")
    return harry, luna, persons, persons.caller(harry.headers)


def _raised(work, *args, **kwargs):
    """
    What the work raised, or None.
    """
    try:
        work(*args, **kwargs)
    except Exception as error:                      # pylint: disable=broad-except
        return error
    return None


@posix_only
def test_a_plugin_calls_luna_as_the_person_where_their_calls_go(
        client, headers, tmp_path, monkeypatch):
    """
    persons.call: harry's own kept Luna token, at the delegate endpoint, method and body as
    given, and Luna's answer back.
    """
    harry, luna, persons, person = _person_calls(client, headers, tmp_path, monkeypatch)
    kept = _luna_minted(PERSON)
    _luna_folder(harry.home, token=kept)
    call = getattr(persons, "call", lambda *args, **kwargs: None)

    answer = call(person, "GET", "config/node/node001")
    assert luna.sent_to == [f"{FLOATING_BASE}/config/node/node001"], luna.sent_to
    assert luna.seen == [kept], "Luna was not handed harry's own Luna token"
    assert luna.signed == [], luna.signed
    assert answer.status_code == 200


@posix_only
def test_a_plugin_s_call_with_a_refused_kept_token_is_sent_once_more(
        client, headers, tmp_path, monkeypatch):
    """
    A kept token Luna refuses: dropped, the delegate signs harry in, the call goes again
    with the new token, and that token is kept.
    """
    harry, luna, persons, person = _person_calls(client, headers, tmp_path, monkeypatch)
    stale = _luna_minted(PERSON, seconds=200)
    luna.refused.add(stale)
    _luna_folder(harry.home, token=stale)
    call = getattr(persons, "call", lambda *args, **kwargs: None)

    answer = call(person, "POST", "config/node/node001", b'{"config": {}}')
    assert len(luna.seen) == 2, "the call was not sent again, once"
    assert luna.seen == [stale, luna.issued[-1]], luna.seen
    assert [asked["username"] for asked in luna.signed] == [DELEGATE], luna.signed
    assert (harry.home / ".luna" / "token").read_text(encoding="utf-8") == luna.issued[-1]
    assert answer.status_code == 200


@posix_only
def test_a_plugin_s_call_refused_twice_asks_the_person_to_sign_in(
        client, headers, tmp_path, monkeypatch):
    """
    Luna refusing the fresh token as well: two sends, then 401 naming the sign-in route.
    """
    harry, luna, persons, person = _person_calls(client, headers, tmp_path, monkeypatch)
    stale = _luna_minted(PERSON, seconds=200)
    luna.refused.update({stale, _luna_minted(PERSON)})
    _luna_folder(harry.home, token=stale)
    call = getattr(persons, "call", lambda *args, **kwargs: None)

    raised = _raised(call, person, "GET", "whoami")
    assert len(luna.seen) == 2, luna.seen
    assert isinstance(raised, Unauthenticated), raised
    assert "sign in at Luna with POST /api/v1/luna/token" in raised.message


@posix_only
def test_a_plugin_s_call_with_a_token_just_signed_in_for_is_not_sent_again(
        client, headers, tmp_path, monkeypatch):
    """
    No kept token: the delegate signs harry in, and Luna refusing that token is Luna's
    answer, sent once and returned as it is.
    """
    _, luna, persons, person = _person_calls(client, headers, tmp_path, monkeypatch)
    luna.refused.add(_luna_minted(PERSON))
    call = getattr(persons, "call", lambda *args, **kwargs: None)

    answer = call(person, "GET", "whoami")
    assert len(luna.seen) == 1 and len(luna.signed) == 1, (luna.seen, luna.signed)
    assert answer.status_code == 401


@posix_only
def test_a_plugin_s_call_to_a_luna_that_does_not_answer_is_a_503(
        client, headers, tmp_path, monkeypatch):
    """
    Luna unreachable: ServiceUnavailable, as the route to Luna answers it.
    """
    harry, _, persons, person = _person_calls(client, headers, tmp_path, monkeypatch)
    _luna_folder(harry.home, token=_luna_minted(PERSON))
    tried = []

    def down(method, url, **kwargs):
        """
        Nobody answering.
        """
        tried.append(url)
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("trixd_plugin_luna.backend.requests.request", down)
    call = getattr(persons, "call", lambda *args, **kwargs: None)

    raised = _raised(call, person, "GET", "whoami")
    assert tried == [f"{FLOATING_BASE}/whoami"], tried
    assert isinstance(raised, ServiceUnavailable), raised
    assert "Luna is not answering" in raised.message


@posix_only
def test_may_answers_per_name_in_luna_s_own_words(client, headers, tmp_path, monkeypatch):
    """
    persons.may: one GET of harry's own _access, and per name what Luna would say: a node
    he cannot read is not available, one he reads without x is not permitted.
    """
    harry, luna, persons, person = _person_calls(
        client, headers, tmp_path, monkeypatch,
        node={"node001": "rwx", "node002": "r--", "node003": "rw-", "node005": "--x"})
    _luna_folder(harry.home, token=_luna_minted(PERSON))
    may = getattr(persons, "may", lambda *args, **kwargs: None)

    refused = may(person, "node", ["node001", "node002", "node003", "node004", "node005"], "x")
    assert luna.sent_to == [f"{FLOATING_BASE}/config/user/{PERSON}/_access"], luna.sent_to
    assert refused == {
        "node002": "operating node node002 is not permitted: you may read it",
        "node003": "operating node node003 is not permitted: you may read and change it",
        "node004": "node node004 is not available",
        "node005": "node node005 is not available",
    }, refused


@posix_only
def test_may_is_empty_when_every_name_is_held(client, headers, tmp_path, monkeypatch):
    """
    Every bit held: nothing refused.
    """
    harry, luna, persons, person = _person_calls(
        client, headers, tmp_path, monkeypatch, node={"node001": "rwx", "node002": "r-x"})
    _luna_folder(harry.home, token=_luna_minted(PERSON))
    may = getattr(persons, "may", lambda *args, **kwargs: None)

    refused = may(person, "node", ["node001", "node002"], "x")
    assert len(luna.sent_to) == 1, luna.sent_to
    assert refused == {}, refused


@posix_only
def test_may_reads_the_cluster_row_whatever_the_cluster_is_called(
        client, headers, tmp_path, monkeypatch):
    """
    Luna holds the cluster under its own name, mycluster here; it is asked for as
    "cluster", as Luna's gate names it in a refusal.
    """
    harry, luna, persons, person = _person_calls(
        client, headers, tmp_path, monkeypatch, cluster={"mycluster": "r--"})
    _luna_folder(harry.home, token=_luna_minted(PERSON))
    may = getattr(persons, "may", lambda *args, **kwargs: None)

    refused = may(person, "cluster", ["cluster"], "w")
    assert len(luna.sent_to) == 1, luna.sent_to
    assert refused == {"cluster": "changing or removing cluster cluster is not permitted: "
                                  "you may read it"}, refused
    assert may(person, "cluster", ["cluster"], "r") == {}


@posix_only
@pytest.mark.parametrize("status, message", [
    (403, "using the token of harry is not permitted: it is a delegate"),
    (404, "User harry is not available"),
])
def test_may_passes_luna_s_refusal_of_access_through(
        client, headers, tmp_path, monkeypatch, status, message):
    """
    Luna refusing GET .../_access: its status and its message, not a trixd failure.
    """
    harry, luna, persons, person = _person_calls(client, headers, tmp_path, monkeypatch)
    _luna_folder(harry.home, token=_luna_minted(PERSON))

    def refusing(method, url, headers=None, **kwargs):
        """
        Luna saying no to _access.
        """
        luna.sent_to.append(url)
        return SimpleNamespace(content=json.dumps({"message": message}).encode(),
                               status_code=status, headers={"content-type": "application/json"})

    monkeypatch.setattr("trixd_plugin_luna.backend.requests.request", refusing)
    may = getattr(persons, "may", lambda *args, **kwargs: None)

    raised = _raised(may, person, "node", ["node001"], "x")
    assert luna.sent_to == [f"{FLOATING_BASE}/config/user/{PERSON}/_access"], luna.sent_to
    assert getattr(raised, "status_code", None) == status, raised
    assert raised.message == message


@posix_only
def test_token_is_the_person_s_kept_luna_token(client, headers, tmp_path, monkeypatch):
    """
    persons.token: ~/.luna/token as it is, nothing signed in for and nothing sent.
    """
    harry, luna, persons, person = _person_calls(client, headers, tmp_path, monkeypatch)
    kept = _luna_minted(PERSON)
    _luna_folder(harry.home, token=kept)
    token = getattr(persons, "token", lambda *args, **kwargs: None)

    assert token(person) == kept
    assert luna.signed == [] and luna.seen == [], (luna.signed, luna.seen)


@posix_only
def test_token_is_signed_in_for_by_the_delegate_and_kept(
        client, headers, tmp_path, monkeypatch):
    """
    persons.token with nothing kept: the delegate signs harry in, that token is answered
    and kept where the command line keeps it.
    """
    harry, luna, persons, person = _person_calls(client, headers, tmp_path, monkeypatch)
    token = getattr(persons, "token", lambda *args, **kwargs: None)

    answered = token(person)
    assert [asked.get("on_behalf_of") for asked in luna.signed] == [PERSON], luna.signed
    assert answered == luna.issued[-1]
    assert (harry.home / ".luna" / "token").read_text(encoding="utf-8") == answered


@posix_only
def test_no_password_reaches_the_log(client, headers, tmp_path, monkeypatch):
    """
    A luna.ini the parser chokes on at its password line, a Luna refusal and a delegated
    sign-in: the passwords are nowhere in what the daemon says.
    """
    from tests.unit.test_daemon import _listening, _said
    from trixd.common import logging as trixlog

    harry = _person(client, headers, tmp_path, monkeypatch)
    _Luna(monkeypatch, {PERSON: PERSON_PASSWORD, DELEGATE: DELEGATE_PASSWORD})
    _delegate(monkeypatch)
    with _listening(trixlog.get()) as records:
        _luna_folder(harry.home, luna_ini=f"[API]\nusername = {PERSON}\n{PERSON_PASSWORD}\n")
        client.get("/api/v1/luna/whoami", headers=harry.headers)
        client.post("/api/v1/luna/token", headers=harry.headers,
                    json={"username": PERSON, "password": "not-" + PERSON_PASSWORD})
    assert any("cannot be read" in record.getMessage() for record in records), (
        "the broken luna.ini was not said, so this proves nothing: " + _said(records))
    for password in (PERSON_PASSWORD, DELEGATE_PASSWORD):
        assert password not in _said(records)


def test_delegate_sections_are_read_alike_and_are_not_unknown(tmp_path):
    """
    Every [DELEGATE:<component>] is read the same way, as that component's account, with
    no component known by name; none is named at start as a section trixd does not read,
    while a key the section does not take still is.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[API]\nUSERNAME = tester\nPASSWORD = secret\n"
                   f"[DELEGATE:LUNA]\nUSERNAME = {DELEGATE}\nPASSWORD = {DELEGATE_PASSWORD}\n"
                   "ENDPOINT = luna-float.test:7050\n"
                   "[DELEGATE:OTHER]\nUSERNAME = them\nPASSWORD = theirs\nTOKEN = x\n",
                   encoding="utf-8")
    found = []
    loaded = configuration.load(str(ini), found)
    assert not [said for said in found if "has a [DELEGATE" in said], found
    delegates = getattr(loaded, "delegates", None)
    assert delegates is not None, "trixd.ini's delegate accounts are not read"
    assert delegates == {"LUNA": (DELEGATE, DELEGATE_PASSWORD, "luna-float.test:7050"),
                         "OTHER": ("them", "theirs", "")}
    assert [said for said in found if "[DELEGATE:OTHER] TOKEN" in said], found
