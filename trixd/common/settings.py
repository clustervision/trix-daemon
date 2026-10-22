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

from trixd.common.constants import (DEFAULT_MAX_BODY, DEFAULT_TOKEN_ALGORITHM,
                                   TOKEN_ALGORITHM_SHARED, TOKEN_ALGORITHMS)

# Inside the package, as luna.ini is; the wheel ships only trixd.ini.sample beside it.
DEFAULT_CONFIG: str = "/trinity/local/trixd/daemon/config/trixd.ini"

# Sections trixd.ini may still name that this daemon does not read. Named at startup as
# ignored rather than complained about as unknown, and the one place they are listed.
IGNORED_SECTIONS: tuple = ("LUNA", "TRIGGERS")

_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$", re.IGNORECASE)
# K, M and G with the suffix an operator writes them with: 8M, 64MB and 16MiB alike.
_SIZE = re.compile(r"^\s*(\d+)\s*([kmg])?i?b?\s*$", re.IGNORECASE)
_FACTOR = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_TRUE = ("1", "yes", "y", "true", "t", "on", "enabled", "enable")
_FALSE = ("0", "no", "n", "false", "f", "off", "disabled", "disable")


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
    bind: str = "0.0.0.0:7070"
    insecure_bind: str = "0.0.0.0:7071"
    protocol: str = "https"
    certfile: str = ""
    keyfile: str = ""
    expiry: int = 3600
    username: str = ""
    password: str = ""
    max_body: int = DEFAULT_MAX_BODY


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


