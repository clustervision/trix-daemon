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
from trixd.common.errors import (BackendFailure, BadRequest, Conflict, Forbidden,
                                 ServiceUnavailable, TrixdError)
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
# A line of a site's own, outside the blocks: no save writes there.
SITE_LINE: str = "# the site's own note above the blocks\n"
SLURM_INIT: str = "POST /api/v1/slurm/files/actions/init"
GENDERS_INIT: str = "POST /api/v1/genders/files/actions/init"


def _refusal(call) -> str:
    """
    What the call refused with, or nothing where it went through.
    """
    try:
        call()
    except TrixdError as error:
        return str(error)
    return ""


def _emptied(text: str) -> str:
    """
    The file with our block holding nothing, which is the file as an init alone leaves it.
    """
    return blocks.replace(text, "TrinityX", "")


@pytest.fixture(autouse=True)
def _levels_forgotten():
    """
    A render run for real makes the engine's logger remember which levels were off, and
    that outlives the test. Setting its level again forgets it, so a later test that
    listens to that logger hears what it asks for.
    """
    yield
    from trixd.core.render import engine

    engine.LOGGER.setLevel(engine.LOGGER.level)


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
    monkeypatch.setattr(backend, "OWNER", os.getuid(), raising=False)
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


def _with_a_line_of_its_own(slurm) -> dict:
    """
    The three files with a preset in the node file and a line of a site's own above the
    blocks, which no save could have written, and what each then holds.
    """
    _with_a_preset(slurm)
    nodes = slurm.files["nodes"]
    nodes.write_text(SITE_LINE + nodes.read_text(encoding="utf-8"), encoding="utf-8")
    return _texts(slurm)


def _bare(text: str) -> str:
    """
    The file with both blocks holding nothing: what it carries outside them.
    """
    return blocks.replace(_emptied(text), "Defaults", "")


def _body(slurm, judged=(), force=True) -> dict:
    """
    What a restore is asked with: the files' stamp as they are, the rules, the State changes.
    """
    stamp = {kind: helper.sha(path.read_bytes()) for kind, path in slurm.files.items()}
    return {"stamp": stamp, "judged": list(judged), "rules": slurm.backend.rules_digest(),
            "force": force or None}


def _restore(slurm, judged=(), force=True, caller=ROOTUS, request=None) -> dict:
    """
    The restore route, forced unless told otherwise, as rootus calls it.
    """
    return slurm.plugin.restore(body=_body(slurm, judged, force), caller=caller,
                                request=request)


def _person(slurm, monkeypatch, refused: dict) -> dict:
    """
    A person behind the request, and Luna refusing them what `refused` names per table.
    """
    from trixd.plugins.luna import persons

    monkeypatch.setattr(slurm.backend.persons, "caller",
                        lambda headers: persons.Person("alice", None))
    monkeypatch.setattr(slurm.backend.persons, "may", lambda person, table, names, bit: {
        name: why for name, why in refused.get(table, {}).items() if name in names})
    return {"caller": SimpleNamespace(rootus=False, name="alice"),
            "request": SimpleNamespace(headers={})}


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


def test_a_slurm_reset_empties_the_presets_and_keeps_the_lines_outside_the_blocks(slurm):
    """
    A reset leaves every line outside the two blocks as it was and the presets empty, with
    Luna's nodes rendered into ours and what the files held in the backups. Without `reset`
    the same files are refused.
    """
    _init(slurm)
    held = _with_a_line_of_its_own(slurm)
    assert _refusal(lambda: _init(slurm)), "an init over initialised files was not refused"
    assert _texts(slurm) == held

    said = _init(slurm, reset=True)
    assert said["rendered"] is True, said
    wrote = _texts(slurm)
    assert wrote["nodes"].startswith(SITE_LINE), wrote["nodes"]
    assert {kind: _bare(text) for kind, text in wrote.items()} == {
        kind: _bare(text) for kind, text in held.items()}
    assert blocks.block(wrote["nodes"], "Defaults") == "", wrote["nodes"]
    assert "NodeName=node001" in (blocks.block(wrote["nodes"], "TrinityX") or "")
    assert _backups(slurm) == held, "a reset did not keep the files it replaced"


def test_a_forced_slurm_reset_puts_the_installers_files_back_and_keeps_what_was_there(slurm):
    """
    A forced reset leaves the three files as an init over a fresh installation does,
    whatever they held, and what they held is in the backups. `force` alone asks for
    nothing, so it is refused as a bad request and no file moves.
    """
    _init(slurm)
    held = _with_a_line_of_its_own(slurm)
    backups = _backups(slurm)
    with pytest.raises((BadRequest, Conflict)) as refused:
        _init(slurm, force=True)
    assert isinstance(refused.value, BadRequest), refused.value
    assert (_texts(slurm), _backups(slurm)) == (held, backups)

    said = _init(slurm, reset=True, force=True)
    assert said["rendered"] is True, said
    wrote = _texts(slurm)
    assert {kind: _emptied(text) for kind, text in wrote.items()} == {
        kind: INSTALLED[kind] + ROLE_PRESETS + ROLE_BLOCK for kind in KINDS}
    assert "NodeName=node001" in (blocks.block(wrote["nodes"], "TrinityX") or "")
    assert _backups(slurm) == held, "a reset did not keep the files it replaced"


