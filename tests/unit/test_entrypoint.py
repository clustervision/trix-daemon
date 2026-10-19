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
from trixd.common import logging as trixlog
from trixd.common import settings as configuration
from trixd.common.constants import DAEMON_VERSION
from trixd.core import instance


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


def test_start_up_is_given_longer_than_hypercorn_allows_by_itself():
    """
    Start-up runs the migrations, loads every plugin and produces every render before a
    socket exists, and hypercorn's own default kills the process part way - which systemd
    restarts into the same wait. How much longer is a judgement; inheriting is the defect.
    """
    from hypercorn.config import Config

    shipped = configuration.ApiSettings()
    served = server.build_config(_settings(bind=shipped.bind, insecure_bind=shipped.insecure_bind,
                                           protocol="http"))
    assert served.startup_timeout > Config().startup_timeout


def test_replication_follows_the_protocol_too(monkeypatch):
    """
    The peer serves what this controller serves, so the sweep asks it the same way.
    """
    from types import SimpleNamespace

    from trixd.core.replication import sweep as replication

    seen = {}
    monkeypatch.setattr(replication, "settings", lambda: _settings(protocol="http"))
    monkeypatch.setattr(replication, "_token", lambda peer, verify, renew=False: "t")
    monkeypatch.setattr(replication.requests, "get", lambda url, **kwargs: (
        seen.setdefault("url", url), SimpleNamespace(status_code=200, json=lambda: {}))[1])
    replication.Sweep()._get("peer.cluster:8050", "hashes")
    assert seen["url"].startswith("http://peer.cluster:8050/")


def _refusal(ini: str):
    """
    What main() does with an ini it cannot use: its exit code, or the escape itself.

    An exception that leaves main() is what this is about, so it is reported as the
    outcome rather than raised: a test that let it through would fail on the escape
    instead of on the assertion that names the rule.
    """
    try:
        return server.main(["--config", ini])
    except Exception as error:
        return f"{type(error).__name__} reached the interpreter"


