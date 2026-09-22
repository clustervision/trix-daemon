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

__author__      = 'Sumit Sharma'
__copyright__   = 'Copyright 2026, TrinityX Project'
__license__     = 'GPL'
__version__     = '1.0'
__maintainer__  = 'Sumit Sharma'
__email__       = 'sumit.sharma@clustervision.com'
__status__      = 'Development'

import os
import shutil

import pytest
from fastapi.testclient import TestClient

from trix.app import create_app
from trix.common import settings as configuration
from trix.core.lifespan import start
from trix.core.security import keys

PLUGIN_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'trix', 'plugins')


@pytest.fixture()
def rig(tmp_path):
    """A configured daemon: its own database, its own keypair, its own plugins."""
    plugins = tmp_path / 'plugins'
    shutil.copytree(PLUGIN_SOURCE, plugins)
    trust = tmp_path / 'trust.d'
    trust.mkdir()

    ini = tmp_path / 'trix.ini'
    ini.write_text(
        '[LOGGER]\nLEVEL = warning\nLOGFILE =\n'
        '[API]\nBIND =\nINSECURE_BIND = 127.0.0.1:0\n'
        'USERNAME = tester\nPASSWORD = secret\nEXPIRY = 1h\n'
        f'[TOKENS]\nPRIVATE_KEY = {tmp_path / "trix.ed25519"}\nKEY_ID = trix-test\n'
        f'TRUST = {trust}\nISSUER = trix-daemon\n'
        f'[DATABASE]\nURL = sqlite:///{tmp_path / "trix.db"}\n'
        '[RESOLVE]\nUSERS = local\nGROUPS = local\nCACHE = 0s\n'
        f'[PLUGINS]\nDIRECTORY = {plugins}\nENABLED = directory, services\n',
        encoding='utf-8')

    config = configuration.set_current(configuration.load(str(ini)))
    keys.generate(config.tokens.private_key)

    application = create_app(with_lifespan=False)
    start(application)
    return application


@pytest.fixture()
def client(rig):
    """A client speaking to that daemon."""
    return TestClient(rig)


@pytest.fixture()
def token(client):
    """A token for the seeded administrator."""
    answer = client.post('/api/v1/auth/token',
                         json={'username': 'tester', 'password': 'secret'})
    assert answer.status_code == 201, answer.text
    return answer.json()['token']


@pytest.fixture()
def headers(token):
    """Ready to pass to a request."""
    return {'x-access-tokens': token}
