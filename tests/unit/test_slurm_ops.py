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
The slurm plugin's helper on files laid out as Luna and the Slurm app left them: drain and
resume as scontrol is asked, save and restore whole or not at all, the lock, the mark.

The helper is loaded by its path and imports nothing of trixd's, so these run wherever its
libraries are (python-hostlist, slurmlint). scontrol and sacctmgr are faked at `subprocess`.
"""

__author__      = "Alex Ninaber"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Alex Ninaber"
__status__      = "Development"

import hashlib
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent


def _by_path(name: str, path: Path):
    """A module by its file: the helper runs as a script, and neither it nor the fixture is a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLUGIN = HERE.parent.parent / "trixd" / "plugins" / "slurm"
PRESETS = _by_path("slurm_presets", HERE / "slurm_presets.py").FILES
NAMES = {"nodes": "slurm-nodes.conf", "partitions": "slurm-partitions.conf", "gres": "slurm-gres.conf"}


helper = _by_path("slurm_helper", PLUGIN / "helper.py")


def _sha(data) -> str:
    """The sha256 hex of a text or of bytes, as the helper stamps a file."""
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


@pytest.fixture
def slurm(tmp_path, monkeypatch):
    """The three files and their `.bkp` in a directory this uid owns, the share, the state, and
    scontrol/sacctmgr answering from `said` (an exception is raised)."""
    folder, share, state = tmp_path / "slurm", tmp_path / "share", tmp_path / "state"
    for d in (folder, share, state):
        d.mkdir(mode=0o755)
    files = {k: folder / NAMES[k] for k in helper.KINDS}
    for kind, path in files.items():
        for each in (path, helper.backup(path)):
            each.write_text(PRESETS[kind])
            os.chmod(each, 0o664)  # root:<admin group>, group write, as config-slurm leaves them
    ran: list = []
    said: dict = {"sacctmgr": "Administrator\n"}

    def run(args, **kw):
        """
        The fake subprocess.run: records the argv and answers what the test set for the command
        (an output, an exit code with stderr, or an exception).
        """
        ran.append(args)
        out = said.get(Path(args[0]).name, "")
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, tuple):
            return subprocess.CompletedProcess(args, out[0], "", out[1])
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(helper.os, "geteuid", lambda: os.getuid() + 1)  # the account owns nothing here
    monkeypatch.setattr(helper, "OWNER", os.getuid())  # this uid stands in for root
    monkeypatch.setattr(helper, "subprocess",
                        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(helper, "pwd",
                        SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_name="trinityx-slurm")))
    base = {"files": {k: str(p) for k, p in files.items()},
            "share_dir": str(share), "state_dir": str(state), "group": os.getgid()}

    def ask(verb, **body):
        """
        Runs one helper verb on the fixture's files, share and state with the given body fields.
        """
        return helper.answer(verb, {**base, **body})

    return SimpleNamespace(ask=ask, files=files, share=share, state=state, ran=ran, said=said)


def _stamp(slurm) -> dict:
    """The stamp of the three Slurm files as they are now, as a page sends it with a save."""
    return {k: _sha(p.read_bytes()) for k, p in slurm.files.items()}


def _bytes(slurm) -> dict:
    """Every managed file and its backup with its bytes, to show a refused write changed nothing."""
    return {str(q): q.read_bytes() for p in slurm.files.values() for q in (p, helper.backup(p))}


def _boards(text: str, n: int) -> str:
    """The thin preset with another board count: a line the editor makes."""
    return text.replace("# HWPresetName=thin Boards=1", f"# HWPresetName=thin Boards={n}", 1)


# -- the paths ----------------------------------------------------------------------------------


def test_the_files_must_be_declared_in_a_folder_the_account_cannot_change(slurm, monkeypatch):
    """
    The helper refuses with 503 when the plugin does not send all three files, when a path is not
    absolute, and when a directory above a file can be changed by the helper's account.
    """
    said = helper.answer("files", {"files": {"nodes": "/x"}, "share_dir": "/", "state_dir": "/"})
    assert said["code"] == 503 and "nodes, partitions and gres" in said["message"]
    said = slurm.ask("files", files={**{k: str(p) for k, p in slurm.files.items()}, "gres": "x"})
    assert said == {"ok": False, "code": 503, "message": "not an absolute path: 'x'"}
    folder = slurm.files["nodes"].parent
    os.chmod(folder, 0o775)
    said = slurm.ask("files")
    assert said["code"] == 503 and f"may change {folder}" in said["message"]
    os.chmod(folder, 0o757)
    assert slurm.ask("files")["code"] == 503
    os.chmod(folder, 0o755)
    assert slurm.ask("files")["code"] == 200
    monkeypatch.setattr(helper.os, "geteuid", os.getuid)  # the account owns the folder
    assert slurm.ask("files")["code"] == 503