def test_a_bad_trixd_ini_is_reported_by_name_with_exit_code_2(tmp_path, capsys):
    """
    A typo in the ini is one line on stderr, not a traceback.

    Every way the file can be wrong, not only a value trixd cannot read: configparser's
    own refusals are not ValueErrors, so a dropped section header or a key written twice
    used to leave main() for the interpreter's excepthook - a traceback in the journal and
    exit 1, from the one block whose whole purpose is that neither happens.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[API]\nEXPIRY = tomorrow\n", encoding="utf-8")
    assert server.main(["--config", str(ini)]) == 2
    assert "[API] EXPIRY" in capsys.readouterr().err
    assert server.main(["--config", str(tmp_path / "absent.ini")]) == 2
    for text, named in (("BIND = 1\n[API]\n", "no section headers"),
                        ("[API]\nBIND = 1\nBIND = 2\n", "option 'bind'"),
                        ("[API]\n[API]\n", "section 'API' already exists"),
                        ("[API]\nnonsense\n", "parsing errors")):
        ini.write_text(text, encoding="utf-8")
        assert _refusal(str(ini)) == 2, f"{text!r} did not stop the command by name"
        assert named in capsys.readouterr().err, f"{text!r} was not named on stderr"


def _ini_that_starts_up(tmp_path, extra: str = "", insecure_bind: str = "127.0.0.1:0") -> tuple:
    """
    A configuration a real start-up can run against, logging to a file of its own, with
    extra appended for the reading of it to find fault with.

    Emptying insecure_bind leaves nothing to serve, which is one of the refusals serve()
    makes below the point where what reading the ini found is said.
    """
    from tests.conftest import SHARED_SECRET

    for name in ("plugins", "site-plugins"):
        (tmp_path / name).mkdir()
    logfile = tmp_path / "log" / "trixd.log"
    ini = tmp_path / "trixd.ini"
    ini.write_text(
        f"[LOGGER]\nLEVEL = warning\nLOGFILE = {logfile}\n"
        f"[API]\nBIND =\nINSECURE_BIND = {insecure_bind}\n"
        f"[TOKENS]\nALGORITHM = HS256\nSECRET_KEY = {SHARED_SECRET}\n"
        f'[DATABASE]\nURL = sqlite:///{tmp_path / "trixd.db"}\n'
        f'[PLUGINS]\nDIRECTORY = {tmp_path / "plugins"}\n'
        f'SITE_DIRECTORY = {tmp_path / "site-plugins"}\nENABLED =\n{extra}',
        encoding="utf-8")
    return str(ini), logfile


def _started(ini: str, monkeypatch) -> None:
    """
    main() through serve() to the end of the start-up, which is everything that happens
    before the first socket exists.

    What start-up does after it has said what reading the ini found is stood in for. The
    database, the plugins, the renders and the timers are not what these tests are about,
    and every one of them runs after that line.
    """
    from trixd.core import lifespan
    from trixd.core.render.engine import Engine

    monkeypatch.setattr(lifespan.database, "start", lambda url: None)
    monkeypatch.setattr(lifespan.loader, "load_all", lambda app: {})
    monkeypatch.setattr(lifespan.audit, "start", lambda: None)
    monkeypatch.setattr(lifespan.tasks, "start", lambda: None)
    monkeypatch.setattr(lifespan.triggers, "baseline_all", lambda: None)
    monkeypatch.setattr(lifespan.triggers, "start_all", lambda: None)
    monkeypatch.setattr(Engine, "run_all", lambda self: 0)

    async def _as_hypercorn_does(hypercorn_serve, application, hypercorn_config) -> None:
        """
        The whole startup, which is all that happens before the first socket.
        """
        lifespan.start(application)

    monkeypatch.setattr(server, "_run", _as_hypercorn_does)
    try:
        assert server.main(["--config", ini]) == 0, "the daemon did not start"
    finally:
        # This start-up attached a handler on a file under tmp_path, which is about to
        # go: leave the daemon logger as the rest of the suite expects to find it.
        trixlog.setup("info", "")


def _lines_about(logfile, wording: str) -> list:
    """
    The lines of [LOGGER] LOGFILE that carry one finding.

    Read from the file rather than counted on a logger: the file is what a site reads, so
    it is the only place that answers both whether a finding arrived and how often. It
    also has no blind spot - a second emission from anywhere else lands here too, where a
    handler on one named logger sees only what that logger said.
    """
    return [line for line in logfile.read_text(encoding="utf-8").splitlines()
            if wording in line]


def test_what_reading_the_ini_found_reaches_the_configured_logfile(tmp_path, monkeypatch):
    """
    settings.load() runs before trixlog.setup(), so a warning emitted while the ini is
    read reaches stderr through logging's last resort: unformatted in the journal on a
    unit, and absent from [LOGGER] LOGFILE, which is the file a site actually reads.

    The name on the line is pinned with it: a site greps trixd.settings for what is wrong
    with its ini, so moving these lines somewhere it reads must not rename them.
    """
    ini, logfile = _ini_that_starts_up(tmp_path, "[AUDIT]\nKEEPP = 10d\n")
    _started(ini, monkeypatch)
    # The precondition first: without this the rest measures a file nobody wrote to.
    assert logfile.is_file(), "the start-up logged nothing at all to [LOGGER] LOGFILE"
    assert _lines_about(logfile, "KEEPP"), \
        "a key trixd does not read never reached the logfile"
    account = _lines_about(logfile, "nobody can hold rootus")
    assert account, "an empty ini account never reached the logfile"
    assert "trixd.settings" in account[0], \
        f"the ini's own finding is attributed elsewhere, so a site's grep misses it: " \
        f"{account[0]}"


def test_one_problem_in_the_ini_is_reported_once_however_often_it_is_read(tmp_path,
                                                                         monkeypatch):
    """
    main() pre-checks the ini so a bad one fails the command rather than the daemon, and
    serve() then reads it again. That is two reads of one file, and one empty account is
    one problem: a site reading its log must not be told about it twice.
    """
    ini, logfile = _ini_that_starts_up(tmp_path)
    _started(ini, monkeypatch)
    # The precondition first: without this the rest counts lines nobody wrote.
    assert logfile.is_file(), "the start-up logged nothing at all to [LOGGER] LOGFILE"
    times = len(_lines_about(logfile, "nobody can hold rootus"))
    assert times == 1, f"one empty ini account reached the logfile {times} times"


def test_what_reading_the_ini_found_is_said_even_when_the_bind_refuses_the_start(tmp_path):
    """
    Below the point where this is said, serve() refuses for a second daemon, a certificate
    it cannot read and a bind it cannot take - and a bad setting is the likeliest
    explanation for all three, so it has to have been said before them.

    Said in the lifespan instead it is lost on exactly those paths, because none of them
    ever reaches a lifespan: hypercorn is never handed the application.
    """
    ini, logfile = _ini_that_starts_up(tmp_path, "[AUDIT]\nKEEPP = 10d\n", insecure_bind="")
    try:
        # The precondition: the start really was refused, and for the bind rather than
        # anything earlier - otherwise this measures a daemon that simply came up.
        with pytest.raises(SystemExit, match="neither BIND nor INSECURE_BIND"):
            server.main(["--config", ini])
        assert _lines_about(logfile, "KEEPP"), \
            "the bind refused the start and took what reading the ini found with it"
    finally:
        # This start-up attached a handler on a file under tmp_path, which is about to
        # go: leave the daemon logger as the rest of the suite expects to find it.
        trixlog.setup("info", "")


def test_every_command_that_reads_the_ini_says_what_reading_it_found(tmp_path, capsys):
    """
    A finding is collected while the ini is read and said once the command has a logger,
    so a command that never says it has read the file and thrown the answer away.

    These are the three main() runs to the end without a lifespan; serve is the fourth and
    the tests above cover it. plugin check by name is the one that read the ini and said
    nothing: an unknown key, an empty account, a file it could not read at all - every
    finding on the branch an author uses to ask why their plugin will not load.
    """
    for name in ("plugins", "site"):
        (tmp_path / name).mkdir()
    shipped = os.path.join(os.path.dirname(server.__file__), "plugins")
    ini = tmp_path / "trixd.ini"
    ini.write_text(
        f"[LOGGER]\nLEVEL = warning\nLOGFILE =\n"
        f"[TOKENS]\nALGORITHM = EdDSA\nPRIVATE_KEY = {tmp_path / 'keys' / 'trixd.key'}\n"
        f"[PLUGINS]\nDIRECTORY = {shipped}\nSITE_DIRECTORY = {tmp_path / 'site'}\n"
        f"[AUDIT]\nKEEPP = 10d\n", encoding="utf-8")
    try:
        for command in (["keygen"], ["plugin", "new", "asked"],
                        ["plugin", "check", "services"]):
            capsys.readouterr()
            # The precondition: the command really ran. A refusal says what was found as
            # well, so without this the assertion below passes on a command that failed.
            assert server.main(["--config", str(ini)] + command) == 0, \
                f"{' '.join(command)} did not run: {capsys.readouterr().err}"
            said = capsys.readouterr()
            assert "KEEPP" in said.out + said.err, \
                f"{' '.join(command)} read the ini and never said what it found"
    finally:
        # These commands configured the daemon logger for themselves: leave it as the rest
        # of the suite expects to find it.
        trixlog.setup("info", "")


def test_what_was_found_is_still_said_when_a_later_setting_stops_the_command(tmp_path,
                                                                            capsys):
    """
    A daemon refusing to start over one setting still has to say what else was wrong with
    the ini: nothing runs after this to say it, and there is no logfile to say it to. The
    unreadable ceiling is found before the duration that raises, so carrying it on the
    returned object alone would drop it exactly where the operator is already in trouble.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[API]\nMAX_BODY = not a size\n[AUDIT]\nKEEP = sometime\n",
                   encoding="utf-8")
    assert server.main(["--config", str(ini)]) == 2
    said = capsys.readouterr().err
    assert "[AUDIT] KEEP" in said, "the setting that stopped the command was not named"
    assert "MAX_BODY" in said, "what was found before it was swallowed by the refusal"


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


