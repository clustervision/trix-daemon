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
The helpers everything else stands on: the shell runner, file writes, settings,
keys, logging and the task table's housekeeping.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import logging
import os
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from trixd.common import logging as trixlog
from trixd.common import settings as configuration
from trixd.common.errors import BackendFailure
from trixd.core import tasks
from trixd.core.security import keys
from trixd.db import engine as database
from trixd.db import models
from trixd.utils import files, shell

POSIX = os.name == "posix"


class Records(logging.Handler):
    """
    Every record a logger emits, kept for a test to read.
    """

    def __init__(self) -> None:
        """
        An empty list of messages.
        """
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record) -> None:
        """
        Keep the formatted message.
        """
        self.messages.append(record.getMessage())


def _python(code: str) -> list:
    """
    An argument list that runs a line of Python, so the tests need no Unix tools.
    """
    return [sys.executable, "-c", code]


def test_a_command_must_be_an_argument_list():
    """
    A string would mean a shell, and a shell would parse whatever a caller sent.
    """
    with pytest.raises(BackendFailure):
        shell.run("echo hello")
    with pytest.raises(BackendFailure):
        shell.run([])


def test_a_command_reports_its_code_and_both_streams():
    """
    The exit code and the two streams, decoded and trimmed.
    """
    result = shell.run(_python("import sys; print('out'); print('err', file=sys.stderr); "
                               "sys.exit(3)"))
    assert (result.code, result.out, result.err, result.ok) == (3, "out", "err", False)


def test_a_failing_command_raises_only_when_checked():
    """
    check=True turns a non-zero exit into an error naming the exit status. What the command
    itself said goes to the journal and not into the error, which leaves the machine; the
    test below pins that.
    """
    argv = _python("import sys; print('it broke', file=sys.stderr); sys.exit(1)")
    assert not shell.run(argv).ok
    with pytest.raises(BackendFailure, match="exit status 1"):
        shell.run(argv, check=True)


def test_a_missing_program_is_named():
    """
    Not a FileNotFoundError traceback, but which program is missing.
    """
    with pytest.raises(BackendFailure, match="is not installed"):
        shell.run(["trixd-no-such-program-anywhere"])


def test_a_command_that_hangs_is_stopped():
    """
    Every command has a timeout, so a hung tool cannot hold a request for ever.
    """
    with pytest.raises(BackendFailure, match="did not finish within 1s"):
        shell.run(_python("import time; time.sleep(10)"), timeout=1)


def test_json_is_parsed_and_anything_else_is_an_error():
    """
    run_json answers the document, None for no output, and refuses what is not JSON.
    """
    assert shell.run_json(_python("print('{\"a\": [1, 2]}')")) == {"a": [1, 2]}
    assert shell.run_json(_python("pass")) is None
    with pytest.raises(BackendFailure, match="did not return JSON"):
        shell.run_json(_python("print('not json')"))


def test_a_password_never_reaches_the_log():
    """
    The value after a secret flag is hidden in what the runner logs.
    """
    assert shell._loggable(["obol", "user", "add", "carol", "--password", "s3cret"]) == \
        ["obol", "user", "add", "carol", "--password", "********"]
    assert shell._loggable(["--user", "carol", "-w"]) == ["--user", "carol", "-w"],         "only the value after a secret flag is hidden"

    records = Records()
    shell.LOGGER.addHandler(records)
    level = shell.LOGGER.level
    shell.LOGGER.setLevel(logging.DEBUG)
    try:
        shell.run(_python("pass") + ["--bind-pass", "s3cret"])
    finally:
        shell.LOGGER.removeHandler(records)
        shell.LOGGER.setLevel(level)
    assert records.messages, "the runner logged nothing"
    assert not any("s3cret" in message for message in records.messages), records.messages


def test_a_write_is_whole_and_leaves_no_temporary_file(tmp_path):
    """
    Written beside the target and renamed, with the mode asked for.
    """
    target = tmp_path / "deep" / "er" / "file.conf"
    files.write(str(target), "one\n", mode=0o640)
    files.write(str(target), "two\n", mode=0o640)
    assert target.read_text(encoding="utf-8") == "two\n"
    assert os.listdir(target.parent) == ["file.conf"], "a temporary file was left behind"
    if POSIX:
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


def test_a_write_that_cannot_happen_says_where(tmp_path):
    """
    A path under a file is not writable, and the error names the path.
    """
    blocker = tmp_path / "plain"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(BackendFailure, match="cannot write"):
        files.write(str(blocker / "child.conf"), "x")