def test_no_directory_above_the_files_may_be_the_accounts_to_change(slurm):
    """Not only the files' own folder: an ancestor the account could write could be swapped whole."""
    above = slurm.files["nodes"].parent.parent
    mode = os.stat(above).st_mode & 0o7777
    try:
        os.chmod(above, 0o775)
        said = slurm.ask("files")
        assert said["code"] == 503 and f"may change {above}" in said["message"]
        os.chmod(above, 0o1777)  # sticky and not the account's: nobody renames another's entry
        assert slurm.ask("files")["code"] == 200
    finally:
        os.chmod(above, mode)


def test_an_unknown_verb_and_a_request_not_json_are_400(slurm, monkeypatch, capsys):
    """
    An unknown verb and standard input that is not JSON are both answered 400, and main() still
    exits 0 with the answer on standard output.
    """
    assert slurm.ask("format")["code"] == 400
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: "{"))
    assert helper.main() == 0
    assert json.loads(capsys.readouterr().out)["code"] == 400


# -- drain and resume ---------------------------------------------------------------------------


@pytest.mark.parametrize("names", [
    [], "node001", ["node[001-002]"], ["ALL x"], ["node001 State=DOWN"], ["a=b"], [1],
    ["n"] * (helper.NAMES_MAX + 1),
])
def test_scontrol_is_handed_only_validated_names(slurm, names):
    """A resume with names that are not node names is refused 400 and runs nothing."""
    said = slurm.ask("resume", names=names)
    assert said["code"] == 400 and slurm.ran == []


def test_a_reason_is_one_argument_naming_the_person(slurm):
    """
    A drain runs one scontrol update with the names folded into a hostlist and the reason as one
    argument prefixed with the person.
    """
    said = slurm.ask("drain", names=["node003", "node001"], reason="bad DIMM State=DOWN",
                     person="admin")
    assert said["code"] == 200 and said["names"] == ["node001", "node003"], said
    assert slurm.ran[-1] == ["/usr/bin/scontrol", "update", "NodeName=node[001,003]",
                             "State=DRAIN", "Reason=[admin] bad DIMM State=DOWN"]


def test_a_resume_carries_no_reason(slurm):
    """A resume runs scontrol update with State=RESUME and no Reason argument."""
    assert slurm.ask("resume", names=["node002"])["code"] == 200
    assert slurm.ran[-1] == ["/usr/bin/scontrol", "update", "NodeName=node002", "State=RESUME"]


def test_a_drain_names_its_person(slurm):
    """A drain without the person is refused 400 and runs nothing."""
    said = slurm.ask("drain", names=["node001"], reason="fan")
    assert said["code"] == 400 and "person" in said["message"] and slurm.ran == []


@pytest.mark.parametrize("reason, words", [
    ("", "a drain needs a reason"),
    ("two\nlines", "one line of at most 200"),
    ("x" * 201, "one line of at most 200"),
    ("TRIX-DRAINER: NHC", "the drainer's mark"),
    ("fan\x00", "one line of at most 200"),
])
def test_the_reason_is_held_to_the_consoles_rules(slurm, reason, words):
    """
    A drain reason the console's rules refuse is refused 400 with the rule's words and runs
    nothing.
    """
    said = slurm.ask("drain", names=["node001"], reason=reason, person="admin")
    assert said["code"] == 400 and words in said["message"] and slurm.ran == []


def test_a_node_luna_does_not_render_is_refused_whole(slurm):
    """
    A resume naming one node Luna does not render into the nodes file is refused whole: no node
    is updated.
    """
    nodes = slurm.files["nodes"]
    nodes.write_text("NodeName=node009 State=UNKNOWN\n" + nodes.read_text())
    said = slurm.ask("resume", names=["node001", "node009"])
    assert said == {"ok": False, "code": 400,
                    "message": f"not nodes Luna renders into {nodes}: node009"}
    assert [a for a in slurm.ran if "update" in a] == []