def _serving_ini(tmp_path) -> str:
    """
    A configuration a real start-up can run against: its own database, no plugins.
    """
    from tests.conftest import SHARED_SECRET

    for name in ("plugins", "site-plugins"):
        (tmp_path / name).mkdir()
    ini = tmp_path / "trixd.ini"
    ini.write_text(
        "[LOGGER]\nLEVEL = warning\nLOGFILE =\n"
        "[API]\nBIND =\nINSECURE_BIND = 127.0.0.1:0\n"
        f"[TOKENS]\nALGORITHM = HS256\nSECRET_KEY = {SHARED_SECRET}\n"
        f'[DATABASE]\nURL = sqlite:///{tmp_path / "trixd.db"}\n'
        f'[PLUGINS]\nDIRECTORY = {tmp_path / "plugins"}\n'
        f'SITE_DIRECTORY = {tmp_path / "site-plugins"}\nENABLED =\n',
        encoding="utf-8")
    return str(ini)


def test_a_second_daemon_is_refused_before_it_touches_anything(tmp_path, monkeypatch):
    """
    Hypercorn runs the lifespan to completion before it creates a socket, so the port a
    second daemon cannot have stops it only once it has migrated the database, taken over
    the tasks the first one is running and rewritten every render. Nothing may run.
    """
    import fcntl

    from trixd.core import lifespan, tasks
    from trixd.core.render.engine import Engine

    ini = _serving_ini(tmp_path)
    reached = []

    def _requeued() -> None:
        """
        Stand where the rows the first daemon is running are taken from it.
        """
        reached.append("_requeue")

    def _ran_all(self) -> int:
        """
        Stand where every render file is written again from this process.
        """
        reached.append("run_all")
        return 0

    # A second daemon is a fresh interpreter, so its worker has never been started and
    # start() does not return early.
    monkeypatch.setattr(tasks, "_worker", None)
    monkeypatch.setattr(tasks, "_requeue", _requeued)
    monkeypatch.setattr(Engine, "run_all", _ran_all)

    async def _as_hypercorn_does(hypercorn_serve, application, hypercorn_config) -> None:
        """
        The whole startup, which is all that happens before the first socket.
        """
        lifespan.start(application)

    monkeypatch.setattr(server, "_run", _as_hypercorn_does)

    # The first daemon, holding what it holds for the life of its process.
    first = os.open(f"{ini}.lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
    refused = ""
    try:
        server.serve(ini)
    except SystemExit as stopped:
        refused = str(stopped)
    finally:
        os.close(first)
    assert reached == [], f"a second daemon reached {reached} with the lock already held"
    assert "already running" in refused and f"{ini}.lock" in refused


def test_the_lock_dies_with_the_daemon_that_held_it(tmp_path):
    """
    An advisory lock on a descriptor is the kernel's to drop, so a daemon killed outright
    leaves nothing behind and the process started in its place is never refused.
    """
    import subprocess
    import sys

    config = configuration.Settings(config_file=str(tmp_path / "trixd.ini"))
    path = instance.lock_path(config)
    first = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         f"handle = os.open({path!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
         "fcntl.flock(handle, fcntl.LOCK_EX)\n"
         "sys.stdout.write('held\\n')\n"
         "sys.stdout.flush()\n"
         "time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "held", "the first daemon never took it"
        assert instance.SingleInstance(config).take() is False
    finally:
        first.kill()
        first.wait(timeout=10)
    replacement = instance.SingleInstance(config)
    assert replacement.take() is True, "a daemon killed outright left its lock behind"
    replacement.release()


def test_the_terminal_subcommands_run_beside_a_serving_daemon(tmp_path, capsys):
    """
    A person asks the installed command line what it would do while the daemon serves.
    The lock belongs to serving, so neither keygen nor plugin check waits for it.
    """
    import fcntl

    private = tmp_path / "keys" / "trixd.ed25519"
    ini = tmp_path / "trixd.ini"
    ini.write_text(f"[LOGGER]\nLOGFILE =\n[TOKENS]\nALGORITHM = EdDSA\n"
                   f"PRIVATE_KEY = {private}\nKEY_ID = trixd-1\n", encoding="utf-8")
    config = configuration.Settings(config_file=str(ini))
    held = os.open(instance.lock_path(config), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert server.main(["--config", str(ini), "keygen"]) == 0
        assert server.main(["--config", str(ini), "plugin", "check", str(tmp_path)]) == 1
    finally:
        os.close(held)
    assert f"wrote {private}" in capsys.readouterr().out


def test_the_lock_is_beside_the_ini_and_never_on_storage_a_pair_shares(tmp_path):
    """
    One lock per configuration file, named as the daemon was told to read it: each
    controller of a pair keeps its own even where that ini points at shared storage.
    """
    real = tmp_path / "shared" / "trixd.ini"
    real.parent.mkdir()
    real.write_text("[API]\n", encoding="utf-8")
    link = tmp_path / "local" / "trixd.ini"
    link.parent.mkdir()
    os.symlink(real, link)
    assert instance.lock_path(configuration.Settings(config_file=str(link))) == f"{link}.lock"