def test_a_yaml_document_round_trips(tmp_path):
    """
    write_yaml keeps the key order a reader expects to see.
    """
    import yaml

    target = tmp_path / "doc.yml"
    files.write_yaml(str(target), {"b": 1, "a": [1, 2]})
    text = target.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert text.index("b:") < text.index("a:")
    assert yaml.safe_load(text) == {"b": 1, "a": [1, 2]}


def test_a_managed_block_keeps_the_sites_own_lines(tmp_path):
    """
    The site's lines before and after the block survive every rewrite of the block.
    """
    target = tmp_path / "genders"
    target.write_text("site-before\n", encoding="utf-8")
    files.set_managed_block(str(target), "TrinityX", "node001 compute\n")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("site-after\n")
    files.set_managed_block(str(target), "TrinityX", "node001 compute\nnode002 compute")

    text = target.read_text(encoding="utf-8")
    assert text.startswith("site-before\n")
    assert text.endswith("site-after\n")
    assert text.count("managed block, do not edit") == 1
    assert files.get_managed_block(str(target), "TrinityX") == "node001 compute\nnode002 compute"


def test_a_managed_block_is_added_to_a_file_without_a_final_newline(tmp_path):
    """
    The block starts on a line of its own, not glued to the site's last line.
    """
    target = tmp_path / "hosts"
    target.write_text("last-line-without-newline", encoding="utf-8")
    files.set_managed_block(str(target), "TrinityX", "x")
    assert target.read_text(encoding="utf-8").splitlines()[0] == "last-line-without-newline"
    assert files.get_managed_block(str(tmp_path / "missing"), "TrinityX") == ""
    assert files.get_managed_block(str(target), "Someone-else") == ""


def test_a_missing_ini_stops_the_daemon_by_name(tmp_path):
    """
    No silent defaults when the file named is not there.
    """
    with pytest.raises(ValueError, match="does not exist"):
        configuration.load(str(tmp_path / "nowhere.ini"))


def test_an_unreadable_duration_names_its_section_and_key(tmp_path):
    """
    A typo in an interval stops the start and says which one.
    """
    ini = tmp_path / "trixd.ini"
    ini.write_text("[REPLICATION]\nINTERVAL = soon\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[REPLICATION\] INTERVAL"):
        configuration.load(str(ini))


def test_durations_read_every_unit():
    """
    s, m, h and d, any case, and empty is the fallback.
    """
    assert configuration.seconds("45") == 45
    assert configuration.seconds("2m") == 120
    assert configuration.seconds("1H") == 3600
    assert configuration.seconds("2d") == 172800
    assert configuration.seconds("", fallback=7) == 7
    assert configuration.seconds("3", bare=3600) == 10800


def test_a_generated_private_key_is_never_readable_by_others(tmp_path):
    """
    0600 from the moment it exists, and the public half beside it.
    """
    private = tmp_path / "keys" / "trixd.ed25519"
    public = keys.generate(str(private))
    assert os.path.isfile(public)
    assert keys.read_private(str(private)) is not None
    if POSIX:
        assert stat.S_IMODE(os.stat(private).st_mode) == 0o600


def test_a_trust_directory_ignores_what_is_not_a_public_key(tmp_path):
    """
    A stray file, a broken key and a missing directory are skipped, not fatal.
    """
    (tmp_path / "trust").mkdir()
    keys.generate(str(tmp_path / "peer.ed25519"), str(tmp_path / "trust" / "peer.pub"))
    (tmp_path / "trust" / "broken.pub").write_text("not a key", encoding="utf-8")
    (tmp_path / "trust" / "notes.txt").write_text("hello", encoding="utf-8")
    assert list(keys.read_trust(str(tmp_path / "trust"))) == ["peer"]
    assert keys.read_trust(str(tmp_path / "absent")) == {}