def test_without_slurms_grant_nothing_is_updated(slurm):
    """
    When Slurm has not made the helper's account an Administrator, a resume is refused 503 and
    nothing is updated.
    """
    slurm.said["sacctmgr"] = "Operator\n"
    said = slurm.ask("resume", names=["node001"])
    assert said["code"] == 503 and "has not made trinityx-slurm an Administrator" in said["message"]
    assert [a for a in slurm.ran if "update" in a] == []


def test_a_silent_scontrol_is_502_saying_the_outcome_is_unknown(slurm):
    """
    scontrol timing out is 502 saying the outcome is unknown; scontrol refusing is 502 with its
    own words; scontrol missing is 503.
    """
    slurm.said["scontrol"] = subprocess.TimeoutExpired("scontrol", helper.QUICK)
    said = slurm.ask("resume", names=["node001"])
    assert said["code"] == 502 and "the outcome is unknown, reload" in said["message"]
    slurm.said["scontrol"] = (1, "slurm_update error: Invalid node name specified\n")
    said = slurm.ask("resume", names=["node001"])
    assert said == {"ok": False, "code": 502,
                    "message": "scontrol refused: slurm_update error: Invalid node name specified"}
    slurm.said["scontrol"] = FileNotFoundError(2, "No such file or directory")
    assert slurm.ask("resume", names=["node001"])["code"] == 503
    assert helper.QUICK < 8 and helper.SLOW < 60  # inside the console's own waits


def test_the_grant_is_asked_once_a_minute(slurm, monkeypatch):
    """
    The helper asks sacctmgr for its grant once per GRANT_TTL, however many requests come in
    between.
    """
    now = [1000.0]
    monkeypatch.setattr(helper.time, "time", lambda: now[0])
    for _ in range(3):
        slurm.ask("files")
    asked = [a for a in slurm.ran if a[0] == "/usr/bin/sacctmgr"]
    assert len(asked) == 1
    now[0] += helper.GRANT_TTL
    slurm.ask("files")
    assert len([a for a in slurm.ran if a[0] == "/usr/bin/sacctmgr"]) == 2
    slurm.said["sacctmgr"] = (1, "Connection refused")
    now[0] += helper.GRANT_TTL
    assert slurm.ask("files")["granted"].startswith("Slurm accounting did not answer")


# -- files --------------------------------------------------------------------------------------


def test_files_says_each_gap_and_the_stamp(slurm):
    """
    The files verb answers the account, the grant, the stamp and the rules digest, and names each
    gap (a missing backup, a wrong mode) as a sentence.
    """
    said = slurm.ask("files")
    assert said["why"] == [] and said["granted"] == "" and said["account"] == "trinityx-slurm"
    assert said["stamp"] == _stamp(slurm)
    assert said["rules"] == _sha((PLUGIN / "rules.py").read_bytes())
    gone = helper.backup(slurm.files["gres"])
    gone.unlink()
    os.chmod(slurm.files["nodes"], 0o644)
    why = slurm.ask("files")["why"]
    assert f"there is no {gone}" in why
    assert any(w.startswith(f"{slurm.files['nodes']} is not the files' group's to write") for w in why)


def test_the_helper_writes_only_root_files_of_the_group_never_a_link(slurm, monkeypatch):
    """Each of the six is checked without following a link: a regular file, root's, of the
    files' group, with group read and write; else nothing is written."""
    texts = dict(PRESETS)
    before = _bytes(slurm)
    bkp = helper.backup(slurm.files["partitions"])
    elsewhere = bkp.parent.parent / "elsewhere"
    elsewhere.write_bytes(bkp.read_bytes())
    bkp.unlink()
    bkp.symlink_to(elsewhere)
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 503 and f"{bkp} is not a regular file" in said["message"]
    bkp.unlink()
    bkp.write_bytes(elsewhere.read_bytes())
    os.chmod(bkp, 0o664)
    monkeypatch.setattr(helper, "OWNER", os.getuid() + 1)
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 503 and "is not the files' group's to write" in said["message"]
    monkeypatch.setattr(helper, "OWNER", os.getuid())
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[], group=os.getgid() + 1)
    assert said["code"] == 503 and f"of group {os.getgid() + 1}" in said["message"]
    for group in (None, "admins", True):
        said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[], group=group)
        assert said == {"ok": False, "code": 503,
                        "message": "the plugin names no group for the files"}
    assert _bytes(slurm) == before and elsewhere.read_bytes() == bkp.read_bytes()
    assert slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])["code"] == 200