def test_a_restore_undoes_a_slurm_reset(slurm):
    """
    The backups a reset leaves are the ones Restore writes back, so a reset that emptied
    only what the editor writes is undone by the unforced route, held to a save's rules.
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


def test_a_genders_reset_renders_the_block_afresh_and_keeps_the_lines_outside_it(genders):
    """
    A reset keeps every line outside our block and renders the block afresh, and what the
    file held is kept beside the file the link points at.
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
    assert _emptied(wrote) == held, wrote
    assert "node001 all,compute" in wrote


def test_a_forced_genders_reset_leaves_the_block_alone_and_keeps_what_was_there(genders):
    """
    A forced reset leaves the file an init over the installer's leaves, whatever it held,
    and what it held is kept beside the file the link points at. `force` alone is refused.
    """
    held = "login01 all,login\n" + ROLE_BLOCK
    genders.real.write_text(held, encoding="utf-8")
    with pytest.raises((BadRequest, Conflict)) as refused:
        genders.plugin.init(body={"force": True})
    assert isinstance(refused.value, BadRequest), refused.value
    assert genders.real.read_text(encoding="utf-8") == held

    said = genders.plugin.init(body={"reset": True, "force": True})
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


@pytest.mark.parametrize("force", (False, True))
def test_a_forced_restore_undoes_a_slurm_reset_whatever_the_lines(slurm, force):
    """
    A site's own line outside the blocks is in the backups either reset leaves, and no
    unforced restore writes a set a save could not have made. Forced, the three come back as
    they were, Luna's nodes rendered, and the files the reset left are the backups.
    """
    _init(slurm)
    held = _with_a_line_of_its_own(slurm)
    _init(slurm, reset=True, force=force)
    left = _texts(slurm)
    assert _backups(slurm) == held

    refused = _refusal(lambda: _restore(slurm))
    assert not refused, refused
    assert _texts(slurm) == held
    assert _backups(slurm) == left, "a forced restore did not keep the files it replaced"


def test_a_forced_restore_of_the_installers_backup_still_holds_lunas_nodes(slurm):
    """
    The backup an init leaves is the installer's file, with no block at all. Forced back it
    gains the blocks an init gives, and the render writes Luna's nodes before the answer:
    restored to the letter, slurm would have no nodes.
    """
    _init(slurm)
    left = _texts(slurm)
    said = {}
    refused = _refusal(lambda: said.update(_restore(slurm)))
    assert not refused, refused
    assert said["rendered"] is True, said
    wrote = _texts(slurm)
    assert {kind: _emptied(text) for kind, text in wrote.items()} == {
        kind: INSTALLED[kind] + ROLE_PRESETS + ROLE_BLOCK for kind in KINDS}
    assert "NodeName=node002" in (blocks.block(wrote["nodes"], "TrinityX") or "")
    assert said["stamp"] == {kind: helper.sha(path.read_bytes())
                             for kind, path in slurm.files.items()}
    assert _backups(slurm) == left


def test_a_forced_restore_is_undone_by_a_second_one(slurm):
    """
    The files as they were become the backups, so the same route asked again swaps them
    back, as often as it is asked.
    """
    _init(slurm)
    held = _with_a_line_of_its_own(slurm)
    _init(slurm, reset=True)
    left = _texts(slurm)
    for files, backups in ((held, left), (left, held), (held, left)):
        refused = _refusal(lambda: _restore(slurm))
        assert not refused, refused
        assert (_texts(slurm), _backups(slurm)) == (files, backups)


def _with_a_state_to_lose(slurm) -> dict:
    """
    Files whose backups lack a preset that sets node001's State, and a site's own line: a
    forced restore takes that State away again. What the files then hold.
    """
    _init(slurm)
    _init(slurm, reset=True)
    nodes = slurm.files["nodes"]
    nodes.write_text(SITE_LINE + nodes.read_text(encoding="utf-8").replace(
        "#### Defaults Managed block start ####\n",
        "#### Defaults Managed block start ####\n# NodeName=node001 State=DOWN\n"),
        encoding="utf-8")
    return _texts(slurm)


def test_a_forced_restore_is_held_to_the_state_changes_it_makes(slurm):
    """
    A State on a preset reaches the node the render next writes, so the node is judged as a
    save's is: the forced preview names it, a restore that does not is refused with nothing
    written, and one that does goes through.
    """
    held = _with_a_state_to_lose(slurm)
    backups = _backups(slurm)
    said = slurm.plugin.preview(body={**_body(slurm), "mode": "restore", "texts": None},
                                caller=ROOTUS, request=None)
    assert said == {"states": ["node001"], "refused": ""}

    with pytest.raises(Conflict) as refused:
        _restore(slurm)
    assert "the State changes judged are not the ones this text makes" in str(refused.value)
    assert (_texts(slurm), _backups(slurm)) == (held, backups)

    refused = _refusal(lambda: _restore(slurm, judged=["node001"]))
    assert not refused, refused
    assert _backups(slurm) == held


def test_a_state_set_outside_the_blocks_counts_every_node(slurm):
    """
    The save rules read the blocks alone. A line slurm reads outside them that sets a State
    reaches any node, so where the backups and the files differ in one, every node is judged.
    """
    _init(slurm)
    _init(slurm, reset=True)
    nodes = slurm.files["nodes"]
    nodes.write_text(nodes.read_text(encoding="utf-8").replace("State=UNKNOWN", "State=DOWN"),
                     encoding="utf-8")
    said = slurm.plugin.preview(body={**_body(slurm), "mode": "restore", "texts": None},
                                caller=ROOTUS, request=None)
    assert said == {"states": ["node001", "node002"], "refused": ""}