def test_a_trust_directory_others_may_write_is_not_believed(tmp_path):
    """
    A key here decides which issuers this daemon accepts, so whoever can write one in
    decides that - and in the keypair mode a trusted key may assert anything it likes.
    Ownership and mode were the whole protection and neither was looked at, while the
    private key beside it is created 0600 and has a test pinning that.

    The directory is refused as a whole rather than file by file: if others may write
    there, which file they chose is not the interesting part.
    """
    if not POSIX:
        pytest.skip("modes mean nothing here")

    trust = tmp_path / "trust"
    trust.mkdir()
    keys.generate(str(tmp_path / "peer.ed25519"), str(trust / "peer.pub"))
    assert list(keys.read_trust(str(trust))) == ["peer"], "a sound directory stopped loading"

    os.chmod(trust, 0o777)
    assert keys.read_trust(str(trust)) == {}, "a world-writable trust directory was believed"

    os.chmod(trust, 0o755)
    os.chmod(trust / "peer.pub", 0o666)
    assert keys.read_trust(str(trust)) == {}, "a world-writable key was believed"

    # What the generator actually writes must still load: 0644 is readable by others,
    # which is fine, and writable by none of them, which is the part that matters.
    os.chmod(trust / "peer.pub", 0o644)
    assert list(keys.read_trust(str(trust))) == ["peer"], "a generated key stopped loading"


def test_a_missing_private_key_says_who_makes_it(tmp_path):
    """
    The daemon never makes its own key; the message points at the installer.
    """
    from trixd.common.errors import TrixdError

    with pytest.raises(TrixdError, match="installer"):
        keys.read_private(str(tmp_path / "absent.ed25519"))


def test_the_signing_secret_is_trixds_own_and_needs_no_luna_ini(tmp_path, monkeypatch):
    """
    trixd signs with the secret its own ini names, so it starts on a controller where
    Luna's luna.ini is absent, unreadable or holds a different secret.
    """
    monkeypatch.setenv("TRIXD_LUNA_INI", str(tmp_path / "there-is-no-luna.ini"))
    mine = "a" * 32
    ini = tmp_path / "trixd.ini"
    ini.write_text(f"[TOKENS]\nALGORITHM = HS256\nSECRET_KEY = {mine}\n", encoding="utf-8")
    ring = keys.KeyRing(configuration.load(str(ini)).tokens).load()
    assert ring.signing_key() == mine
    assert ring.verifying_key() == mine


def test_the_signing_secret_falls_back_to_the_file_the_installer_wrote(tmp_path):
    """
    An empty SECRET_KEY takes SECRET_FILE, which is how the installer may leave it.
    """
    secret_file = tmp_path / "secret.key"
    secret_file.write_text("b" * 32 + "\n", encoding="utf-8")
    ini = tmp_path / "trixd.ini"
    ini.write_text(f"[TOKENS]\nALGORITHM = HS256\nSECRET_KEY =\n"
                   f"SECRET_FILE = {secret_file}\n", encoding="utf-8")
    assert configuration.load(str(ini)).tokens.secret_key == "b" * 32


def test_a_failing_command_does_not_hand_back_what_the_child_said():
    """
    The child's own output went into the error body, which leaves the machine. The masking
    beside the runner covers what we put on the command line; a child can also print its
    own configuration - obol shows its bind credentials on some failures - and that is a
    value trixd never sees and so cannot mask. So the output stays in the journal and the
    caller is told where to look.
    """
    records = Records()
    shell.LOGGER.addHandler(records)
    try:
        with pytest.raises(BackendFailure) as raised:
            shell.run(["sh", "-c", "echo 'bind_dn=cn=admin bind_pass=hunter2' >&2; exit 3"],
                      10, check=True)
    finally:
        shell.LOGGER.removeHandler(records)

    assert "hunter2" not in str(raised.value), f"the child's output reached the caller: {raised.value}"
    assert "exit status 3" in str(raised.value), raised.value
    assert any("hunter2" in message for message in records.messages), \
        f"the reason did not reach the journal either: {records.messages}"


