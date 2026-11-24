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
The init of the slurm files and the genders file: what gives a file its first managed block.

The installer places these files without a block and a render writes only inside one, so the
installer calls each plugin's init once the files are there. What an init leaves is held here
against what the installer's config-slurm and config-genders roles left on a cluster installed
before, byte for byte; a render still refuses a file without a block, and says what gives one.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import json
import multiprocessing
import os
import socket
import stat
import sys
from types import SimpleNamespace

import pytest

from tests.unit.test_plugins import Ran
from tests.unit.test_slurm_changes import _hold
from tests.unit.test_slurm_ops import helper
from trixd.common.errors import BackendFailure, Conflict
from trixd.utils import blocks

KINDS: tuple = ("nodes", "partitions", "gres")
# The three files as the installer's slurm role places them (its templates), and the genders
# file as its pdsh role does.
INSTALLED: dict = {
    "nodes": ("NodeName=DEFAULT Boards=1 SocketsPerBoard=1 CoresPerSocket=1 ThreadsPerCore=1 "
              "RealMemory=100 State=UNKNOWN\n"
              "\n"
              "# Below line is an example/placeholder and can be altered\n"
              "# NodeName=dummynode\n"
              "\n"
              "\n"),
    "partitions": ("PartitionName=defq   Nodes=ALL Shared=YES MaxTime=INFINITE State=UP "
                   "Default=YES AllowAccounts=ALL\n"
                   "\n"
                   "\n"),
    "gres": "# File to configure slurm gres for nodes\n",
}
INSTALLED_GENDERS: str = ""

# What the installer's config-slurm and config-genders roles appended to such a file, taken
# from a run of their own tasks over the files above.
ROLE_PRESETS: str = (
    "# Defaults blocks are for trinityx-config-slurm to read and append generated config.\n"
    "# The settings and values can be altered and will not be overwritten.\n"
    "#### Defaults Managed block start ####\n"
    "#### Defaults Managed block end   ####\n"
    "\n"
    "\n")
ROLE_BLOCK: str = (
    "# TrinityX will only manage inside 'TrinityX Managed block'.\n"
    "# If manual override is desired, the 'TrinityX Managed block' can be removed.\n"
    "#### TrinityX Managed block start ####\n"
    "#### TrinityX Managed block end   ####\n"
    "\n"
    "\n")

NODES: dict = {"node001": {"group": "compute"}, "node002": {"group": "compute"}}
ROOTUS = SimpleNamespace(rootus=True, name="trixd")
SLURM_INIT: str = "POST /api/v1/slurm/files/actions/init"
GENDERS_INIT: str = "POST /api/v1/genders/files/actions/init"


def _refusal(call) -> str:
    """
    What the call refused with, or nothing where it went through.
    """
    try:
        call()
    except (BackendFailure, Conflict) as error:
        return str(error)
    return ""


def _emptied(text: str) -> str:
    """
    The file with our block holding nothing, which is the file as an init alone leaves it.
    """
    return blocks.replace(text, "TrinityX", "")


def _engine(monkeypatch, plugin: str, path: str, producer) -> None:
    """
    The plugin's render in the engine, as the loader puts it there, so an init can run it.
    """
    from trixd.core.plugins.manifest import Render
    from trixd.core.render import engine

    item = engine.Render(plugin, path, Render(inputs=["luna.node"]), producer)
    monkeypatch.setitem(engine.current().renders, item.key, item)


