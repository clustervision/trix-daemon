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
Typed settings read once from trixd.ini. Importing this module has no side effects.
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

from trixd.common.constants import (DEFAULT_TOKEN_ALGORITHM, TOKEN_ALGORITHM_SHARED,
                                   TOKEN_ALGORITHMS)

DEFAULT_CONFIG: str = "/trinity/local/trixd/etc/trixd.ini"

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
_FACTOR = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(value: str, fallback: int = 3600, bare: int = 1) -> int:
    """
    Turn 30s, 5m, 1h or 2d into seconds; a bare number counts in units of bare seconds.
    Empty is the fallback, and anything else unreadable is an error.
    """
    text = str(value or "").strip()
    if not text:
        return fallback
    found = _DURATION.match(text)
    if not found:
        raise ValueError(f"{text} is not a duration such as 30s, 5m, 1h or 2d")
    unit = found.group(2).lower()
    return int(found.group(1)) * (_FACTOR[unit] if unit else bare)


class LoggerSettings(BaseModel):
    """
    Where the daemon logs, and how loudly.
    """
    level: str = "info"
    logfile: str = "/var/log/trixd/trixd.log"


class ApiSettings(BaseModel):
    """
    The two listeners, the certificate and the ini account.
    """
    bind: str = "0.0.0.0:8050"
    insecure_bind: str = "127.0.0.1:8051"
    certfile: str = ""
    keyfile: str = ""
    expiry: int = 3600
    username: str = ""
    password: str = ""


class TokenSettings(BaseModel):
    """
    What tokens are signed with, and by which algorithm.
    """
    algorithm: str = DEFAULT_TOKEN_ALGORITHM
    secret_key: str = ""
    secret_file: str = "/trinity/local/etc/ssl/secret.key"
    private_key: str = "/trinity/local/etc/ssl/trixd.ed25519"
    key_id: str = "trixd-1"
    trust: str = "/trinity/local/etc/ssl/trust.d"
    issuer: str = "trixd"

    @property
    def shared(self) -> bool:
        """
        Whether tokens are signed with a secret both daemons hold.
        """
        return self.algorithm == TOKEN_ALGORITHM_SHARED


class LunaSettings(BaseModel):
    """
    How to reach Luna, and as whom.
    """
    endpoint: str = "controller.cluster:7050"
    protocol: str = "http"
    verify_certificate: bool = False
    username: str = ""
    password: str = ""
    timeout: int = 20


class DatabaseSettings(BaseModel):
    """
    Where the store lives.
    """
    url: str = "sqlite:////trinity/local/trixd/var/trixd.db"


class AuthSettings(BaseModel):
    """
    The chain of sources, and what each one needs.
    """
    chain: list = Field(default_factory=lambda: ["local", "pam"])
    cache: int = 60
    pam_service: str = "login"
    ldap_uri: str = ""
    ldap_base: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_attribute: str = "uid"


class ServicesSettings(BaseModel):
    """
    What runs a unit action on this controller.
    """
    command: str = "/usr/bin/systemctl"


class ReplicationSettings(BaseModel):
    """
    The peers to reconcile with, and how often.
    """
    peers: list = Field(default_factory=list)
    interval: int = 300
    enabled: bool = True
    ca_file: str = "/trinity/local/etc/ssl/cluster-ca.crt"


class TriggerSettings(BaseModel):
    """
    How often the fallback asks Luna what changed.
    """
    luna_hashes: int = 60


class AuditSettings(BaseModel):
    """
    How long the audit trail keeps a row.
    """
    keep: int = 90 * 86400


class PluginSettings(BaseModel):
    """
    Where plugins live, and which of them to load.
    """
    directory: str = "/trinity/local/trixd/daemon/plugins"
    # A site's own plugins: outside the package, so an upgrade leaves them alone.
    site_directory: str = "/trinity/local/trixd/plugins"
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
    auth: AuthSettings = Field(default_factory=AuthSettings)
    services: ServicesSettings = Field(default_factory=ServicesSettings)
    replication: ReplicationSettings = Field(default_factory=ReplicationSettings)
    triggers: TriggerSettings = Field(default_factory=TriggerSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)


def _csv(value: str) -> list:
    """
    A comma separated setting as a list, without the blanks.
    """
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
    """
    Accept yes, true, on and 1 as true, anything else as false.
    """
    return str(value or "").strip().lower() in ("1", "yes", "true", "on")


def load(path: str = "") -> Settings:
    """
    Read the ini into a Settings object, falling back to the defaults above.
    """
    path = path or os.environ.get("TRIXD_CONFIG") or DEFAULT_CONFIG
    if not os.path.isfile(path):
        raise ValueError(f"{path} does not exist")
    parser = RawConfigParser()
    parser.read(path)

    def get(section: str, option: str, fallback: str = "") -> str:
        """
        One value from the ini, or the fallback when it says nothing.
        """
        if parser.has_section(section) and parser.has_option(section, option):
            return parser.get(section, option)
        return fallback

    def duration(section: str, option: str, default: str, bare: int = 1) -> int:
        """
        One duration from the ini; an unreadable one names itself and stops the daemon.
        """
        try:
            return seconds(get(section, option, default), seconds(default), bare)
        except ValueError as error:
            raise ValueError(f"[{section}] {option}: {error}") from None

    data = Settings(config_file=path)
    data.logger = LoggerSettings(
        level=get("LOGGER", "LEVEL", data.logger.level),
        logfile=get("LOGGER", "LOGFILE", data.logger.logfile))
    data.api = ApiSettings(
        bind=get("API", "BIND", data.api.bind),
        insecure_bind=get("API", "INSECURE_BIND", data.api.insecure_bind),
        certfile=get("API", "CERTFILE", ""),
        keyfile=get("API", "KEYFILE", ""),
        # A bare EXPIRY counts in hours, as luna.ini's does.
        expiry=duration("API", "EXPIRY", "1h", bare=3600),
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
        timeout=duration("LUNA", "TIMEOUT", "20s"))
    data.database = DatabaseSettings(url=get("DATABASE", "URL", data.database.url))
    data.auth = AuthSettings(
        chain=_csv(get("AUTH", "CHAIN", "local, pam")),
        cache=duration("AUTH", "CACHE", "60s"),
        pam_service=get("AUTH_PAM", "SERVICE", "login"),
        ldap_uri=get("AUTH_LDAP", "URI", ""),
        ldap_base=get("AUTH_LDAP", "BASE", ""),
        ldap_bind_dn=get("AUTH_LDAP", "BIND_DN", ""),
        ldap_bind_password=get("AUTH_LDAP", "BIND_PASSWORD", ""),
        ldap_user_attribute=get("AUTH_LDAP", "USER_ATTRIBUTE", "uid"))
    data.services = ServicesSettings(
        command=get("SERVICES", "COMMAND", data.services.command))
    data.replication = ReplicationSettings(
        peers=_csv(get("REPLICATION", "PEERS", "")),
        interval=duration("REPLICATION", "INTERVAL", "300s"),
        enabled=_bool(get("REPLICATION", "ENABLED", "yes")),
        ca_file=get("REPLICATION", "CA_FILE", data.replication.ca_file))
    data.triggers = TriggerSettings(
        luna_hashes=duration("TRIGGERS", "LUNA_HASHES", "60s"))
    data.audit = AuditSettings(keep=duration("AUDIT", "KEEP", "90d"))
    data.plugins = PluginSettings(
        directory=get("PLUGINS", "DIRECTORY", data.plugins.directory),
        site_directory=get("PLUGINS", "SITE_DIRECTORY", data.plugins.site_directory),
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
