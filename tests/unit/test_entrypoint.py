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
The command line the unit and the installer call: listeners, keygen, the plugin
scaffold and how a bad trixd.ini is reported.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os
import stat

import pytest

from trixd import server
from trixd.common import settings as configuration
from trixd.common.constants import DAEMON_VERSION


def _settings(**api) -> configuration.Settings:
    """
    Settings whose [API] section says only what the test gives it.
    """
    config = configuration.Settings()
    config.api = configuration.ApiSettings(**{"bind": "", "insecure_bind": "", **api})
    return config


def _pair(tmp_path) -> tuple:
    """
    A certificate and a key file; their content is not read here.
    """
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    return str(cert), str(key)


def test_https_serves_tls_on_bind_and_plain_on_every_address(tmp_path):
    """
    https and the shipped binds: TLS on 8050, and plain 8051 open to the nodes.
    """
    cert, key = _pair(tmp_path)
    shipped = configuration.ApiSettings()
    served = server.build_config(_settings(bind=shipped.bind, insecure_bind=shipped.insecure_bind,
                                           protocol="https", certfile=cert, keyfile=key))
    assert served.bind == ["0.0.0.0:8050"]
    assert served.insecure_bind == ["0.0.0.0:8051"]
    assert (served.certfile, served.keyfile) == (cert, key)
    assert served.workers == 1, "one process: the plugin registry is in memory"


def test_http_serves_plain_on_both_with_no_certificate():
    """
    http, as a site may choose: 8050 and 8051 both plain, both open.
    """
    shipped = configuration.ApiSettings()
    served = server.build_config(_settings(bind=shipped.bind, insecure_bind=shipped.insecure_bind,
                                           protocol="http"))
    assert served.bind == ["0.0.0.0:8050", "0.0.0.0:8051"]
    assert served.insecure_bind == []
    assert not served.ssl_enabled


def test_https_without_its_certificate_stops_the_start_by_name(tmp_path):
    """
    Better a named stop than a daemon that quietly serves no TLS where https was chosen.
    """
    cert, _ = _pair(tmp_path)
    with pytest.raises(SystemExit, match=r"needs \[API\] KEYFILE"):
        server.build_config(_settings(bind="0.0.0.0:8050", protocol="https", certfile=cert,
                                      keyfile=str(tmp_path / "absent.key")))
    with pytest.raises(SystemExit, match=r"needs \[API\] CERTFILE"):
        server.build_config(_settings(bind="0.0.0.0:8050", protocol="https"))


def test_no_listener_at_all_stops_the_start():
    """
    Neither BIND nor INSECURE_BIND leaves nothing to serve.
    """
    with pytest.raises(SystemExit, match="neither BIND nor INSECURE_BIND"):
        server.build_config(_settings(protocol="http"))


def test_replication_follows_the_protocol_too(monkeypatch):
    """
    The peer serves what this controller serves, so the sweep asks it the same way.
    """
    from types import SimpleNamespace

    from trixd.core.replication import sweep as replication

    seen = {}
    monkeypatch.setattr(replication, "settings", lambda: _settings(protocol="http"))
    monkeypatch.setattr(replication.tokens, "service_token", lambda: "t")
    monkeypatch.setattr(replication.requests, "get", lambda url, **kwargs: (
        seen.setdefault("url", url), SimpleNamespace(status_code=200, json=lambda: {}))[1])
    replication.Sweep()._get("peer.cluster:8050", "hashes")
    assert seen["url"].startswith("http://peer.cluster:8050/")


def test_a_bad_trixd_ini_is_reported_by_name_with_exit_code_2(tmp_path, capsys):
    """
    A typo in the ini is one line on stderr, not a traceback.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[API]\nEXPIRY = tomorrow\n", encoding="utf-8")
    assert server.main(["--config", str(ini)]) == 2
    assert "[API] EXPIRY" in capsys.readouterr().err
    assert server.main(["--config", str(tmp_path / "absent.ini")]) == 2


def test_the_version_is_the_installed_one(capsys):
    """
    --version answers what the package metadata says, or the release line from a checkout.
    """
    with pytest.raises(SystemExit) as stopped:
        server.main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == DAEMON_VERSION


def test_keygen_refuses_under_the_shared_secret(rig, capsys):
    """
    HS256 signs with a shared secret, so there is no keypair to make.
    """
    config = configuration.current().config_file
    assert server.main(["--config", config, "keygen"]) == 1
    assert "needs no keypair" in capsys.readouterr().out


def test_keygen_writes_a_keypair_once(tmp_path, capsys):
    """
    Under EdDSA it writes the pair, private 0600, and refuses to overwrite it.
    """
    private = tmp_path / "keys" / "trixd.ed25519"
    ini = tmp_path / "trixd.ini"
    ini.write_text(f"[LOGGER]\nLOGFILE =\n[TOKENS]\nALGORITHM = EdDSA\nPRIVATE_KEY = {private}\n"
                   f"KEY_ID = trixd-1\n", encoding="utf-8")
    assert server.main(["--config", str(ini), "keygen"]) == 0
    said = capsys.readouterr().out
    assert f"wrote {private}" in said and "trixd-1.pub" in said
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(private).st_mode) == 0o600
    before = private.read_bytes()
    assert server.main(["--config", str(ini), "keygen"]) == 1
    assert private.read_bytes() == before, "an existing key was replaced"


def test_the_plugin_command_wants_new_and_a_name(rig, capsys):
    """
    Anything else prints the usage and changes nothing.
    """
    config = configuration.current().config_file
    assert server.main(["--config", config, "plugin"]) == 1
    assert server.main(["--config", config, "plugin", "old", "x"]) == 1
    assert "usage: trixd plugin new <name>" in capsys.readouterr().out
    assert server.main(["--config", config, "plugin", "new", "luna"]) == 1, "reserved name"