def test_a_forced_restore_of_files_that_moved_is_refused(slurm):
    """
    The stamp is held as a save's is: files that changed since they were read are not
    swapped away unseen.
    """
    _init(slurm)
    _with_a_line_of_its_own(slurm)
    _init(slurm, reset=True, force=True)
    body = _body(slurm)
    nodes = slurm.files["nodes"]
    nodes.write_text(nodes.read_text(encoding="utf-8") + "# written meanwhile\n",
                     encoding="utf-8")
    held, backups = _texts(slurm), _backups(slurm)
    with pytest.raises(Conflict, match="changed since this page loaded it"):
        slurm.plugin.restore(body=body, caller=ROOTUS, request=None)
    assert (_texts(slurm), _backups(slurm)) == (held, backups)


def test_a_forced_restore_needs_the_rights_a_save_needs(slurm, monkeypatch):
    """
    Force lifts the rule on changed lines and nothing else: Luna's w on the cluster, and the
    hardware right on each node whose State changes, asked as the person.
    """
    held = _with_a_state_to_lose(slurm)
    backups = _backups(slurm)
    for refused in ({"cluster": {"cluster": "changing cluster cluster is not permitted"}},
                    {"node": {"node001": "node001: no hardware right"}}):
        who = _person(slurm, monkeypatch, refused)
        with pytest.raises(Forbidden):
            _restore(slurm, judged=["node001"], **who)
        assert (_texts(slurm), _backups(slurm)) == (held, backups)

    who = _person(slurm, monkeypatch, {})
    refused = _refusal(lambda: _restore(slurm, judged=["node001"], **who))
    assert not refused, refused


def test_slurmlint_judges_what_a_forced_restore_writes(slurm, monkeypatch):
    """
    A backup's line slurm cannot load is refused with slurmlint's words and nothing written.
    Where slurmlint is not installed the restore goes ahead, as the init it undoes does.
    """
    pytest.importorskip("slurmlint")
    _init(slurm)
    backup = helper.backup(slurm.files["nodes"])
    backup.write_text(INSTALLED["nodes"] + "NodeName=login01 RealMemory=lots\n",
                      encoding="utf-8")
    held, backups = _texts(slurm), _backups(slurm)
    with pytest.raises((BadRequest, Conflict)) as refused:
        _restore(slurm)
    assert isinstance(refused.value, BadRequest), refused.value
    assert "slurmlint: " in str(refused.value) and "RealMemory=lots" in str(refused.value)
    assert (_texts(slurm), _backups(slurm)) == (held, backups)

    monkeypatch.setitem(sys.modules, "trixd.plugins.slurm.lintcheck", None)
    refused = _refusal(lambda: _restore(slurm))
    assert not refused, refused
    assert "NodeName=login01 RealMemory=lots\n" in _texts(slurm)["nodes"]


def test_a_forced_restore_writes_nothing_while_another_change_holds_the_files(slurm):
    """
    The forced restore takes the lock an init takes and does not wait for it: held by
    another process, it is a conflict to ask again, and none of the six moves.
    """
    _init(slurm)
    held, backups = _texts(slurm), _backups(slurm)
    ctx = multiprocessing.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold, args=(str(slurm.share / "lock"), ready, done))
    holder.start()
    try:
        assert ready.wait(10)
        with pytest.raises(Conflict, match="so the restore wrote nothing"):
            _restore(slurm)
        assert (_texts(slurm), _backups(slurm)) == (held, backups)
    finally:
        done.set()
        holder.join(10)


@pytest.mark.parametrize("failing", ("slurm-partitions.conf", "slurm-partitions.conf.bkp"))
def test_a_forced_restore_that_cannot_write_puts_all_six_back(slurm, monkeypatch, failing):
    """
    A write failing part-way, on a file or on a backup, leaves the three files and the three
    backups holding what they held, and the failure says so.
    """
    from trixd.plugins.slurm import configfiles

    _init(slurm)
    _with_a_line_of_its_own(slurm)
    _init(slurm, reset=True, force=True)
    held, backups = _texts(slurm), _backups(slurm)
    real_put, real_keep = blocks.put, configfiles._keep

    def put(path, text, follow=False):
        """blocks.put, failing on the file named as a full disk would."""
        if os.path.basename(path) == failing:
            raise OSError(28, "No space left on device")
        real_put(path, text, follow)

    def keep(path, data):
        """configfiles._keep, failing on the backup named as a full disk would."""
        if os.path.basename(path) + ".bkp" == failing:
            raise OSError(28, "No space left on device")
        real_keep(path, data)

    monkeypatch.setattr(blocks, "put", put)
    monkeypatch.setattr(configfiles, "_keep", keep)
    refused = _refusal(lambda: _restore(slurm))
    assert f"cannot write {slurm.files['partitions'].parent / failing}" in refused, refused
    assert "the three files and their backups are as they were" in refused, refused
    assert (_texts(slurm), _backups(slurm)) == (held, backups)