@pytest.fixture()
def slurm(rig, monkeypatch, tmp_path):
    """
    The slurm plugin over three files and their backups as the installer leaves them - the
    slurm role's templates, of the files' group with group write - with Luna's nodes, a
    resolver that knows every name, scontrol recorded, and the helper answering in this
    process for the restore that follows a reset.
    """
    from trixd.plugins.slurm import backend
    from trixd.utils import declarations

    folder = tmp_path / "slurm"
    folder.mkdir(mode=0o755)
    (folder / backend.SHARE).mkdir(mode=0o700)
    files = {"nodes": folder / "slurm-nodes.conf", "partitions": folder / "slurm-partitions.conf",
             "gres": folder / "gres.conf"}
    for kind, path in files.items():
        for each in (path, helper.backup(path)):
            each.write_text(INSTALLED[kind], encoding="utf-8")
            os.chmod(each, 0o664)
    declarations.write("slurm", "config", "", {
        **backend.DEFAULT_CONFIG, **{f"{kind}_file": str(path) for kind, path in files.items()}})

    # The helper as test_slurm_ops stands it up: this uid for root, the account another.
    monkeypatch.setattr(helper.os, "geteuid", lambda: os.getuid() + 1)
    real_stat, above = os.stat, set(map(str, tmp_path.parents))
    monkeypatch.setattr(helper.os, "stat", lambda path, *args, **kwargs: os.stat_result(
        (0o40755, 0, 0, 2, 0, 0, 0, 0, 0, 0)) if str(path) in above
        else real_stat(path, *args, **kwargs))
    monkeypatch.setattr(helper, "OWNER", os.getuid())
    ran = []

    def run(argv, timeout=None, check=False, secret_input=""):
        """
        shell.run: the helper's verb answered in this process, anything else recorded.
        """
        if secret_input:
            return Ran(json.dumps(helper.answer(argv[-1], json.loads(secret_input))))
        ran.append(argv)
        return Ran()

    monkeypatch.setattr(backend.shell, "run", run)
    monkeypatch.setattr(backend, "pwd", SimpleNamespace(getpwnam=lambda name: name))
    monkeypatch.setattr(backend, "grp", SimpleNamespace(
        getgrnam=lambda name: SimpleNamespace(gr_gid=os.getgid())))
    monkeypatch.setattr(backend, "SETPRIV", sys.executable)
    monkeypatch.setattr(backend, "PRLIMIT", sys.executable)
    monkeypatch.setattr(backend.audit, "record", lambda *args, **kwargs: None)
    monkeypatch.setattr(backend, "luna", lambda: SimpleNamespace(nodes=lambda: NODES))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.141.0.1", 0))])
    plugin = backend.Backend()
    _engine(monkeypatch, "slurm", "renders/config", plugin.render_config)
    return SimpleNamespace(plugin=plugin, files=files, ran=ran, backend=backend,
                           share=folder / backend.SHARE)


def _texts(slurm) -> dict:
    """
    What each of the three files holds now.
    """
    return {kind: path.read_text(encoding="utf-8") for kind, path in slurm.files.items()}


def _backups(slurm) -> dict:
    """
    What the backup of each of the three files holds now.
    """
    return {kind: helper.backup(path).read_text(encoding="utf-8")
            for kind, path in slurm.files.items()}


def _with_a_preset(slurm) -> dict:
    """
    The three files with a preset of a site's own in the node file, as the editor writes
    one, and what each then holds.
    """
    nodes = slurm.files["nodes"]
    nodes.write_text(nodes.read_text(encoding="utf-8").replace(
        "#### Defaults Managed block start ####\n",
        "#### Defaults Managed block start ####\n# HWPresetName=thin Boards=1\n"),
        encoding="utf-8")
    return _texts(slurm)


def _init(slurm, **body) -> dict:
    """
    The init route as rootus calls it, which is how the installer does.
    """
    return slurm.plugin.init(body=body, caller=ROOTUS, request=None)


@pytest.fixture()
def genders(rig, monkeypatch, tmp_path):
    """
    The genders plugin on Luna's nodes and the installer's genders file: empty, in a shared
    tree, behind the link the plugin is pointed at.
    """
    from trixd.plugins.genders import backend

    monkeypatch.setattr("trixd.plugins.slurm.backend.luna",
                        lambda: SimpleNamespace(nodes=lambda: NODES))
    shared = tmp_path / "shared"
    shared.mkdir()
    real, link = shared / "genders", tmp_path / "genders"
    real.write_text(INSTALLED_GENDERS, encoding="utf-8")
    link.symlink_to(real)
    monkeypatch.setitem(backend.WRITES, "genders_file", str(link))
    plugin = backend.Backend()
    _engine(monkeypatch, "genders", "renders/genders", plugin.render_genders)
    return SimpleNamespace(plugin=plugin, file=link, real=real)