def test_following_a_personal_luna_ini_is_said_once(tmp_path, monkeypatch):
    """
    Which luna.ini is followed decides which cluster this daemon reads. A command-line
    login as root leaves a personal one behind, and the daemon then takes that cluster's
    nodes and renders this controller's genders and slurm files from them, with nothing
    saying why. The preference is kept; it is no longer silent.

    Once, not per call: the path is re-read whenever the file's timestamp moves.
    """
    from trixd.plugins.luna import config as luna_config

    monkeypatch.delenv(luna_config.LUNA_INI_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    personal = tmp_path / ".luna" / "luna.ini"
    personal.parent.mkdir(parents=True)
    personal.write_text("[API]\nENDPOINT = elsewhere:7050\n", encoding="utf-8")
    # raising=False so this still runs against a build without the variable: otherwise
    # the test errors in setup instead of asserting, and proves only that it is new.
    monkeypatch.setattr(luna_config, "_said_personal", "", raising=False)

    records = Records()
    luna_config.LOGGER.addHandler(records)
    try:
        assert luna_config.chosen_path() == str(personal)
        assert luna_config.chosen_path() == str(personal)
    finally:
        luna_config.LOGGER.removeHandler(records)

    said = [message for message in records.messages if str(personal) in message]
    assert len(said) == 1, f"expected one line about the personal file, got {said}"
    assert luna_config.DEFAULT_LUNA_INI in said[0], said[0]


def test_a_secret_file_others_can_read_is_said_out_loud(tmp_path):
    """
    The secret signs every token this daemon issues, rootus ones included, and on a default
    install it is the file Luna's key is in too. Its mode was never looked at, while the
    private key beside it is created 0600 and has a test pinning that.

    Said rather than refused, deliberately: a controller already serving on a loose file
    should keep serving, and a daemon that will not start is its own outage.
    """
    if not POSIX:
        pytest.skip("modes mean nothing here")

    secret_file = tmp_path / "secret.key"
    secret_file.write_text("b" * 32 + "\n", encoding="utf-8")
    ini = tmp_path / "trixd.ini"
    ini.write_text(f"[TOKENS]\nALGORITHM = HS256\nSECRET_KEY =\n"
                   f"SECRET_FILE = {secret_file}\n", encoding="utf-8")

    def said(mode: int) -> list:
        """
        What the key ring logged while loading a secret file at this mode.
        """
        os.chmod(secret_file, mode)
        records = Records()
        keys.LOGGER.addHandler(records)
        try:
            ring = keys.KeyRing(configuration.load(str(ini)).tokens).load()
            assert ring.signing_key() == "b" * 32, "the secret stopped being read"
        finally:
            keys.LOGGER.removeHandler(records)
        return records.messages

    tight = said(0o600)
    assert not any(str(secret_file) in message and "mode" in message for message in tight), \
        f"a 0600 secret file was complained about: {tight}"

    loose = said(0o644)
    assert any(str(secret_file) in message and "0644" in message for message in loose), \
        f"a world-readable secret file was not mentioned: {loose}"


def test_no_secret_anywhere_stops_the_start_naming_both_places(tmp_path):
    """
    Signing with nothing is the one thing that must not pass quietly.
    """
    from trixd.common.errors import TrixdError

    ini = tmp_path / "trixd.ini"
    ini.write_text(f"[TOKENS]\nALGORITHM = HS256\nSECRET_KEY =\n"
                   f"SECRET_FILE = {tmp_path / 'absent.key'}\n", encoding="utf-8")
    with pytest.raises(TrixdError, match=r"\[TOKENS\] SECRET_KEY in trixd.ini"):
        keys.KeyRing(configuration.load(str(ini)).tokens).load()


def test_logging_survives_a_log_file_it_cannot_write(tmp_path):
    """
    A bad LOGFILE is a warning, not a failed start.
    """
    blocker = tmp_path / "plain"
    blocker.write_text("", encoding="utf-8")
    logger = trixlog.setup("debug", str(blocker / "trixd.log"))
    try:
        assert logger.level == logging.DEBUG
        assert len(logger.handlers) == 1, "only the console handler"
        logger = trixlog.setup("loud", str(tmp_path / "logs" / "trixd.log"))
        assert logger.level == logging.INFO, "an unknown level falls back to info"
        assert len(logger.handlers) == 2
    finally:
        for handler in list(logger.handlers):
            handler.close()
        trixlog.setup("warning", "")


def test_finished_tasks_are_forgotten_after_a_while(rig):
    """
    Old finished rows go; recent ones and unfinished ones stay.
    """
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        seconds=tasks.KEEP_SECONDS + 60)
    handle = database.session()
    try:
        handle.add_all([
            models.Task(request_id="old-done", state=tasks.DONE, finished=old),
            models.Task(request_id="old-failed", state=tasks.FAILED, finished=old),
            models.Task(request_id="new-done", state=tasks.DONE,
                        finished=datetime.now(timezone.utc).replace(tzinfo=None)),
            models.Task(request_id="old-queued", state=tasks.QUEUED, created=old),
        ])
        handle.commit()
    finally:
        handle.close()

    tasks._prune()

    handle = database.session()
    try:
        left = sorted(row.request_id for row in handle.query(models.Task).all())
    finally:
        handle.close()
    assert left == ["new-done", "old-queued"]