def test_a_forced_restore_names_the_backup_it_could_not_put_back(slurm, monkeypatch):
    """
    Where a backup was cut short and will not be written again, the failure names it rather
    than saying all is as it was; the files still go back.
    """
    from trixd.plugins.slurm import configfiles

    _init(slurm)
    held = _texts(slurm)
    backup = helper.backup(slurm.files["partitions"])
    real_keep = configfiles._keep

    def keep(path, data):
        """configfiles._keep, cutting the partition file's backup short and failing."""
        if path == str(slurm.files["partitions"]):
            backup.write_text("", encoding="utf-8")
            raise OSError(28, "No space left on device")
        real_keep(path, data)

    monkeypatch.setattr(configfiles, "_keep", keep)
    refused = _refusal(lambda: _restore(slurm))
    assert f"{backup} could not be put back" in refused, refused
    assert _texts(slurm) == held


def test_a_forced_restore_reads_a_backup_only_as_a_plain_file_of_its_own(slurm, monkeypatch):
    """
    The backups are written by the admin group and read here as root, so one that is a link,
    has a second name or is larger than a save's texts is refused, and nothing is written -
    not through the link either.
    """
    _init(slurm)
    held, backups = _texts(slurm), _backups(slurm)
    backup = helper.backup(slurm.files["gres"])
    elsewhere = backup.parent / "elsewhere"
    elsewhere.write_text(INSTALLED["gres"], encoding="utf-8")
    for plant in (backup.symlink_to, lambda target: os.link(target, backup)):
        backup.unlink()
        plant(elsewhere)
        refused = _refusal(lambda: _restore(slurm))
        assert "the backups cannot be read" in refused, refused
        assert _texts(slurm) == held
        assert elsewhere.read_text(encoding="utf-8") == INSTALLED["gres"]
    backup.unlink()
    backups["gres"] += "#" * 4096
    backup.write_text(backups["gres"], encoding="utf-8")

    monkeypatch.setattr(blocks, "KEPT_MAX", 4096, raising=False)  # above each file, as it is
    refused = _refusal(lambda: _restore(slurm))
    assert "the backups cannot be read" in refused and "File too large" in refused, refused
    assert (_texts(slurm), _backups(slurm)) == (held, backups)


def test_a_genders_restore_undoes_a_reset_and_is_undone_by_a_second_one(genders):
    """
    The backup either reset leaves goes back with Luna's nodes rendered into our block, and
    the file as it was becomes the backup, so the same route asked again swaps them back.
    """
    genders.real.write_text("login01 all,login\n" + ROLE_BLOCK, encoding="utf-8")
    genders.plugin.render_genders()
    held = genders.real.read_text(encoding="utf-8")
    backup = genders.real.parent / "genders.bkp"
    genders.plugin.init(body={"reset": True, "force": True})
    left = genders.real.read_text(encoding="utf-8")
    assert "login01" not in left and backup.read_text(encoding="utf-8") == held

    for file, kept in ((held, left), (left, held)):
        said = genders.plugin.restore()
        assert said == {"file": str(genders.file), "backup": str(backup), "rendered": True}
        assert genders.file.is_symlink()
        assert genders.real.read_text(encoding="utf-8") == file
        assert backup.read_text(encoding="utf-8") == kept


def test_a_genders_restore_gives_a_backup_without_a_block_one(genders):
    """
    A backup holding a site's own file with no block of ours comes back with the block at
    its end and Luna's nodes in it: restored to the letter, the render would refuse the file.
    """
    genders.plugin.init(body={})
    backup = genders.real.parent / "genders.bkp"
    backup.write_text("login01 all,login\n", encoding="utf-8")
    assert genders.plugin.restore()["rendered"] is True
    wrote = genders.real.read_text(encoding="utf-8")
    assert _emptied(wrote) == "login01 all,login\n" + ROLE_BLOCK, wrote
    assert "node001 all,compute" in wrote


def test_a_genders_restore_is_refused_without_a_backup_of_its_own(genders):
    """
    No backup, or one that is a link, is refused and the file does not move.
    """
    genders.plugin.init(body={})
    held = genders.real.read_text(encoding="utf-8")
    backup = genders.real.parent / "genders.bkp"
    assert "cannot read" in _refusal(genders.plugin.restore)
    elsewhere = genders.real.parent / "elsewhere"
    elsewhere.write_text("login01 all,login\n", encoding="utf-8")
    backup.symlink_to(elsewhere)
    assert "cannot read" in _refusal(genders.plugin.restore)
    assert genders.real.read_text(encoding="utf-8") == held
    assert elsewhere.read_text(encoding="utf-8") == "login01 all,login\n"


def test_a_genders_restore_that_cannot_keep_the_file_puts_it_back(genders, monkeypatch):
    """
    Where the file as it was cannot be kept, it is put back and the backup stays, so nothing
    is lost to a restore that failed.
    """
    from trixd.plugins.genders import backend

    genders.real.write_text("login01 all,login\n" + ROLE_BLOCK, encoding="utf-8")
    genders.plugin.init(body={"reset": True, "force": True})
    held = genders.real.read_text(encoding="utf-8")
    backup = genders.real.parent / "genders.bkp"
    kept = backup.read_text(encoding="utf-8")

    def write(path, content, mode=0o644):
        """files.write, failing as a full disk would."""
        raise BackendFailure(f"cannot write {path}: No space left on device")

    monkeypatch.setattr(backend.files, "write", write)
    refused = _refusal(genders.plugin.restore)
    assert "the file is as it was" in refused, refused
    assert genders.real.read_text(encoding="utf-8") == held
    assert backup.read_text(encoding="utf-8") == kept


def _silent():
    """
    Luna as it is while it is away: every read of its nodes fails.
    """
    raise BackendFailure("Luna is not answering")