def test_the_slurm_render_refuses_the_installers_files_and_names_the_init(slurm):
    """
    The installer's files carry no block, so the render writes none of the three, as it
    writes no file a site took the block out of, and its refusal says what gives them one.
    """
    refused = _refusal(slurm.plugin.render_config)
    assert "has no TrinityX managed block" in refused, refused
    assert _texts(slurm) == INSTALLED, "a render wrote a file that has no block of ours"
    assert SLURM_INIT in refused, refused


def test_the_genders_render_refuses_the_installers_file_and_names_the_init(genders):
    """
    The installer's genders file is empty and carries no block: the render leaves it so,
    and its refusal says what gives it one.
    """
    refused = _refusal(genders.plugin.render_genders)
    assert "has no TrinityX managed block" in refused, refused
    assert genders.real.read_text(encoding="utf-8") == INSTALLED_GENDERS
    assert GENDERS_INIT in refused, refused


def test_a_slurm_init_leaves_the_files_the_installers_roles_left(slurm):
    """
    An init over the installer's files leaves each as config-slurm left it, byte for byte,
    with Luna's nodes rendered into our block before it answers and the installer's own file
    in the backup. A second init has nothing to give: it is refused and nothing moves.
    """
    said = _init(slurm)
    assert said == {"files": sorted(map(str, slurm.files.values())), "rendered": True}
    wrote = _texts(slurm)
    assert {kind: _emptied(text) for kind, text in wrote.items()} == {
        kind: INSTALLED[kind] + ROLE_PRESETS + ROLE_BLOCK for kind in KINDS}
    assert (blocks.block(wrote["nodes"], "TrinityX") or "").splitlines() == [
        "", "NodeName=node001 # GroupName=compute", "NodeName=node002 # GroupName=compute",
        "", "# Nodesets", "NodeSet=compute Nodes=node[001-002]"], wrote["nodes"]
    assert _backups(slurm) == INSTALLED
    assert slurm.ran == [["/usr/bin/scontrol", "reconfigure"]]

    refused = _refusal(lambda: _init(slurm))
    assert "carry the TrinityX managed block already" in refused, refused
    assert (_texts(slurm), _backups(slurm)) == (wrote, INSTALLED), \
        "an init that was refused moved a file"


def test_a_slurm_init_gives_only_a_file_without_a_block_its_blocks(slurm):
    """
    One file of the three without our block is what stops the render for all three, and an
    init gives that file its blocks: at its end, every line it held kept, the others as they
    were apart from what the render then writes into them.
    """
    _init(slurm)
    before = _texts(slurm)
    theirs = INSTALLED["gres"] + "NodeName=login01 Name=gpu Count=1\n"
    slurm.files["gres"].write_text(theirs, encoding="utf-8")
    assert "has no TrinityX managed block" in _refusal(slurm.plugin.render_config)

    assert _init(slurm)["rendered"] is True
    wrote = _texts(slurm)
    assert _emptied(wrote["gres"]) == theirs + ROLE_PRESETS + ROLE_BLOCK, wrote["gres"]
    assert {kind: wrote[kind] for kind in ("nodes", "partitions")} == {
        kind: before[kind] for kind in ("nodes", "partitions")}


def test_a_slurm_reset_puts_the_installers_files_back_and_keeps_what_was_there(slurm):
    """
    A reset leaves the three files as an init over a fresh installation does, whatever they
    held, and what they held is in the backups. Without `reset` the same files are refused.
    """
    _init(slurm)
    held = _with_a_preset(slurm)
    assert _refusal(lambda: _init(slurm)), "an init over initialised files was not refused"
    assert _texts(slurm) == held

    said = _init(slurm, reset=True)
    assert said["rendered"] is True, said
    wrote = _texts(slurm)
    assert {kind: _emptied(text) for kind, text in wrote.items()} == {
        kind: INSTALLED[kind] + ROLE_PRESETS + ROLE_BLOCK for kind in KINDS}
    assert "NodeName=node001" in (blocks.block(wrote["nodes"], "TrinityX") or "")
    assert _backups(slurm) == held, "a reset did not keep the files it replaced"


