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
A daemon built entirely in a temporary directory, with no /trinity tree needed.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import shutil

import pytest
from fastapi.testclient import TestClient

from trixd.app import create_app
from trixd.common import settings as configuration
from trixd.core import tasks
from trixd.core.lifespan import start
from trixd.core.plugins.registry import current as registry
from trixd.core.render import triggers
from trixd.core.render.engine import current as renders
from trixd.core.replication import applied, outbox
from trixd.core.replication.sweep import current as sweep
from trixd.core.security import keys
from trixd.plugins.luna import hashes
from trixd.plugins.luna.config import LUNA_INI_ENV

PLUGIN_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "trixd", "plugins")


# What the installer writes into the shared vault file: 32 hexadecimal characters.
SHARED_SECRET: str = "004f2af45d3a4e161a7dd2d17fdae47f"


def build(tmp_path, algorithm: str = "HS256", monkeypatch=None):
    """
    Write a configuration and bring a daemon up against it.

    `monkeypatch` is how the Luna ini reaches the environment. Assigning it directly left
    the variable set for whatever ran next: the autouse fixture below clears it before each
    test, so the leak stayed inside the run, but a test asking what happens with no Luna
    configuration depended on nothing having built a rig first.
    """
    plugins = tmp_path / "plugins"
    if not plugins.exists():
        shutil.copytree(PLUGIN_SOURCE, plugins)
    trust = tmp_path / "trust.d"
    trust.mkdir(exist_ok=True)
    # Luna's own luna.ini, which the luna plugin reads for Luna's address and secret.
    luna_ini = tmp_path / "luna.ini"
    luna_ini.write_text(f"[API]\nENDPOINT = luna.test:7050\nPROTOCOL = https\n"
                        f"VERIFY_CERTIFICATE = no\nSECRET_KEY = {SHARED_SECRET}\n",
                        encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.setenv(LUNA_INI_ENV, str(luna_ini))
    else:
        os.environ[LUNA_INI_ENV] = str(luna_ini)

    ini = tmp_path / "trixd.ini"
    ini.write_text(
        "[LOGGER]\nLEVEL = warning\nLOGFILE =\n"
        "[API]\nBIND =\nINSECURE_BIND = 127.0.0.1:0\n"
        "USERNAME = tester\nPASSWORD = secret\nEXPIRY = 1h\n"
        f"[TOKENS]\nALGORITHM = {algorithm}\nSECRET_KEY = {SHARED_SECRET}\n"
        f'PRIVATE_KEY = {tmp_path / "trixd.ed25519"}\nKEY_ID = trixd-test\n'
        f"TRUST = {trust}\nISSUER = trixd\n"
        f'[DATABASE]\nURL = sqlite:///{tmp_path / "trixd.db"}\n'
        "[AUTH]\nCHAIN = local\nCACHE = 0s\n"
        f"[PLUGINS]\nDIRECTORY = {plugins}\nSITE_DIRECTORY = {tmp_path / 'site-plugins'}\n"
        "ENABLED = luna, directory, services\n",
        encoding="utf-8")

    config = configuration.set_current(configuration.load(str(ini)))
    if not config.tokens.shared and not os.path.isfile(config.tokens.private_key):
        keys.generate(config.tokens.private_key)

    application = create_app(with_lifespan=False)
    start(application)
    return application


@pytest.fixture(autouse=True)
def _own_luna_ini(monkeypatch):
    """
    Each test sees only the luna.ini it writes, and no test starts the hash timer
    unless it says so: both are put back afterwards.
    """
    monkeypatch.delenv(LUNA_INI_ENV, raising=False)
    monkeypatch.setattr(hashes, "INTERVAL", 0)
    yield


@pytest.fixture(autouse=True)
def _own_process_state():
    """
    The state that lives in the process rather than in a rig, put back afterwards.

    These are module-level singletons, so one test's leavings are in the next test's way:
    that is how a test passes alone and fails in a full run, and the other way round.
    Building a rig does not clear any of them, because none belongs to a database or a
    configuration file.

    The plugin registry is the one that actually bites. A test that loads a plugin of its
    own leaves it registered, so its hooks go on being dispatched into the next test's run -
    which shows up as a hook answering for a plugin that test never loaded. Taken out the
    way `unload` does, minus the route table, which is a fresh app per test anyway.
    """
    standing = set(registry().plugins)
    yield
    for name in set(registry().plugins) - standing:
        tasks.unregister(name)
        triggers.forget(name)
        registry().plugins.pop(name, None)
    renders().forget_all()
    applied.forget_all()
    sweep().stubborn.clear()
    outbox._failures.clear()
    outbox._open.clear()


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """
    A configured daemon: its own database, the shared secret, its own plugins.
    """
    return build(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def keypair_rig(tmp_path, monkeypatch):
    """
    The same daemon with the optional keypair algorithm selected instead.
    """
    return build(tmp_path, algorithm="EdDSA", monkeypatch=monkeypatch)


@pytest.fixture()
def client(rig):
    """
    A client speaking to that daemon.
    """
    return TestClient(rig)


@pytest.fixture()
def token(client):
    """
    A token for the ini account, which is rootus.
    """
    answer = client.post("/api/v1/auth/token",
                         json={"username": "tester", "password": "secret"})
    assert answer.status_code == 201, answer.text
    return answer.json()["token"]


@pytest.fixture()
def headers(token):
    """
    Ready to pass to a request.
    """
    return {"x-access-tokens": token}