@pytest.mark.parametrize("asked", ("reset", "forced reset", "forced restore"))
def test_a_render_that_cannot_run_leaves_slurm_the_nodes_it_had(slurm, monkeypatch, asked):
    """
    Luna's block is carried over until the render writes it afresh, so a reset or a forced
    restore made while Luna is away answers `rendered` false with the nodes still in the
    files: emptied, a slurmctld started then would have none.
    """
    _init(slurm)
    _with_a_line_of_its_own(slurm)
    monkeypatch.setattr(slurm.backend, "luna", lambda: SimpleNamespace(nodes=_silent))
    said = {"reset": lambda: _init(slurm, reset=True),
            "forced reset": lambda: _init(slurm, reset=True, force=True),
            "forced restore": lambda: _restore(slurm)}[asked]()
    assert said["rendered"] is False, said
    wrote = _texts(slurm)
    assert (blocks.block(wrote["nodes"], "TrinityX") or "").splitlines() == [
        "", "NodeName=node001 # GroupName=compute", "NodeName=node002 # GroupName=compute",
        "", "# Nodesets", "NodeSet=compute Nodes=node[001-002]"], wrote["nodes"]
    assert "PartitionName=compute" in (blocks.block(wrote["partitions"], "TrinityX") or "")
    assert SITE_LINE not in wrote["nodes"] or asked == "reset"


def test_a_broken_block_of_lunas_does_not_stop_a_forced_restore(slurm):
    """
    slurmlint judges what a forced restore brings back, not Luna's block, which the render
    rewrites: a block slurm cannot load is no reason to refuse the way out of it.
    """
    pytest.importorskip("slurmlint")
    _init(slurm)
    nodes = slurm.files["nodes"]
    nodes.write_text(blocks.replace(nodes.read_text(encoding="utf-8"), "TrinityX",
                                    "\nNodeName=node001 RealMemory=lots"), encoding="utf-8")
    refused = _refusal(lambda: _restore(slurm))
    assert not refused, refused
    assert "RealMemory=lots" not in _texts(slurm)["nodes"]


def _a_node_file(above: str = "", below: str = "") -> str:
    """
    A node file with Luna's two nodes in our block, and lines of a site's own around it.
    """
    return blocks.replace(above + ROLE_PRESETS + ROLE_BLOCK, "TrinityX",
                          "\nNodeName=node001 # GroupName=compute\n"
                          "NodeName=node002 # GroupName=compute") + below


@pytest.mark.parametrize("held, wrote, judged", (
    ("NodeName=DEFAULT RealMemory=4000 State=UNKNOWN\n",
     "NodeName=DEFAULT RealMemory=100 State=UNKNOWN\n", False),
    ("NodeName=DEFAULT RealMemory=100 State=UNKNOWN\n",
     "nodename=DEFAULT state=unknown RealMemory=100\n", False),
    ("PartitionName=defq Nodes=ALL State=UP\n",
     "PartitionName=defq Nodes=ALL State=DOWN\n", False),
    ("NodeName=DEFAULT RealMemory=100 State=UNKNOWN\n",
     "NodeName=DEFAULT RealMemory=100 State=DOWN\n", True),
    ("NodeName=DEFAULT RealMemory=100 \\\n  State=UNKNOWN\n",
     "NodeName=DEFAULT RealMemory=100 \\\n  State=DOWN\n", True),
    ("NodeName=DEFAULT State=UNKNOWN\nNodeName=login01 State=DOWN\n",
     "NodeName=login01 State=UNKNOWN\nNodeName=DEFAULT State=DOWN\n", True),
    ("NodeName=DEFAULT State=UNKNOWN\n", "DownNodes=node001 State=DOWN\n", True),
), ids=("another setting", "case and order in a line", "a partition's", "the state",
        "on a continued line", "moved between entries", "down nodes"))
def test_only_a_node_state_that_differs_outside_the_blocks_is_judged(slurm, held, wrote,
                                                                     judged):
    """
    Outside the blocks it is the State each node entry sets that is compared, not its line:
    the installer's DEFAULT with another RealMemory judges nobody, a partition's State is
    not a node's, and a State that differs on any node entry judges every node.
    """
    states = slurm.backend._states(_a_node_file(held), _a_node_file(wrote), "TrinityX")
    assert states == (["node001", "node002"] if judged else []), states


def test_a_default_state_moved_past_our_block_is_judged(slurm):
    """
    A DEFAULT reaches the lines after it, so the same line above our block and below it
    are not the same State for Luna's nodes.
    """
    line = "NodeName=DEFAULT State=DOWN\n"
    states = slurm.backend._states(_a_node_file(above=line), _a_node_file(below=line),
                                   "TrinityX")
    assert states == ["node001", "node002"], states


@pytest.mark.parametrize("force", (False, True))
def test_a_reset_that_changes_a_state_needs_the_hardware_right(slurm, monkeypatch, force):
    """
    A reset takes a preset's State away, which a save may only do with the hardware right
    on the node: a person holding w on the cluster alone is refused and nothing is written,
    and rootus resets as before.
    """
    held = _with_a_state_to_lose(slurm)
    backups = _backups(slurm)
    who = _person(slurm, monkeypatch, {"node": {"node001": "node001: no hardware right"}})
    refused = _refusal(lambda: slurm.plugin.init(body={"reset": True, "force": force}, **who))
    assert "no hardware right" in refused, refused
    assert (_texts(slurm), _backups(slurm)) == (held, backups)

    assert _init(slurm, reset=True, force=force)["rendered"] is True
    assert _backups(slurm) == held


