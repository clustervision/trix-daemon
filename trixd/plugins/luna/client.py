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

import requests
import urllib3

from trixd.common import logging as trixlog
from trixd.common.constants import TOKEN_HEADER
from trixd.common.errors import ServiceUnavailable, Unauthenticated
from trixd.plugins.luna import config as luna_config
from trixd.plugins.luna import credentials

LOGGER = trixlog.get("luna")

urllib3.disable_warnings()


class LunaClient:
    """
    A thin client. It never models a Luna object, it only carries one.
    """

    def __init__(self) -> None:
        """
        No connection yet: the token is fetched on first use.
        """
        self.session = requests.Session()

    @property
    def base(self) -> str:
        """
        The address Luna answers on, with its protocol, as Luna's own luna.ini says.
        """
        return luna_config.current().base

    def _headers(self, renew: bool = False) -> dict:
        """
        The token header Luna expects. The one cache lives in credentials, so this asks
        for a token rather than keeping a second one of its own.
        """
        try:
            return {TOKEN_HEADER: credentials.get_token(renew=renew)}
        except Unauthenticated as error:
            raise ServiceUnavailable(f"no token for Luna: {error.message}") from None

    def request(self, method: str, uri: str, payload=None, params: dict = None):
        """
        One call to Luna, refreshing the token once if it has expired.
        """
        config = luna_config.current()
        url = f'{config.base}/{uri.lstrip("/")}'
        for attempt in (1, 2):
            try:
                answer = self.session.request(
                    method, url, headers=self._headers(renew=attempt == 2), json=payload,
                    params=params, timeout=luna_config.TIMEOUT, verify=config.verify)
            except requests.RequestException as error:
                raise ServiceUnavailable(f"Luna is not answering: {error}") from None
            if answer.status_code in (401, 403) and attempt == 1:
                # The token was refused: ask for a new one rather than the held one.
                continue
            if answer.status_code == 404:
                return None
            if answer.status_code >= 400:
                raise ServiceUnavailable(
                    f"Luna answered {answer.status_code} for {uri}")
            if not answer.content:
                return None
            try:
                return answer.json()
            except ValueError:
                return answer.text
        return None

    def get(self, uri: str, params: dict = None):
        """
        A GET against Luna, decoded.
        """
        return self.request("GET", uri, params=params)

    def post(self, uri: str, payload=None):
        """
        A POST against Luna, decoded.
        """
        return self.request("POST", uri, payload=payload)

    def nodes(self) -> dict:
        """
        Every node, keyed by name.
        """
        answer = self.get("config/node") or {}
        return (answer.get("config") or {}).get("node") or {}

    def node(self, name: str) -> dict:
        """
        One node by name, or None.
        """
        answer = self.get(f"config/node/{name}") or {}
        found = (answer.get("config") or {}).get("node") or {}
        return found.get(name)

    def cluster(self) -> dict:
        """
        The cluster row, which carries the security flag the node token turns on.
        """
        answer = self.get("config/cluster") or {}
        return (answer.get("config") or {}).get("cluster") or {}

    def enforcing_security(self) -> bool:
        """
        Whether Luna is enforcing cluster security, and so whether we must too.

        An answer this cannot read means enforcing: the flag decides whether a node has to
        present a measurement, and reading "off" from a 404 or a moved shape would turn
        attestation off cluster-wide with nothing reporting it.
        """
        held = self.cluster()
        if not isinstance(held, dict) or "security" not in held:
            LOGGER.warning("Luna did not say whether the cluster enforces security, so this "
                           "daemon assumes it does; a node must present a measurement")
            return True
        return bool(held["security"])

    def groups(self) -> dict:
        """
        Every group Luna holds.
        """
        answer = self.get("config/group") or {}
        return (answer.get("config") or {}).get("group") or {}

    def networks(self) -> dict:
        """
        Every network Luna holds.
        """
        answer = self.get("config/network") or {}
        return (answer.get("config") or {}).get("network") or {}

    def table_hashes(self) -> dict:
        """
        Luna's per-table hashes, the fallback change trigger. Luna answers only on
        the HA master, so empty is normal here and is not "nothing changed".
        """
        answer = self.get("table/hashes") or {}
        return (answer.get("table") or {}).get("hashes") or {}


_client: LunaClient = LunaClient()


def current() -> LunaClient:
    """
    The Luna client for this process.
    """
    return _client