class DatabaseSettings(BaseModel):
    """
    Where the store lives.
    """
    url: str = "sqlite:////trinity/local/trixd/daemon/var/trixd.db"


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
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    services: ServicesSettings = Field(default_factory=ServicesSettings)
    replication: ReplicationSettings = Field(default_factory=ReplicationSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    # Settings trixd.ini still names that are no longer read, to say so at startup.
    ignored: list = Field(default_factory=list)
    # What load() found wrong in the ini, each worded as it is to be said. Carried
    # rather than logged, because the ini is read before there is a logfile to log to.
    warnings: list = Field(default_factory=list)


def size(value: str, fallback: int, bare: int = 1) -> int:
    """
    A byte count, where K, M and G are powers of 1024, written with or without a B or
    iB after them. Empty is the fallback, and anything else unreadable is an error.
    """
    text = str(value or "").strip()
    if not text:
        return fallback
    found = _SIZE.match(text)
    if not found:
        raise ValueError(f"{text} is not a size such as 512K, 8M, 64MB or 16MiB")
    unit = (found.group(2) or "").lower()
    scale = {"": bare, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[unit]
    return int(found.group(1)) * scale


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


def _bool(value: str, fallback: bool = False) -> bool:
    """
    Accept what an operator writes for on and for off. Empty is the fallback, and
    anything else unreadable is an error: reading it as off is the dangerous guess.
    """
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{text} is not a yes or a no")


def _secret(inline: str, path: str) -> str:
    """
    This daemon's own secret: what the ini states, else the file the installer wrote it to.
    """
    if inline:
        return inline.strip()
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    return ""


def _protocol(value: str) -> str:
    """
    Canonicalise the scheme trixd serves on, refusing one it cannot serve.
    """
    wanted = str(value or "").strip().lower() or "https"
    if wanted not in ("https", "http"):
        raise ValueError(f"[API] PROTOCOL is {wanted}, which is not https or http")
    return wanted


def _unknown(parser: RawConfigParser, asked: set, path: str, found: list) -> None:
    """
    Name every key the ini carries that this daemon does not read, so a misspelled one
    is not a silent fall back to a default. The known keys are the ones load() just
    read, so a setting added there is covered without a second list to remember.
    """
    sections = {section for section, _ in asked}
    inherited = set(parser.defaults())
    for section in parser.sections():
        if section in IGNORED_SECTIONS:
            continue
        if section not in sections:
            found.append("%s has a [%s] section, which trixd does not read; every "
                         "setting in it is ignored" % (path, section))
            continue
        for option in sorted(parser.options(section)):
            if option in inherited or (section, option) in asked:
                continue
            found.append("%s names [%s] %s, which trixd does not read; it is ignored "
                         "and the default is used, so check it against "
                         "trixd.ini.sample" % (path, section, option.upper()))


def load(path: str = "", found: list = None) -> Settings:
    """
    Read the ini into a Settings object, falling back to the defaults above.

    What is wrong with the ini is appended to found, and is on the returned object as
    warnings. Pass a list to keep what was found when a later setting raises instead:
    the caller holds it either way, where a half-built Settings object is gone.
    """
    found = [] if found is None else found
    path = path or os.environ.get("TRIXD_CONFIG") or DEFAULT_CONFIG
    if not os.path.isfile(path):
        raise ValueError(f"{path} does not exist")
    parser = RawConfigParser()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        # isfile() passes and RawConfigParser.read() swallows this, so without the open
        # a file it cannot read starts the daemon on its defaults without a word.
        found.append("%s cannot be read (%s); trixd is starting on its built-in "
                     "defaults, so the bind, the database and the plugins are not "
                     "the ones that file names" % (path, error))
        text = ""
    parser.read_string(text, source=path)
    asked: set = set()

    def get(section: str, option: str, fallback: str = "") -> str:
        """
        One value from the ini, or the fallback when it says nothing. Every call records
        its key, so what the daemon never asked for is whatever the ini has left over.
        """
        asked.add((section, option.lower()))
        if parser.has_section(section) and parser.has_option(section, option):
            return parser.get(section, option)
        return fallback

    def unreadable(section: str, option: str, value: str, using: str) -> None:
        """
        A value the daemon could not read, named with what it is using instead. Loud,
        because the daemon comes up on something other than what the ini asked for.
        """
        found.append("[%s] %s is %r, which trixd cannot read; it is using %s instead"
                     % (section, option, value, using))

    def capacity(section: str, option: str, fallback: int) -> int:
        """
        One byte count from the ini; an unreadable one names itself and is not fatal.
        """
        raw = get(section, option, "")
        try:
            return size(raw, fallback)
        except ValueError:
            unreadable(section, option, raw, f"{fallback} bytes")
            return fallback

    def flag(section: str, option: str, default: bool) -> bool:
        """
        One yes or no from the ini; an unreadable one names itself and is not fatal.
        """
        raw = get(section, option, "")
        try:
            return _bool(raw, default)
        except ValueError:
            unreadable(section, option, raw, "yes" if default else "no")
            return default

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
        protocol=_protocol(get("API", "PROTOCOL", data.api.protocol)),
        certfile=get("API", "CERTFILE", ""),
        keyfile=get("API", "KEYFILE", ""),
        # A bare EXPIRY counts in hours, as luna.ini's does.
        expiry=duration("API", "EXPIRY", "1h", bare=3600),
        username=get("API", "USERNAME", ""),
        password=get("API", "PASSWORD", ""),
        max_body=capacity("API", "MAX_BODY", DEFAULT_MAX_BODY))
    if not data.api.username or not data.api.password:
        # The same check the luna plugin makes on luna.ini. With no account nobody can
        # hold rootus, and the sweep signs in to its peer as nobody at all.
        found.append("%s has no [API] USERNAME and PASSWORD, so nobody can hold "
                     "rootus: the directory, slurm and replication routes answer 403 "
                     "to every caller and a peer refuses the sweep" % (path,))
    secret_file = get("TOKENS", "SECRET_FILE", data.tokens.secret_file)
    data.tokens = TokenSettings(
        algorithm=_algorithm(get("TOKENS", "ALGORITHM", DEFAULT_TOKEN_ALGORITHM)),
        secret_key=_secret(get("TOKENS", "SECRET_KEY", ""), secret_file),
        secret_file=secret_file,
        private_key=get("TOKENS", "PRIVATE_KEY", data.tokens.private_key),
        key_id=get("TOKENS", "KEY_ID", data.tokens.key_id),
        trust=get("TOKENS", "TRUST", data.tokens.trust),
        issuer=get("TOKENS", "ISSUER", data.tokens.issuer))
    # Settings this daemon no longer reads here: where Luna answers is Luna's own
    # business, and a plugin's own settings belong to the plugin.
    data.ignored = [f"[{name}]" for name in IGNORED_SECTIONS
                    if parser.has_section(name)]
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
        enabled=flag("REPLICATION", "ENABLED", True),
        ca_file=get("REPLICATION", "CA_FILE", data.replication.ca_file))
    data.audit = AuditSettings(keep=duration("AUDIT", "KEEP", "90d"))
    data.plugins = PluginSettings(
        directory=get("PLUGINS", "DIRECTORY", data.plugins.directory),
        site_directory=get("PLUGINS", "SITE_DIRECTORY", data.plugins.site_directory),
        enabled=_csv(get("PLUGINS", "ENABLED", "")))
    _unknown(parser, asked, path, found)
    data.warnings = found
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