def test_a_save_and_a_restore_start_no_child_process(slurm):
    """They run with the files' group: nothing else may run with it."""
    assert slurm.ask("save", texts=dict(PRESETS), stamp=_stamp(slurm), judged=[])["code"] == 200
    assert slurm.ask("restore", stamp=_stamp(slurm), judged=[])["code"] == 200
    assert slurm.ran == []
    assert helper.WRITERS == ("save", "restore")


# -- save, restore, preview ---------------------------------------------------------------------


def test_a_save_writes_the_files_and_backs_up_what_was_there(slurm):
    """
    A save of unchanged text writes nothing; a save of changed text writes the file and keeps
    what was there in its backup.
    """
    before = _bytes(slurm)
    said = slurm.ask("save", texts=dict(PRESETS), stamp=_stamp(slurm), judged=[])
    assert said["code"] == 200 and _bytes(slurm) == before
    nodes = slurm.files["nodes"]
    changed = _boards(PRESETS["nodes"], 2)
    said = slurm.ask("save", texts={**PRESETS, "nodes": changed}, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 200, said
    assert nodes.read_text() == changed
    assert helper.backup(nodes).read_bytes() == before[str(nodes)]
    assert said["stamp"]["nodes"] == _sha(changed)
    assert not helper.marked(slurm.share)


def test_a_save_needs_the_new_text_of_each_file(slurm):
    """
    A save without the text of every file, with a text that is not a string, not encodable or
    larger than TEXTS_MAX, is refused 400 and writes nothing.
    """
    big = {**PRESETS, "gres": "#" * (helper.TEXTS_MAX + 1)}
    for texts in (None, {"nodes": "x"}, {**PRESETS, "gres": 1}, {**PRESETS, "nodes": "\udc80"}, big):
        before = _bytes(slurm)
        said = slurm.ask("save", texts=texts, stamp=_stamp(slurm))
        assert said["code"] == 400 and said["message"].startswith("texts: ")
        assert _bytes(slurm) == before
    assert "more than the 4194304 a save carries" in slurm.ask("save", texts=big)["message"]


def test_the_files_are_written_whole_or_not_at_all(slurm, monkeypatch):
    """A write that fails half way puts back only the files holding the helper's bytes; the
    helper creates no file (a missing `.bkp` is 503)."""
    before = _bytes(slurm)
    texts = {**PRESETS, "nodes": _boards(PRESETS["nodes"], 2)}
    real_put = helper.put
    written: list = []

    def put(path, data):
        """
        Cuts the first write of the nodes file short and raises ENOSPC, as a full disk does;
        every other write goes through.
        """
        if path == slurm.files["nodes"] and "full" not in written:
            written.append("full")
            real_put(path, data[:10])  # cut short, as a full disk does
            raise OSError(28, "No space left on device")
        written.append(str(path))
        real_put(path, data)

    monkeypatch.setattr(helper, "put", put)
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 502 and "No space left on device" in said["message"]
    assert "put back" in said["message"]
    assert written[:3] == [str(helper.backup(p)) for p in slurm.files.values()]
    monkeypatch.setattr(helper, "put", real_put)
    assert _bytes(slurm) == before and not helper.marked(slurm.share)
    bkp = helper.backup(slurm.files["gres"])
    bkp.unlink()
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert (said["code"], said["message"]) == (503, f"there is no {bkp}")
    assert not bkp.exists()


def test_the_mark_goes_before_the_files_it_names(slurm, monkeypatch):
    """
    Every file write of a save happens while the mark naming it exists, so a cut save can be put
    back.
    """
    real_put = helper.put
    seen: list = []

    def put(path, data):
        """Records whether the mark exists at each write, then writes."""
        seen.append(helper.marked(slurm.share))
        real_put(path, data)

    monkeypatch.setattr(helper, "put", put)
    texts = {**PRESETS, "nodes": _boards(PRESETS["nodes"], 2)}
    assert slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])["code"] == 200
    assert seen and all(seen) and not helper.marked(slurm.share)


