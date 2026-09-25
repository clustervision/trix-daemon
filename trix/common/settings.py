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
Typed settings read once from trix.ini. Importing this module has no side effects.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import re
from configparser import RawConfigParser

from pydantic import BaseModel, Field

from trix.common.constants import (DEFAULT_TOKEN_ALGORITHM, TOKEN_ALGORITHM_SHARED,
                                   TOKEN_ALGORITHMS)

DEFAULT_CONFIG: str = "/trinity/local/trix/daemon/config/trix.ini"

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_FACTOR = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 3600}


def seconds(value: str, fallback: int = 3600) -> int:
    """
    Turn 30s, 5m, 1h or a bare number into seconds.
    """
    found = _DURATION.match(str(value or ""))
    if not found:
        return fallback
    return int(found.group(1)) * _FACTOR[found.group(2).lower()]


class LoggerSettings(BaseModel):
    level: str = "info"
    logfile: str = "/var/log/trix/trix-daemon.log"


class ApiSettings(BaseModel):
    bind: str = "0.0.0.0:8051"
    insecure_bind: str = "127.0.0.1:8050"
    certfile: str = ""
    keyfile: str = ""
    expiry: int = 3600
    username: str = ""
    password: str = ""


class TokenSettings(BaseModel):
    algorithm: str = DEFAULT_TOKEN_ALGORITHM
    secret_key: str = ""
    secret_file: str = "/trinity/local/etc/ssl/secret.key"
    private_key: str = "/trinity/local/etc/ssl/trix.ed25519"
    key_id: str = "trix-1"
    trust: str = "/trinity/local/etc/ssl/trust.d"
    issuer: str = "trix-daemon"

    @property
    def shared(self) -> bool:
        """
        Whether tokens are signed with a secret both daemons hold.
        """
        return self.algorithm == TOKEN_ALGORITHM_SHARED


class LunaSettings(BaseModel):
    endpoint: str = "controller.cluster:7050"
    protocol: str = "http"
    verify_certificate: bool = False
    username: str = ""
    password: str = ""
    timeout: int = 20


class DatabaseSettings(BaseModel):
    url: str = "sqlite:////trinity/local/trix/daemon/common/trix.db"


class ResolveSettings(BaseModel):
    users: list = Field(default_factory=lambda: ["local"])
    groups: list = Field(default_factory=lambda: ["local"])
    cache: int = 60


class ReplicationSettings(BaseModel):
    peers: list = Field(default_factory=list)
    interval: int = 300
    enabled: bool = True


class TriggerSettings(BaseModel):
    luna_hashes: int = 60


class PluginSettings(BaseModel):
    directory: str = "/trinity/local/trix/daemon/plugins"
    enabled: list = Field(default_factory=list)


class Settings(BaseModel):
    """
    Everything the daemon reads from its configuration file.
    """

    config_file: str = DEFAULT_CONFIG
    logger: LoggerSettings = Field(default_factory=LoggerSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    tokens: TokenSettings = Field(default_factory=TokenSettings)
    luna: LunaSettings = Field(default_factory=LunaSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    resolve: ResolveSettings = Field(default_factory=ResolveSettings)
    replication: ReplicationSettings = Field(default_factory=ReplicationSettings)
    triggers: TriggerSettings = Field(default_factory=TriggerSettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)


def _csv(value: str) -> list:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _algorithm(value: str) -> str:
    """
    Canonicalise the configured algorithm, refusing one this daemon cannot sign with.
    """
    wanted = str(value or "").strip()
    if not wanted:
        return DEFAULT_TOKEN_ALGORITHM
    for known in TOKEN_ALGORITHMS:
        if wanted.lower() == known.lower():
            return known
    raise ValueError(f"[TOKENS] ALGORITHM is {wanted}, which is not one of "
                     f"{', '.join(TOKEN_ALGORITHMS)}")


def _secret(inline: str, path: str) -> str:
    """
    The shared secret: what the ini states, else the vault file luna.ini is built from.
    """
    if inline:
        return inline.strip()
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    return ""


def _bool(value: str) -> bool:
    return str(value or "").strip().lower() in ("1", "yes", "true", "on")


def load(path: str = "") -> Settings:
    """
    Read the ini into a Settings object, falling back to the defaults above.
    """
    path = path or os.environ.get("TRIX_CONFIG") or DEFAULT_CONFIG
    parser = RawConfigParser()
    parser.read(path)

    def get(section: str, option: str, fallback: str = "") -> str:
        if parser.has_section(section) and parser.has_option(section, option):
            return parser.get(section, option)
        return fallback

    data = Settings(config_file=path)
    data.logger = LoggerSettings(
        level=get("LOGGER", "LEVEL", data.logger.level),
        logfile=get("LOGGER", "LOGFILE", data.logger.logfile))
    data.api = ApiSettings(
        bind=get("API", "BIND", data.api.bind),
        insecure_bind=get("API", "INSECURE_BIND", data.api.insecure_bind),
        certfile=get("API", "CERTFILE", ""),
        keyfile=get("API", "KEYFILE", ""),
        expiry=seconds(get("API", "EXPIRY", "1h")),
        username=get("API", "USERNAME", ""),
        password=get("API", "PASSWORD", ""))
    secret_file = get("TOKENS", "SECRET_FILE", data.tokens.secret_file)
    data.tokens = TokenSettings(
        algorithm=_algorithm(get("TOKENS", "ALGORITHM", DEFAULT_TOKEN_ALGORITHM)),
        secret_key=_secret(get("TOKENS", "SECRET_KEY", ""), secret_file),
        secret_file=secret_file,
        private_key=get("TOKENS", "PRIVATE_KEY", data.tokens.private_key),
        key_id=get("TOKENS", "KEY_ID", data.tokens.key_id),
        trust=get("TOKENS", "TRUST", data.tokens.trust),
        issuer=get("TOKENS", "ISSUER", data.tokens.issuer))
    data.luna = LunaSettings(
        endpoint=get("LUNA", "ENDPOINT", data.luna.endpoint),
        protocol=get("LUNA", "PROTOCOL", data.luna.protocol),
        verify_certificate=_bool(get("LUNA", "VERIFY_CERTIFICATE", "no")),
        username=get("LUNA", "USERNAME", ""),
        password=get("LUNA", "PASSWORD", ""),
        timeout=seconds(get("LUNA", "TIMEOUT", "20s"), 20))
    data.database = DatabaseSettings(url=get("DATABASE", "URL", data.database.url))
    data.resolve = ResolveSettings(
        users=_csv(get("RESOLVE", "USERS", "local")),
        groups=_csv(get("RESOLVE", "GROUPS", "local")),
        cache=seconds(get("RESOLVE", "CACHE", "60s"), 60))
    data.replication = ReplicationSettings(
        peers=_csv(get("REPLICATION", "PEERS", "")),
        interval=seconds(get("REPLICATION", "INTERVAL", "300s"), 300),
        enabled=_bool(get("REPLICATION", "ENABLED", "yes")))
    data.triggers = TriggerSettings(
        luna_hashes=seconds(get("TRIGGERS", "LUNA_HASHES", "60s"), 60))
    data.plugins = PluginSettings(
        directory=get("PLUGINS", "DIRECTORY", data.plugins.directory),
        enabled=_csv(get("PLUGINS", "ENABLED", "")))
    return data


_current: Settings = Settings()


def set_current(value: Settings) -> Settings:
    """
    Hold the settings the running daemon was started with.
    """
    global _current
    _current = value
    return _current


def current() -> Settings:
    """
    The settings the running daemon was started with.
    """
    return _current