def test_an_init_alone_asks_luna_about_no_node(slurm, monkeypatch):
    """
    An init without `reset` gives blocks and changes no State, so a person is asked about
    the cluster and nothing else, as the installer's call is.
    """
    asked = []
    who = _person(slurm, monkeypatch, {})
    monkeypatch.setattr(slurm.backend.persons, "may",
                        lambda person, table, names, bit: asked.append((table, bit)) or {})
    assert slurm.plugin.init(body={}, **who)["rendered"] is True
    assert asked == [("cluster", "w")], asked


def test_a_reset_of_files_that_moved_while_luna_was_asked_is_refused(slurm, monkeypatch):
    """
    Luna is asked before the lock is taken, about the State changes the files then made.
    Files that make others once the lock is held are a conflict, and nothing is written.
    """
    _with_a_state_to_lose(slurm)
    who = _person(slurm, monkeypatch, {})
    nodes = slurm.files["nodes"]

    def may(person, table, names, bit):
        """persons.may, with a save landing a second State while Luna answers."""
        if table == "node":
            nodes.write_text(nodes.read_text(encoding="utf-8").replace(
                "# NodeName=node001 State=DOWN\n",
                "# NodeName=node001 State=DOWN\n# NodeName=node002 State=DOWN\n"),
                encoding="utf-8")
        return {}

    monkeypatch.setattr(slurm.backend.persons, "may", may)
    backups = _backups(slurm)
    refused = _refusal(lambda: slurm.plugin.init(body={"reset": True}, **who))
    assert "changed while Luna was asked" in refused, refused
    assert "# NodeName=node002 State=DOWN" in _texts(slurm)["nodes"]
    assert _backups(slurm) == backups


def test_a_forced_restore_is_refused_while_a_cut_save_is_not_put_back(slurm):
    """
    A save cut short leaves its mark, and the files are then not ones to swap: the forced
    restore writes nothing until a save or an unforced restore has put them back.
    """
    _init(slurm)
    held, backups = _texts(slurm), _backups(slurm)
    (slurm.share / "pending").mkdir()
    (slurm.share / "pending" / "paths.json").write_text("[]", encoding="utf-8")
    refused = _refusal(lambda: _restore(slurm))
    assert "a save was cut short" in refused, refused
    assert (_texts(slurm), _backups(slurm)) == (held, backups)


def test_a_forced_preview_is_of_a_restore_only(slurm):
    """
    A save is never forced, so a preview asking for one is a bad request.
    """
    _init(slurm)
    with pytest.raises(BadRequest, match="force: goes with mode restore"):
        slurm.plugin.preview(body={**_body(slurm), "mode": "save", "texts": _texts(slurm)},
                             caller=ROOTUS, request=None)


def test_the_daemons_own_writes_are_in_the_audit(slurm, monkeypatch):
    """
    A reset and a forced restore are recorded under whoever asked, with what was asked: a
    forced restore names itself and the nodes whose State it changed, and one refused is
    recorded as refused.
    """
    audited = []
    for outcome in ("granted", "refused"):
        monkeypatch.setattr(slurm.backend.audit, outcome, lambda who, action, what="",
                            detail="", outcome=outcome: audited.append(
                                (who, action, what, outcome)))
    _with_a_state_to_lose(slurm)
    assert _refusal(lambda: _restore(slurm))
    _restore(slurm, judged=["node001"])
    _init(slurm, reset=True, force=True)
    assert audited == [("trixd", "slurm init", "cluster", "granted"),
                       ("trixd", "slurm init", "cluster, reset", "granted"),
                       ("trixd", "slurm restore", "forced, cluster", "refused"),
                       ("trixd", "slurm restore", "forced, cluster; State: node001", "granted"),
                       ("trixd", "slurm init", "cluster, reset, forced", "granted")], audited


def _backup_gaps(slurm) -> list:
    """
    The sentences about a backup among the gaps the files route answers.
    """
    return [why for why in slurm.plugin.files()["why"] if ".bkp" in why]


def _save(slurm) -> dict:
    """
    A save of the three files' own texts, as rootus asks it: the backups are what it writes.
    """
    return slurm.plugin.save(body={**_body(slurm, force=False), "texts": _texts(slurm)},
                             caller=ROOTUS, request=None)


@pytest.mark.parametrize("mode", (0o644, 0o640, 0o600))
def test_a_save_writes_its_backup_whatever_the_backups_mode(slurm, monkeypatch, mode):
    """
    A backup without group write does not stop a save: asking whether a save can run and a
    preview say nothing of it and change nothing, the save gives the backup group read and
    write and puts what the file held in it, and a second save finds nothing left to change.
    """
    _init(slurm)
    backup = helper.backup(slurm.files["nodes"])
    backup.write_text("# an older set\n", encoding="utf-8")
    os.chmod(backup, mode)
    before = (os.stat(backup), backup.read_bytes())
    assert _backup_gaps(slurm) == []
    previewed = slurm.plugin.preview(body={**_body(slurm, force=False), "mode": "save",
                                           "texts": _texts(slurm)}, caller=ROOTUS, request=None)
    assert previewed == {"states": [], "refused": ""}
    assert (os.stat(backup), backup.read_bytes()) == before
    held = _texts(slurm)
    _save(slurm)
    assert _backups(slurm) == held and _texts(slurm) == held
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o664
    changed = []
    monkeypatch.setattr(os, "fchmod", lambda fd, to: changed.append(to))
    _save(slurm)
    assert changed == []