def test_a_write_a_kill_cut_short_is_put_back_before_the_next_write(slurm):
    """The mark names each file with its bytes before and after; the next save or restore puts
    back a file holding a part of the helper's bytes and leaves a file someone else wrote; until
    then the other verbs say so and apply refuses."""
    nodes, gres = slurm.files["nodes"], slurm.files["gres"]
    old_nodes, old_gres = nodes.read_bytes(), gres.read_bytes()
    new_nodes = old_nodes + b"# NodeName=node001 Boards=2\n"
    helper.mark(slurm.share, [(nodes, old_nodes, new_nodes), (gres, old_gres, old_gres + b"# x\n")])
    nodes.write_bytes(new_nodes[:7])  # the kill, half way through the first file
    gres.write_bytes(b"Luna wrote this\n")
    said = slurm.ask("files")
    assert "put_back" not in said and nodes.read_bytes() == new_nodes[:7]
    assert "a save was cut short: the next save or restore puts the files back" in said["why"]
    assert slurm.ask("apply") == {"ok": False, "code": 409,
                                  "message": "a save was cut short: save or restore first, then apply"}
    said = slurm.ask("restore", stamp=_stamp(slurm), judged=[])
    assert said["code"] == 409 and said["put_back"] == [str(nodes)]  # put back, nothing more
    assert said["message"] == "a save was cut short and its files are put back: reload and redo the change"
    assert nodes.read_bytes() == old_nodes and gres.read_bytes() == b"Luna wrote this\n"
    assert not helper.marked(slurm.share)
    assert "put_back" not in slurm.ask("restore", stamp=_stamp(slurm), judged=[])


def test_a_file_gone_since_the_mark_is_left_alone(slurm):
    """
    Putting back a cut save leaves a file deleted since the mark alone, says so, and clears the
    mark.
    """
    gres = slurm.files["gres"]
    old = gres.read_bytes()
    helper.mark(slurm.share, [(gres, old, old + b"# x\n")])
    gres.unlink()
    assert helper.recover(slurm.share, slurm.files) == [
        f"{gres} is gone since the save was cut: not put back"]
    assert not gres.exists() and not helper.marked(slurm.share)


