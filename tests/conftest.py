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
from trixd.core.lifespan import start
from trixd.core.security import keys
from trixd.plugins.luna import hashes
from trixd.plugins.luna.config import LUNA_INI_ENV

PLUGIN_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "trixd", "plugins")


# What the installer writes into the shared vault file: 32 hexadecimal characters.
SHARED_SECRET: str = "004f2af45d3a4e161a7dd2d17fdae47f"


def build(tmp_path, algorithm: str = "HS256"):
    """
    Write a configuration and bring a daemon up against it.
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
    os.environ[LUNA_INI_ENV] = str(luna_ini)
    # The hash fallback is a timer; a test that wants it says so.
    os.environ[hashes.INTERVAL_ENV] = "0s"

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
    Each test sees only the luna.ini it writes: the environment is put back afterwards.
    """
    monkeypatch.delenv(LUNA_INI_ENV, raising=False)
    yield


@pytest.fixture()
def rig(tmp_path):
    """
    A configured daemon: its own database, the shared secret, its own plugins.
    """
    return build(tmp_path)


@pytest.fixture()
def keypair_rig(tmp_path):
    """
    The same daemon with the optional keypair algorithm selected instead.
    """
    return build(tmp_path, algorithm="EdDSA")


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