def test_a_save_makes_the_backup_that_is_missing(slurm):
    """
    A file with no backup beside it stops no save: the save makes one, the helper's to write,
    holding what the file held.
    """
    _init(slurm)
    backup = helper.backup(slurm.files["gres"])
    backup.unlink()
    assert _backup_gaps(slurm) == []
    assert not backup.exists()
    held = _texts(slurm)
    _save(slurm)
    assert _backups(slurm) == held
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o664


def _read_by_another(folder, name: str) -> bool:
    """
    Whether an account outside the files' group opens the file to read, tried for real in a
    child that gives up root, its groups and its uid; None where the suite is not root.
    """
    if os.geteuid() != 0:
        return None
    where = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
            os.close(os.open(name, os.O_RDONLY, dir_fd=where))
            os._exit(0)
        except OSError:
            os._exit(1)
    os.close(where)
    return os.waitstatus_to_exitcode(os.waitpid(child, 0)[1]) == 0


@pytest.mark.parametrize("verb", ("save", "restore"))
@pytest.mark.parametrize("file, backup, left", (
    (0o664, 0o600, 0o664), (0o664, 0o660, 0o664), (0o664, None, 0o664), (0o664, 0o666, 0o664),
    (0o664, 0o775, 0o664), (0o660, 0o664, 0o660), (0o660, 0o600, 0o660), (0o660, None, 0o660),
    (0o775, 0o600, 0o664), (0o666, 0o600, 0o664)))
def test_a_backup_is_read_by_whoever_reads_its_file(slurm, verb, file, backup, left):
    """
    A preview runs the helper without the files' group, so it reads a backup only as others
    do. Before a save or a restore a backup gets its file's own read bits with group read
    and write: read by others exactly when the file is, never written or run by them.
    """
    path = slurm.files["nodes"]
    kept = helper.backup(path)
    os.chmod(path, file)
    if backup is None:
        kept.unlink()
    else:
        os.chmod(kept, backup)
    slurm.backend._backups({kind: str(each) for kind, each in slurm.files.items()},
                           os.getgid(), verb)
    if backup is None and verb == "restore":
        assert not kept.exists()
        return
    mode = stat.S_IMODE(os.stat(kept).st_mode)
    assert bool(mode & stat.S_IROTH) is bool(file & stat.S_IROTH)
    assert mode == left
    assert _read_by_another(path.parent, kept.name) in (None, bool(file & stat.S_IROTH))


def test_a_file_that_is_not_the_installers_opens_no_backup(slurm, monkeypatch, tmp_path):
    """
    Slurm's account can put a file of its own, or a link, where one of the three is: only a
    regular file that is root's and of the files' group gives its backup its read bits.
    """
    path, files = slurm.files["nodes"], {k: str(p) for k, p in slurm.files.items()}
    kept = helper.backup(path)
    os.chmod(path, 0o666)
    monkeypatch.setattr(os, "fchown", lambda fd, owner, group: None)  # not root's to do here
    slurm.backend._backups(files, os.getgid() + 1, "restore")
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o660
    (tmp_path / "elsewhere").write_text("another's\n", encoding="utf-8")
    os.chmod(tmp_path / "elsewhere", 0o666)
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    os.chmod(kept, 0o600)
    slurm.backend._backups(files, os.getgid(), "restore")
    assert stat.S_IMODE(os.stat(kept).st_mode) == 0o660


def test_a_restore_reads_backups_of_any_mode_and_makes_none(slurm):
    """
    A restore puts the backups' texts back whatever the backups' mode; one that is missing is
    refused by name and not made, since the file as it is now is no set to go back to.
    """
    _init(slurm)
    for path in slurm.files.values():
        helper.backup(path).write_bytes(path.read_bytes())
        os.chmod(helper.backup(path), 0o644)
    held = _backups(slurm)
    assert _with_a_preset(slurm) != held
    assert _backup_gaps(slurm) == []
    _restore(slurm, force=False)
    assert _texts(slurm) == held and _backups(slurm) == held
    gone = helper.backup(slurm.files["gres"])
    gone.unlink()
    with pytest.raises(ServiceUnavailable, match="the backups cannot be read"):
        _restore(slurm, force=False)
    assert _texts(slurm) == held and not gone.exists()


SECRET: str = "# a line only root reads\n"
# What the daemon writes itself, as root: each reads the three files into a backup.
AS_ROOT: tuple = ("a forced reset", "a forced restore", "a reset", "a save with no backup")


def _swapped(slurm, tmp_path, asked: str, pipe: bool = False):
    """
    The gres file swapped, as slurm's account can in its own directory, for a link to a file
    of another's or for a pipe, with no backup beside it where a save is asked: the path,
    the file linked to, and the call that asks, its body read before the swap.
    """
    _init(slurm)
    save = {**_body(slurm, force=False), "texts": _texts(slurm)}
    restore = _body(slurm)
    ask = {"a save with no backup": lambda: slurm.plugin.save(body=save, caller=ROOTUS,
                                                              request=None),
           "a reset": lambda: _init(slurm, reset=True),
           "a forced reset": lambda: _init(slurm, reset=True, force=True),
           "a forced restore": lambda: slurm.plugin.restore(body=restore, caller=ROOTUS,
                                                            request=None)}[asked]
    path, target = slurm.files["gres"], tmp_path / "elsewhere"
    target.write_text(SECRET, encoding="utf-8")
    os.chmod(target, 0o600)
    path.unlink()
    if pipe:
        os.mkfifo(path)
    else:
        path.symlink_to(target)
    if asked == "a save with no backup":
        helper.backup(path).unlink()
    return path, target, ask


