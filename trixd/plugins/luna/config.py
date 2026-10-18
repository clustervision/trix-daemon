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
Luna's own luna.ini, read in place: the one file an engineer changes for Luna, so trixd
never holds a second copy that can drift.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import threading
from configparser import RawConfigParser

from trixd.common import logging as trixlog

LOGGER = trixlog.get("luna.config")

# The same order luna2-cli reads: whoever is running this may have logged in as
# themselves, and that file wins over the controller's own.
USER_LUNA_INI: str = "~/.luna/luna.ini"
DEFAULT_LUNA_INI: str = "/trinity/local/luna/daemon/config/luna.ini"
LUNA_INI_ENV: str = "TRIXD_LUNA_INI"
TIMEOUT: int = 20
PROTOCOLS: tuple = ("https", "http")

# The personal file last said to be in use, so it is said once and not on every check.
_said_personal: str = ""


def _say_personal(path: str) -> None:
    """
    Say, once for each path, that a personal file is being followed.

    Which file this is decides which cluster the daemon reads. A command-line login as root
    leaves one behind, and the daemon then takes that cluster's nodes and renders this
    controller's genders and slurm files from them. It is said once rather than on every
    check, because the path is re-read whenever the file's timestamp moves.
    """
    global _said_personal
    if _said_personal == path:
        return
    _said_personal = path
    LOGGER.warning("following %s rather than %s: a personal login file is deciding which "
                   "cluster this daemon reads", path, DEFAULT_LUNA_INI)


def chosen_path() -> str:
    """
    The luna.ini this process follows: what the environment names, else a personal login
    where there is one, else the controller's own. Naming the controller's file when
    neither exists puts the expected location in the error.
    """
    named = os.environ.get(LUNA_INI_ENV)
    if named:
        return named
    personal = os.path.expanduser(USER_LUNA_INI)
    if os.path.isfile(personal):
        _say_personal(personal)
        return personal
    return DEFAULT_LUNA_INI


def _bool(value: str) -> bool:
    """
    A yes or no as luna.ini writes it.
    """
    return str(value or "").strip().lower() in ("1", "yes", "true", "on")


class LunaConfig:
    """
    The [API] section of Luna's luna.ini, re-read whenever the file changes.
    """

    def __init__(self, path: str = "") -> None:
        """
        Nothing is read until a value is asked for.
        """
        self.path = path or chosen_path()
        self.lock = threading.Lock()
        self._stamp = None
        self._values: dict = {}
        self._secret: str = ""

    def _values_now(self) -> dict:
        """
        The current values, read again only when the file's size or time changed.
        """
        with self.lock:
            try:
                stat = os.stat(self.path)
            except OSError:
                if self._stamp != "missing":
                    LOGGER.error("no luna.ini at %s; trixd follows Luna's own settings",
                                 self.path)
                self._stamp = "missing"
                return self._values
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._stamp:
                return self._values
            parser = RawConfigParser()
            parser.read(self.path)
            api = dict(parser.items("API")) if parser.has_section("API") else {}
            protocol = str(api.get("protocol", "https")).strip().lower() or "https"
            if protocol not in PROTOCOLS:
                LOGGER.error("luna.ini PROTOCOL is %s; using https", protocol)
                protocol = "https"
            values = {"endpoint": str(api.get("endpoint", "")).strip() or "localhost:7050",
                      "protocol": protocol,
                      "verify": _bool(api.get("verify_certificate", "no")),
                      "secret": str(api.get("secret_key", "")).strip(),
                      "username": str(api.get("username", "")).strip(),
                      "password": str(api.get("password", "")).strip()}
            if self._values and values != self._values:
                LOGGER.warning("luna.ini changed: Luna is %s://%s", protocol, values["endpoint"])
            self._values, self._stamp = values, stamp
            return values

    @property
    def endpoint(self) -> str:
        """
        Where Luna answers, as host:port.
        """
        return self._values_now().get("endpoint", "localhost:7050")

    @property
    def protocol(self) -> str:
        """
        https or http, as all.yml chose for Luna, and so for trixd.
        """
        return self._values_now().get("protocol", "https")

    @property
    def verify(self) -> bool:
        """
        Whether Luna's certificate is checked.
        """
        return bool(self._values_now().get("verify", False))

    @property
    def username(self) -> str:
        """
        The account Luna's own ini names, which is Luna's administrator.
        """
        return self._values_now().get("username", "")

    @property
    def password(self) -> str:
        """
        That account's password, read from Luna's file and never copied into trixd's.
        """
        return self._values_now().get("password", "")

    @property
    def base(self) -> str:
        """
        Luna's address with its protocol.
        """
        return f"{self.protocol}://{self.endpoint}"

    @property
    def secret(self) -> str:
        """
        The token secret both daemons share. An empty one, as a fresh pip install ships,
        never replaces a good one: the installer writes the real one back moments later.
        """
        found = self._values_now().get("secret", "")
        if found and found != self._secret:
            if self._secret:
                LOGGER.warning("the shared secret in %s changed; tokens signed with the "
                               "old one are refused from now on", self.path)
            self._secret = found
        return self._secret


_config: LunaConfig = None


def current() -> LunaConfig:
    """
    The luna.ini this process follows; a new path in the environment starts a new one.
    """
    global _config
    path = chosen_path()
    if _config is None or _config.path != path:
        _config = LunaConfig(path)
    return _config