def test_a_restore_undoes_a_slurm_reset(slurm):
    """
    The backups a reset leaves are the ones Restore writes back, so a reset of files whose
    lines outside the blocks are the installer's is undone by the route that already exists.
    """
    _init(slurm)
    held = _with_a_preset(slurm)
    from trixd.plugins.slurm import backend

    _init(slurm, reset=True)
    assert _texts(slurm) != held
    stamp = {kind: helper.sha(path.read_bytes()) for kind, path in slurm.files.items()}
    backend.helper("restore", slurm.plugin.config(), {"stamp": stamp, "judged": []})
    assert _texts(slurm) == held


def test_a_slurm_init_is_a_persons_change(slurm):
    """
    An init is asked as a save is: by a person or rootus, never a service or a node.
    """
    from trixd.common.errors import Forbidden

    with pytest.raises(Forbidden):
        slurm.plugin.init(body={}, caller=SimpleNamespace(rootus=False, name="svc"),
                          request=SimpleNamespace(headers={}))
    assert _texts(slurm) == INSTALLED


def test_a_slurm_init_writes_nothing_while_another_change_holds_the_files(slurm):
    """
    The init takes the lock the render and a save take. Held by another process, a save on
    either controller, it is refused as a conflict to ask again, and no file moves.
    """
    ctx = multiprocessing.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(slurm.share / "lock"), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        with pytest.raises(Conflict, match="another Slurm change holds the slurm files"):
            _init(slurm)
        assert (_texts(slurm), _backups(slurm)) == (INSTALLED, INSTALLED)
    finally:
        done.set()
        holder.join(10)
    assert _init(slurm)["rendered"] is True


def test_a_slurm_init_makes_the_backup_that_is_not_there_as_the_file_is(slurm):
    """
    A file with no `.bkp` beside it gets one holding what the file held, of the file's own
    mode, so the save that comes later finds a backup it may write.
    """
    backup = helper.backup(slurm.files["gres"])
    backup.unlink()
    _init(slurm)
    assert backup.read_text(encoding="utf-8") == INSTALLED["gres"]
    assert stat.S_IMODE(backup.stat().st_mode) == stat.S_IMODE(
        slurm.files["gres"].stat().st_mode) == 0o664


def test_a_slurm_init_that_cannot_write_a_file_puts_the_others_back(slurm, monkeypatch):
    """
    The three are loaded together, so a write failing on the second leaves none of them
    with the init's text: the first goes back to what it held, and the failure says so.
    """
    real_put, partitions = blocks.put, str(slurm.files["partitions"])

    def put(path, text, follow=False):
        """blocks.put, failing on the partition file as a full disk would."""
        if path == partitions:
            raise OSError(28, "No space left on device")
        real_put(path, text, follow)

    monkeypatch.setattr(blocks, "put", put)
    refused = _refusal(lambda: _init(slurm))
    assert f"cannot write {partitions}" in refused, refused
    assert "the three files are as they were" in refused, refused
    assert _texts(slurm) == INSTALLED


def test_slurmlint_judges_what_a_slurm_init_writes(slurm):
    """
    An init keeps a site's own lines, so one slurm cannot load is refused as a save of it
    is, with slurmlint's words and nothing written.
    """
    pytest.importorskip("slurmlint")
    from trixd.common.errors import BadRequest

    theirs = INSTALLED["nodes"] + "NodeName=login01 RealMemory=lots\n"
    slurm.files["nodes"].write_text(theirs, encoding="utf-8")
    with pytest.raises(BadRequest, match="slurmlint: .*RealMemory=lots"):
        _init(slurm)
    assert _texts(slurm) == {**INSTALLED, "nodes": theirs} and _backups(slurm) == INSTALLED