@pytest.mark.parametrize("asked", AS_ROOT)
def test_a_file_that_is_a_link_is_never_read_into_a_backup(slurm, monkeypatch, tmp_path, asked):
    """
    Slurm's account owns the directory and can make one of the three a link to a file only
    root reads. What the daemon reads as root goes into a backup the admin group reads, so
    it reads no link: the change is refused naming the file, no backup holds the other
    file's text or is left behind, that file is as it was and the helper does not run.
    """
    path, target, ask = _swapped(slurm, tmp_path, asked)
    ran = []
    monkeypatch.setattr(helper, "answer", lambda verb, request: ran.append(verb))
    before = (os.stat(target), sorted(os.listdir(path.parent)))
    try:
        refused = ask()
    except TrixdError as error:
        refused = error
    backups = [helper.backup(each) for each in slurm.files.values()]
    assert SECRET not in "".join(each.read_text() for each in backups if each.exists())
    assert isinstance(refused, TrixdError) and str(path) in str(refused), refused
    assert (os.stat(target), sorted(os.listdir(path.parent))) == before
    assert target.read_text(encoding="utf-8") == SECRET and ran == []


@pytest.mark.parametrize("asked", AS_ROOT)
def test_a_file_that_is_a_pipe_is_refused_without_waiting(slurm, tmp_path, asked):
    """
    A pipe in place of one of the three is refused by name at once: opened as a file is, it
    would hold the daemon, and the render behind it, until somebody wrote to it.
    """
    import signal

    path, _target, ask = _swapped(slurm, tmp_path, asked, pipe=True)
    signal.signal(signal.SIGALRM, lambda *_: pytest.fail(f"{asked} waited on the pipe"))
    signal.alarm(5)
    try:
        with pytest.raises(TrixdError, match=str(path)):
            ask()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
    assert not helper.backup(path).exists() or asked != "a save with no backup"


def test_the_render_reads_and_puts_back_no_link(slurm, tmp_path):
    """
    What the render holds to put back after a reconfigure slurm refuses is read from no link
    and written through none: a file swapped for a link in between is named as not put back,
    and the file it leads to is as it was.
    """
    path, target = str(slurm.files["gres"]), tmp_path / "elsewhere"
    target.write_text(SECRET, encoding="utf-8")
    slurm.files["gres"].unlink()
    slurm.files["gres"].symlink_to(target)
    assert slurm.backend._contents([path]) == {path: None}
    assert slurm.backend._restore({path: b"# the block as it was\n"}) == [path]
    assert target.read_text(encoding="utf-8") == SECRET


def test_the_files_behind_a_linked_directory_are_read_and_written(slurm, tmp_path):
    """
    Only a link in place of a file is refused: the directory the files are in is a link to
    the shared tree on a controller, and a reset, a save that makes its backup and a forced
    restore go through it.
    """
    from trixd.utils import declarations

    folder = slurm.files["nodes"].parent
    (tmp_path / "etc-slurm").symlink_to(folder, target_is_directory=True)
    declarations.merge("slurm", "config", "", {
        f"{kind}_file": str(tmp_path / "etc-slurm" / path.name)
        for kind, path in slurm.files.items()}, slurm.backend.DEFAULT_CONFIG)
    _init(slurm)
    held = _with_a_preset(slurm)
    _init(slurm, reset=True)
    assert _backups(slurm) == held
    helper.backup(slurm.files["gres"]).unlink()
    held = _texts(slurm)
    _save(slurm)
    assert _backups(slurm) == held
    _restore(slurm)
    assert _backups(slurm) == held


def _genders_silent(monkeypatch) -> None:
    """
    Luna away for the genders render, which reads its nodes through the slurm plugin.
    """
    monkeypatch.setattr("trixd.plugins.slurm.backend.luna",
                        lambda: SimpleNamespace(nodes=_silent))


@pytest.mark.parametrize("asked", ("reset", "forced reset", "restore"))
def test_a_render_that_cannot_run_leaves_the_genders_file_its_nodes(genders, monkeypatch,
                                                                    asked):
    """
    Our block is carried over until the render writes it afresh, so a reset or a restore
    made while Luna is away leaves pdsh the nodes it had.
    """
    genders.real.write_text("login01 all,login\n" + ROLE_BLOCK, encoding="utf-8")
    genders.plugin.render_genders()
    genders.plugin.init(body={"reset": True})
    _genders_silent(monkeypatch)
    said = {"reset": lambda: genders.plugin.init(body={"reset": True}),
            "forced reset": lambda: genders.plugin.init(body={"reset": True, "force": True}),
            "restore": genders.plugin.restore}[asked]()
    assert said["rendered"] is False, said
    wrote = genders.real.read_text(encoding="utf-8")
    assert (blocks.block(wrote, "TrinityX") or "").splitlines() == [
        "", "node001 all,compute", "node002 all,compute"], wrote
    assert ("login01" in wrote) is (asked != "forced reset")