def test_a_mark_may_name_only_the_six_files(slurm, tmp_path):
    """A mark is acted on with the files' group: one naming any other file is refused whole, that
    file untouched and the mark kept for a person to read."""
    other = tmp_path / "slurm" / "slurm.conf"
    other.write_bytes(b"ClusterName=x\n")
    nodes = slurm.files["nodes"]
    old = nodes.read_bytes()
    helper.mark(slurm.share, [(nodes, old, old + b"# x\n"), (other, b"evil", b"ClusterName=x\n")])
    said = slurm.ask("save", texts=PRESETS, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 503 and f"names files that are not the six: {other}" in said["message"]
    assert other.read_bytes() == b"ClusterName=x\n" and helper.marked(slurm.share)


@pytest.mark.parametrize("where", ["share", "pending"])
def test_a_mark_in_a_directory_others_may_write_is_refused(slurm, where):
    """
    A mark in a share or pending directory others may write is refused 503 and nothing is put
    back from it.
    """
    nodes = slurm.files["nodes"]
    old = nodes.read_bytes()
    helper.mark(slurm.share, [(nodes, old, old + b"# x\n")])
    nodes.write_bytes(old + b"# x\n")
    folder = slurm.share if where == "share" else slurm.share / "pending"
    os.chmod(folder, 0o777)
    said = slurm.ask("restore", stamp=_stamp(slurm), judged=[])
    assert said["code"] == 503 and f"{folder} is not the helper's own" in said["message"]
    assert nodes.read_bytes() == old + b"# x\n"


REFUSED = {
    "a line Slurm runs as root": (
        "nodes", "# HWPresetName=thin Boards=1", "# HWPresetName=thin Prolog=/tmp/x Boards=1"),
    "a change outside the blocks": ("gres", "", "Name=gpu File=/dev/sda\n"),
    "a Luna node commented out": ("nodes", "\nNodeName=node001 ", "\n#NodeName=node001 "),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_lines_the_editor_cannot_make_are_refused(slurm, case):
    """
    A save carrying a line the console's editor cannot make is refused 409 naming the file and
    writes nothing; a preview of it answers 200 with the same refusal.
    """
    key, old, new = REFUSED[case]
    texts = dict(PRESETS)
    texts[key] = texts[key].replace(old, new, 1) if old else texts[key] + new
    before = _bytes(slurm)
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 409 and said["message"].startswith(key + ": ")
    assert _bytes(slurm) == before
    preview = slurm.ask("preview", texts=texts, stamp=_stamp(slurm))
    assert preview["code"] == 200 and preview["refused"].startswith(key + ": ")


def test_a_stale_page_and_files_not_lunas_are_refused(slurm):
    """
    A save from a page whose stamp is stale, onto a file without Luna's TrinityX block or onto a
    file that is not text, is refused 409.
    """
    stamp = {**_stamp(slurm), "gres": _sha("older")}
    said = slurm.ask("save", texts=dict(PRESETS), stamp=stamp, judged=[])
    assert (said["code"], said["message"]) == (409, helper.rules.STALE % slurm.files["gres"])
    nodes = slurm.files["nodes"]
    nodes.write_text(nodes.read_text().replace("TrinityX Managed", "Someone Managed"))
    said = slurm.ask("save", texts=dict(PRESETS), stamp=_stamp(slurm), judged=[])
    assert said["code"] == 409 and "has no TrinityX block" in said["message"]
    nodes.write_bytes(b"\xff\xfe")
    said = slurm.ask("save", texts=dict(PRESETS), stamp=_stamp(slurm), judged=[])
    assert said["code"] == 409 and "not text" in said["message"]


def test_the_state_changes_judged_must_be_the_ones_the_text_makes(slurm):
    """
    A preview names the nodes whose State the text changes; a save whose judged list differs is
    refused 409, one that matches is written.
    """
    texts = {**PRESETS, "nodes": PRESETS["nodes"].replace(
        "Gres=mps:100 State=DOWN # GroupName", "Gres=mps:100 # GroupName", 1)}
    preview = slurm.ask("preview", texts=texts, stamp=_stamp(slurm))
    assert preview == {"ok": True, "code": 200, "states": ["node004"], "refused": ""}
    before = _bytes(slurm)
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 409 and "State changes judged" in said["message"]
    assert _bytes(slurm) == before
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=["node004"])
    assert said["code"] == 200 and slurm.files["nodes"].read_text() == texts["nodes"]


GPU_DEFAULTS = "# GRESPresetName=gpu Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1] # Nodes=node001\n"
GPU_LINE = "NodeName=node001 Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1]\n"


def test_a_gres_preset_taken_off_its_node_takes_the_nodes_gres_line_with_it(slurm):
    """gres.conf has a line per node and resource, rendered from the Defaults entries: a save that
    takes the gpu preset off node001 takes node001's gpu line in Luna's block with it, and is
    written."""
    texts = {**PRESETS, "gres": PRESETS["gres"].replace(GPU_DEFAULTS, "").replace(GPU_LINE, "")}
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 200, said
    assert slurm.files["gres"].read_text() == texts["gres"]


def test_a_nodes_gres_line_its_nodegres_entry_still_names_is_kept(slurm):
    """A NodeGres= entry left in the Defaults names node001, so the generator would render its line
    again: a save dropping that line is refused naming it, and writes nothing."""
    gres = slurm.files["gres"]
    nodegres = "# NodeGres=node001 Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1]\n"
    start = PRESETS["gres"].replace(GPU_DEFAULTS, GPU_DEFAULTS + nodegres)
    gres.write_text(start)
    texts = {**PRESETS, "gres": start.replace(GPU_DEFAULTS, "").replace(GPU_LINE, "")}
    before = _bytes(slurm)
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 409, said
    assert "gres: NodeName=node001 is Luna's: a save keeps it" in said["message"]
    assert _bytes(slurm) == before


def test_restore_holds_the_backup_to_the_rules(slurm):
    """
    A restore writes the backup back when it passes the console's rules and refuses one that does
    not, writing nothing.
    """
    nodes = slurm.files["nodes"]
    bkp = helper.backup(nodes)
    older = _boards(bkp.read_text(), 4)
    bkp.write_text(older)
    said = slurm.ask("restore", stamp=_stamp(slurm), judged=[])
    assert said["code"] == 200, said
    assert nodes.read_text() == older and bkp.read_text() == older
    bkp.write_text(older.replace("# HWPresetName=thin Boards=4", "# HWPresetName=thin Prolog=/x", 1))
    before = _bytes(slurm)
    said = slurm.ask("restore", stamp=_stamp(slurm), judged=[])
    assert said["code"] == 409 and _bytes(slurm) == before


def test_a_restore_with_a_backup_missing_writes_nothing(slurm):
    """A restore with any backup missing is refused 503 naming it and writes no file."""
    nodes = slurm.files["nodes"]
    bkp = helper.backup(nodes)
    bkp.write_text(bkp.read_text() + "# the files before the last save\n")
    gone = helper.backup(slurm.files["gres"])
    gone.unlink()
    before = {k: p.read_bytes() for k, p in slurm.files.items()}
    said = slurm.ask("restore", stamp=_stamp(slurm), judged=[])
    assert said["code"] == 503 and str(gone) in said["message"]
    assert {k: p.read_bytes() for k, p in slurm.files.items()} == before and not gone.exists()


def test_a_luna_write_during_a_save_is_named_and_kept(slurm, monkeypatch):
    """
    When Luna renders a file while a save writes it, the save names the file and keeps Luna's
    content.
    """
    gres = slurm.files["gres"]
    real_put = helper.put

    def put(path, data):
        """Writes, then renders the gres file again as Luna would in the middle of the save."""
        real_put(path, data)
        if path == gres:
            gres.write_bytes(data + b"# rendered by Luna\n")

    monkeypatch.setattr(helper, "put", put)
    texts = {**PRESETS, "nodes": _boards(PRESETS["nodes"], 2)}
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 409 and said["message"].startswith(f"{gres} changed while")
    assert gres.read_bytes().endswith(b"# rendered by Luna\n")
    assert slurm.files["nodes"].read_text() == texts["nodes"]


def test_slurmlint_judges_what_is_written(slurm):
    """The rules pass a value slurmlint refuses: 400 naming the line, nothing written."""
    line = "\nNodeName=node001 Boards=1 CoresPerSocket=8 RealMemory=64000"
    texts = {**PRESETS, "nodes": PRESETS["nodes"].replace(line, line.replace("64000", "lots"), 1)}
    assert helper.rules.changed_lines_refused(PRESETS, texts) == []
    before = _bytes(slurm)
    said = slurm.ask("save", texts=texts, stamp=_stamp(slurm), judged=[])
    assert said["code"] == 400 and said["message"].startswith("slurmlint: "), said
    assert "RealMemory=lots" in said["message"] and _bytes(slurm) == before


def test_without_slurmlint_nothing_is_written(slurm, monkeypatch):
    """Without the slurmlint module a save is refused 503 and writes nothing."""
    monkeypatch.setitem(sys.modules, "lintcheck", None)
    before = _bytes(slurm)
    said = slurm.ask("save", texts=dict(PRESETS), stamp=_stamp(slurm), judged=[])
    assert said["code"] == 503 and "cannot lint" in said["message"] and _bytes(slurm) == before


def test_apply_reconfigures(slurm):
    """Apply runs scontrol reconfigure and nothing else."""
    assert slurm.ask("apply")["code"] == 200
    assert slurm.ran == [["/usr/bin/scontrol", "reconfigure"]]


def test_a_refused_apply_says_the_saved_files_stay_and_how_to_put_them_back(slurm):
    """slurmctld keeps serving what it had; the files are left as saved (the backups need not be
    what slurm last read), and the answer says so and names Restore."""
    slurm.said["scontrol"] = (1, "error: reconfigure failed\n")
    before = _bytes(slurm)
    said = slurm.ask("apply")
    assert said == {"ok": False, "code": 502, "message": (
        "scontrol refused: error: reconfigure failed; the saved files stay on disk, where Slurm's "
        "commands read them: Restore puts back the set before the last save")}
    assert _bytes(slurm) == before


# -- the lock -----------------------------------------------------------------------------------


def _hold(share: str, ready, done) -> None:
    """
    Holds the helper's lock on the share in another process until told to let go, setting ready
    once it holds it.
    """
    with helper.locked(Path(share)):
        ready.set()
        done.wait(10)


def test_a_cut_save_is_put_back_only_under_the_lock(slurm, monkeypatch):
    """A save in flight holds the lock with its mark written: a second save or restore, from
    another process, waits for the lock and is refused; it never puts back the first one's files."""
    monkeypatch.setattr(helper, "WAIT", 0.3)
    nodes = slurm.files["nodes"]
    old = nodes.read_bytes()
    helper.mark(slurm.share, [(nodes, old, old + b"# x\n")])
    nodes.write_bytes(old + b"# x")  # the first save, part way
    ctx = multiprocessing.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(slurm.share), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        for verb in ("save", "restore", "files"):
            said = slurm.ask(verb, texts=PRESETS, stamp=_stamp(slurm), judged=[])
            assert said["code"] in (409, 200) and "put_back" not in said
            assert nodes.read_bytes() == old + b"# x" and helper.marked(slurm.share)
    finally:
        done.set()
        holder.join(10)


def test_one_write_at_a_time_across_processes(slurm, monkeypatch):
    """`lockf` is per process: another process holding the share's lock refuses a write 409
    after the wait, and the files stay as they were."""
    monkeypatch.setattr(helper, "WAIT", 0.3)
    ctx = multiprocessing.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(slurm.share), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        before = _bytes(slurm)
        with pytest.raises(helper.Refused) as refused, helper.locked(slurm.share, wait=0.3):
            pass
        assert refused.value.code == 409
        assert _bytes(slurm) == before
    finally:
        done.set()
        holder.join(10)
    with helper.locked(slurm.share, wait=0.3):
        pass


@pytest.mark.parametrize("setting, value", [
    (setting, value)
    for setting in ("nodes_file", "partitions_file", "gres_file", "reconfigure", "luna_ca")
    for value in ("", "etc/slurm/x.conf", "scontrol reconfigure")
    if (setting, value) != ("luna_ca", "")])  # luna_ca "" clears it: test_luna_ca_may_be_cleared
def test_config_refuses_an_empty_or_relative_setting(rig, client, headers, setting, value):
    """AC-12 (R2): an empty or relative path, or a command not named by its absolute path, is
    refused at the schema and nothing is stored: the render would write it relative to trixd's
    working directory, or run it from PATH, as root."""
    from trixd.core.plugins import loader
    from trixd.plugins.slurm import backend

    assert loader.load(rig, "slurm").state == "loaded"
    before = backend.Backend().config()
    answer = client.patch("/api/v1/slurm/config", json={setting: value}, headers=headers)
    assert answer.status_code == 422, answer.text  # trixd's schema refusal
    assert backend.Backend().config() == before
    kept = client.patch("/api/v1/slurm/config", json={"manager": "TrinityX"}, headers=headers)
    assert kept.status_code == 200, kept.text


def test_scontrol_update_runs_under_the_share_lock(slurm, monkeypatch):
    """F9 (PLAN-r140a section 16): the lock is held across `scontrol update`, so no render or save
    lands between the read of the node file and the update. Another process tries the lock while
    scontrol runs."""
    held = []
    run = helper.subprocess.run
    probe = ("import fcntl, os, sys\n"
             "fd = os.open(sys.argv[1], os.O_RDWR)\n"
             "try:\n    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept OSError:\n"
             "    sys.exit(3)\n")

    def scontrol(args, **kw):
        """Records whether the lock is held when scontrol runs, then runs the fake."""
        if Path(args[0]).name == "scontrol":
            held.append(subprocess.run([sys.executable, "-c", probe, str(slurm.share / "lock")],
                                       check=False).returncode == 3)
        return run(args, **kw)

    monkeypatch.setattr(helper, "subprocess",
                        SimpleNamespace(run=scontrol, TimeoutExpired=subprocess.TimeoutExpired))
    assert slurm.ask("drain", names=["node001"], reason="fan", person="admin")["code"] == 200
    assert slurm.ask("resume", names=["node001"])["code"] == 200
    assert held == [True, True]


def test_luna_ca_may_be_cleared(rig, client, headers):
    """Review 3 (1): luna_ca set wrong is undone through the API: "" stores "", which reads as
    luna.ini's VERIFY_CERTIFICATE."""
    from trixd.core.plugins import loader
    from trixd.plugins.slurm import backend

    assert loader.load(rig, "slurm").state == "loaded"
    for value in ("/etc/trinity/pki/ca.crt", ""):
        answer = client.patch("/api/v1/slurm/config", json={"luna_ca": value}, headers=headers)
        assert answer.status_code == 200, answer.text
        assert backend.Backend().config()["luna_ca"] == value