def test_a_slurm_init_goes_ahead_where_slurmlint_is_not_installed(slurm, monkeypatch):
    """
    A controller without slurmlint still has to be installed: the init writes there as the
    render writes, with slurm itself the judge of what it loads.
    """
    monkeypatch.setitem(sys.modules, "trixd.plugins.slurm.lintcheck", None)
    theirs = INSTALLED["nodes"] + "NodeName=login01 RealMemory=lots\n"
    slurm.files["nodes"].write_text(theirs, encoding="utf-8")
    assert _init(slurm)["rendered"] is True
    assert _emptied(_texts(slurm)["nodes"]) == theirs + ROLE_PRESETS + ROLE_BLOCK


def test_a_slurm_init_is_refused_where_a_file_is_not_there(slurm):
    """
    A file the installer has not placed is not made here: its template would never land
    over one this daemon created, so the init is refused and creates nothing.
    """
    slurm.files["gres"].unlink()
    assert "cannot read" in _refusal(lambda: _init(slurm))
    assert not slurm.files["gres"].exists()
    assert {kind: slurm.files[kind].read_text(encoding="utf-8")
            for kind in ("nodes", "partitions")} == {
        kind: INSTALLED[kind] for kind in ("nodes", "partitions")}


def test_a_genders_init_leaves_the_file_the_installers_role_left(genders):
    """
    An init over the installer's empty file leaves it as config-genders left it, byte for
    byte, written through the link, with Luna's nodes rendered into our block before it
    answers. A second init has nothing to give: it is refused and the file does not move.
    """
    said = genders.plugin.init(body={})
    assert said == {"file": str(genders.file), "rendered": True}
    wrote = genders.real.read_text(encoding="utf-8")
    assert genders.file.is_symlink()
    assert _emptied(wrote) == INSTALLED_GENDERS + ROLE_BLOCK, wrote
    assert (blocks.block(wrote, "TrinityX") or "").splitlines() == [
        "", "node001 all,compute", "node002 all,compute"], wrote

    refused = _refusal(lambda: genders.plugin.init(body={}))
    assert "carries the TrinityX managed block already" in refused, refused
    assert genders.real.read_text(encoding="utf-8") == wrote


def test_a_genders_init_keeps_what_a_file_without_a_block_holds(genders):
    """
    A file with lines of a site's own and no block of ours gains the block at its end, as
    the installer's role gave it one, and keeps every line.
    """
    theirs = "# the site's own genders file\nlogin01 all,login\n"
    genders.real.write_text(theirs, encoding="utf-8")
    assert genders.plugin.init(body={})["rendered"] is True
    assert _emptied(genders.real.read_text(encoding="utf-8")) == theirs + ROLE_BLOCK


def test_a_genders_reset_leaves_the_block_alone_and_keeps_what_was_there(genders):
    """
    A reset leaves the file an init over the installer's leaves, whatever it held, and what
    it held is kept beside the file the link points at.
    """
    held = "login01 all,login\n" + ROLE_BLOCK
    genders.real.write_text(held, encoding="utf-8")
    said = genders.plugin.init(body={"reset": True})
    backup = str(genders.real) + ".bkp"
    assert said == {"file": str(genders.file), "backup": backup, "rendered": True}
    with open(backup, "r", encoding="utf-8") as handle:
        assert handle.read() == held
    wrote = genders.real.read_text(encoding="utf-8")
    assert genders.file.is_symlink()
    assert _emptied(wrote) == INSTALLED_GENDERS + ROLE_BLOCK, wrote
    assert "node001 all,compute" in wrote


def test_a_genders_init_is_refused_where_the_file_is_not_there(genders):
    """
    A file the installer has not placed is not made here: the installer wants a link into
    the shared tree where this would leave a file.
    """
    genders.real.unlink()
    assert "cannot read" in _refusal(lambda: genders.plugin.init(body={}))
    assert not genders.real.exists()
