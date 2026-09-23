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
Reading Luna objects by name over Luna's own API.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import threading

import requests
import urllib3

from trix.common import logging as trixlog
from trix.common.constants import TOKEN_HEADER
from trix.common.errors import ServiceUnavailable
from trix.common.settings import current as settings

LOGGER = trixlog.get('luna')

urllib3.disable_warnings()


class LunaClient:
    """A thin client. It never models a Luna object, it only carries one."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.lock = threading.Lock()
        self.token: str = ''

    @property
    def base(self) -> str:
        config = settings().luna
        return f'{config.protocol}://{config.endpoint}'

    def _authenticate(self) -> str:
        """Fetch a token from Luna.

        Interim: Luna still authenticates with a username and a password, so the
        credentials come from trix.ini. TRIX-2085 replaces this with a service
        principal, and only this method changes when it does.
        """
        config = settings().luna
        try:
            answer = self.session.post(
                f'{self.base}/token',
                json={'username': config.username, 'password': config.password},
                timeout=config.timeout, verify=config.verify_certificate)
        except requests.RequestException as error:
            raise ServiceUnavailable(f'Luna is not answering: {error}') from None
        if answer.status_code >= 400:
            raise ServiceUnavailable(f'Luna refused our credentials: {answer.status_code}')
        token = (answer.json() or {}).get('token', '')
        if not token:
            raise ServiceUnavailable('Luna returned no token')
        return token

    def _headers(self) -> dict:
        with self.lock:
            if not self.token:
                self.token = self._authenticate()
            return {TOKEN_HEADER: self.token}

    def request(self, method: str, uri: str, payload=None, params: dict = None):
        """One call to Luna, refreshing the token once if it has expired."""
        config = settings().luna
        url = f'{self.base}/{uri.lstrip("/")}'
        for attempt in (1, 2):
            try:
                answer = self.session.request(
                    method, url, headers=self._headers(), json=payload, params=params,
                    timeout=config.timeout, verify=config.verify_certificate)
            except requests.RequestException as error:
                raise ServiceUnavailable(f'Luna is not answering: {error}') from None
            if answer.status_code in (401, 403) and attempt == 1:
                with self.lock:
                    self.token = ''
                continue
            if answer.status_code == 404:
                return None
            if answer.status_code >= 400:
                raise ServiceUnavailable(
                    f'Luna answered {answer.status_code} for {uri}')
            if not answer.content:
                return None
            try:
                return answer.json()
            except ValueError:
                return answer.text
        return None

    def get(self, uri: str, params: dict = None):
        return self.request('GET', uri, params=params)

    def post(self, uri: str, payload=None):
        return self.request('POST', uri, payload=payload)

    def nodes(self) -> dict:
        """Every node, keyed by name."""
        answer = self.get('config/node') or {}
        return (answer.get('config') or {}).get('node') or {}

    def node(self, name: str) -> dict:
        """One node by name, or None."""
        answer = self.get(f'config/node/{name}') or {}
        found = (answer.get('config') or {}).get('node') or {}
        return found.get(name)

    def groups(self) -> dict:
        answer = self.get('config/group') or {}
        return (answer.get('config') or {}).get('group') or {}

    def networks(self) -> dict:
        answer = self.get('config/network') or {}
        return (answer.get('config') or {}).get('network') or {}

    def table_hashes(self) -> dict:
        """Luna's per-table hashes, used as the fallback change trigger.

        Luna answers this only on the HA master, so an empty answer is normal on a
        single controller and must not be read as "nothing changed".
        """
        answer = self.get('table/hashes') or {}
        return (answer.get('table') or {}).get('hashes') or {}


_client: LunaClient = LunaClient()


def current() -> LunaClient:
    """The Luna client for this process."""
    return _client
