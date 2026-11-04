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
What reaches slurm's files, and what comes back off them when slurm refuses it.

slurm answers a reconfigure two ways. It refuses what it cannot parse, loudly, and every
client refuses it too; and it takes a directive whose name is empty in silence, with exit
code zero and nothing on either stream. So the files are gated on the way out and put back
when the refusal comes.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import socket
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

from tests.unit.test_plugins import Ran
from tests.unit.test_slurm_changes import _Said
from trixd.common import logging as trixlog
from trixd.common.errors import BackendFailure

BLOCKS: tuple = ("slurm-nodes.conf", "slurm-partitions.conf", "slurm-gres.conf")

# What a controller running slurm 25.11.5 said when an included file would not parse.
REFUSAL: str = (
    "scontrol: error: _parse_next_key: Parsing error at unrecognized key: "
    "ThisIsNotASlurmOption\n"
    'scontrol: error: "Include" failed in file /etc/slurm/slurm.conf line 10\n'
    "scontrol: fatal: Unable to process configuration file")


def _slurm(monkeypatch, nodes: dict, tmp_path, codes: tuple = (0,),
           content: str = "one", dark: set = frozenset()) -> dict:
    """
    The slurm plugin on a stand-in generator package, Luna, DNS and scontrol.

    `codes` is what each successive reconfigure answers, the last one standing for the rest,
    so a test can let the written blocks be refused and still say what the restored ones
    meet. The stand-in writes the files it is handed, as the real generator does: one that
    wrote nothing would leave them unchanged for ever and pin the opposite of a controller.
    `content` None writes the generator's `NodeName=` lines. Every name resolves but those
    in `dark`, which the test may change between passes.
    """
    from trixd.plugins.slurm import backend as slurm
    from trixd.utils import declarations

    seen = {"ran": [], "looked": [], "dark_at_reconfigure": []}
    declarations.write("slurm", "config", "", {
        **slurm.DEFAULT_CONFIG,
        "nodes_file": str(tmp_path / BLOCKS[0]),
        "partitions_file": str(tmp_path / BLOCKS[1]),
        "gres_file": str(tmp_path / BLOCKS[2])})

    class Generate:
        """
        trinityx_config_slurm's entry point, recording what it was given.
        """

        def all_configs(self, nodes, configs, manager):
            """
            Remember the call and write what it was asked to, as the real one does.
            """
            seen.update(nodes=nodes, configs=configs, manager=manager)
            text = content if content is not None else "".join(
                f"NodeName={one['name']} # GroupName={one['group']}\n" for one in nodes)
            for path in configs.values():
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(text)
            return True

    def resolve(host, *args, **kwargs):
        """
        The resolver, refusing the names in `dark` as getaddrinfo does.
        """
        seen["looked"].append(host)
        if host in dark:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.141.0.1", 0))]

    def reconfigure(argv, **kwargs):
        """
        scontrol, answering this pass's code and slurm's own words with it.
        """
        seen["ran"].append(argv)
        seen["dark_at_reconfigure"].append(sorted(dark))
        code = codes[min(len(seen["ran"]), len(codes)) - 1]
        return Ran("", code, REFUSAL if code else "")

    package = ModuleType("trinityx_config_slurm")
    package.Generate = Generate
    monkeypatch.setitem(sys.modules, "trinityx_config_slurm", package)
    monkeypatch.setattr(slurm, "luna", lambda: SimpleNamespace(nodes=lambda: nodes))
    monkeypatch.setattr(slurm.shell, "run", reconfigure)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return seen


def _held(tmp_path) -> dict:
    """
    What each managed block holds, with None for one that is not on the disk at all.
    """
    held = {}
    for name in BLOCKS:
        path = tmp_path / name
        held[name] = path.read_bytes() if path.exists() else None
    return held


def test_a_refused_reconfigure_puts_the_previous_slurm_blocks_back(rig, monkeypatch,
                                                                   tmp_path):
    """
    A refused reconfigure left the bytes slurm would not parse on the disk. slurmctld keeps
    the configuration it had, but every client parses these files for itself, so sinfo and
    sbatch fail until they are gone - and they sit on shared storage, where a cold start on
    either controller meets them next.
    """
    from trixd.plugins.slurm.backend import Backend

    _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path)
    assert Backend().render_config()["reconfigured"] is True
    kept = _held(tmp_path)
    assert kept == {name: b"one" for name in BLOCKS}, kept

    seen = _slurm(monkeypatch, {"node001": {"group": "gpu"}}, tmp_path, codes=(1, 0),
                  content="two")
    # The disk is read before the failure is. A build without the rollback does not raise at
    # all, and a pytest.raises around the call would then end the test on its own premise
    # instead of on the bytes, which are there to read whichever build this is.
    refused = ""
    try:
        Backend().render_config()
    except BackendFailure as error:
        refused = str(error)

    assert _held(tmp_path) == kept, "the blocks slurm refused were left on the disk"
    assert len(seen["ran"]) == 2, f"the restored blocks were never reconfigured in: {seen}"
    assert "refused the slurm blocks" in refused, f"the render did not fail: {refused!r}"
    assert "ThisIsNotASlurmOption" in refused, f"slurm's own reason is missing: {refused!r}"


def test_a_block_that_was_not_there_before_is_taken_away_again(rig, monkeypatch, tmp_path):
    """
    An empty file is not the same as no file: slurm includes it either way, so a rollback
    that wrote nought bytes over a gres file a cluster never had would leave the include it
    was there to remove.
    """
    from trixd.plugins.slurm.backend import Backend

    _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path)
    Backend().render_config()
    (tmp_path / BLOCKS[2]).unlink()

    _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path, codes=(1, 0),
           content="two")
    with pytest.raises(BackendFailure):
        Backend().render_config()

    held = _held(tmp_path)
    assert held == {BLOCKS[0]: b"one", BLOCKS[1]: b"one", BLOCKS[2]: None}, \
        f"the gres block came back as {held[BLOCKS[2]]!r}"


def test_a_render_slurm_accepts_still_reconfigures(rig, monkeypatch, tmp_path):
    """
    The rollback is for a refusal, and a render slurm takes is untouched by it.
    """
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path)
    answer = Backend().render_config()

    assert seen["ran"] == [["/usr/bin/scontrol", "reconfigure"]], seen["ran"]
    assert answer["reconfigured"] is True and answer["changed"] is True, answer
    assert _held(tmp_path) == {name: b"one" for name in BLOCKS}


def test_an_unchanged_render_does_not_reconfigure_slurm(rig, monkeypatch, tmp_path):
    """
    The comparison that keeps a boot storm from reconfiguring slurm once per node event now
    reads the previous contents rather than their digests, and answers the same question.
    """
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path)
    assert Backend().render_config()["changed"] is True

    seen["ran"].clear()
    again = Backend().render_config()
    assert seen["ran"] == [], "slurm was reconfigured for blocks that had not changed"
    assert again["changed"] is False and again["reconfigured"] is False, again
    assert _held(tmp_path) == {name: b"one" for name in BLOCKS}


def test_a_restore_slurm_refuses_as_well_reads_differently(rig, monkeypatch, tmp_path):
    """
    Both sets refused is a worse state than a rollback: slurm and its clients are left on a
    configuration that does not load, and the operator has to be told that instead of being
    told the previous blocks are back.
    """
    from trixd.plugins.slurm.backend import Backend

    _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path)
    Backend().render_config()

    seen = _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path, codes=(1, 1),
                  content="two")
    with pytest.raises(BackendFailure) as raised:
        Backend().render_config()

    said = str(raised.value)
    assert len(seen["ran"]) == 2, f"the restored blocks were never reconfigured in: {seen}"
    assert "previous blocks as well" in said, said
    assert "back in place" not in said, f"this still reads as an ordinary rollback: {said}"
    assert _held(tmp_path) == {name: b"one" for name in BLOCKS}


def test_an_entry_slurm_cannot_load_is_left_out(rig, monkeypatch, tmp_path):
    """
    `PartitionName= Nodes=` is accepted with exit code zero and both streams empty, and
    makes a live partition named after the next token on the line, so the reconfigure
    cannot report this class at all. Whitespace inside a name is refused instead, loudly,
    which a rollback would handle - but not emitting it is cheaper than undoing it.
    """
    from trixd.plugins.slurm.backend import Backend

    nodes = {"node001": {"group": "compute"},
             "node002": {"group": ""},
             "node003": {"group": "   "},
             "node004": {"group": "two words"},
             "node 005": {"group": "compute"},
             "": {"group": "compute"},
             "node006": {"group": "gpu"}}
    seen = _slurm(monkeypatch, nodes, tmp_path)
    answer = Backend().render_config()

    assert seen["nodes"] == [{"name": "node001", "group": "compute"},
                             {"name": "node006", "group": "gpu"}], seen["nodes"]
    assert answer["skipped"] == sorted(["", "node 005", "node002", "node003", "node004"]), \
        answer["skipped"]
    assert answer["nodes"] == 2 and answer["reconfigured"] is True, answer
    assert _held(tmp_path) == {name: b"one" for name in BLOCKS}


def _render() -> str:
    """
    One render pass; a put-off or a failure is a BackendFailure, kept as its message.
    """
    from trixd.plugins.slurm.backend import Backend

    try:
        Backend().render_config()
    except BackendFailure as error:
        return str(error)
    return ""


def _file_names(tmp_path) -> str:
    """
    The node block as it stands, for a test to read the names in it.
    """
    path = tmp_path / BLOCKS[0]
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_a_rename_to_a_name_that_does_not_resolve_does_not_reconfigure(rig, monkeypatch,
                                                                        tmp_path):
    """
    slurmctld drops a node whose name it cannot resolve, and Luna puts a new name into DNS
    after trixd has already rendered: the files keep the old name until the new one resolves.
    """
    nodes, dark = {"node001": {"group": "compute"}}, set()
    seen = _slurm(monkeypatch, nodes, tmp_path, content=None, dark=dark)
    _render()
    seen["ran"].clear()

    nodes.clear()
    nodes["node002"] = {"group": "compute"}
    dark.add("node002")
    said = _render()

    assert seen["ran"] == [], f"slurm was reconfigured before node002 resolved: {said!r}"
    assert "NodeName=node001 " in _file_names(tmp_path), _file_names(tmp_path)
    assert "node002" in said and "resolve" in said, said


def test_every_reconfigure_runs_while_every_name_resolves(rig, monkeypatch, tmp_path):
    """
    The ticket's order: the rename renders before DNS has the name, and the pass after the
    reload finds the files unchanged, so the one reconfigure has to be the late one.
    """
    nodes, dark = {"node001": {"group": "compute"}}, set()
    seen = _slurm(monkeypatch, nodes, tmp_path, content=None, dark=dark)
    _render()

    nodes.clear()
    nodes["node002"] = {"group": "compute"}
    dark.add("node002")
    _render()
    dark.clear()
    _render()
    _render()

    assert all("node002" not in held for held in seen["dark_at_reconfigure"]), \
        f"slurm was reconfigured while node002 did not resolve: {seen['dark_at_reconfigure']}"
    assert len(seen["ran"]) == 2, f"not one reconfigure for the rename: {seen['ran']}"
    assert "NodeName=node002 " in _file_names(tmp_path), _file_names(tmp_path)


def test_a_name_that_never_resolves_is_written_after_the_wait(rig, monkeypatch, tmp_path):
    """
    The wait is bounded: a node that never gets a DNS record holds slurm off for the wait
    and no longer, and the operator is told which one and what to run.
    """
    from trixd.plugins.slurm import backend as slurm

    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    said = _Said()
    trixlog.get("slurm").addHandler(said)
    try:
        seen = _slurm(monkeypatch, {"node009": {"group": "compute"}}, tmp_path,
                      content=None, dark={"node009"})
        _render()
        assert seen["ran"] == [], "slurm was reconfigured before the name had a chance"
        clock[0] += getattr(slurm, "RESOLVE_WAIT", 60.0) + 1
        _render()
    finally:
        trixlog.get("slurm").removeHandler(said)

    assert len(seen["ran"]) == 1, f"the wait held slurm off for ever: {seen['ran']}"
    warned = [text for level, text in said.said if level == "WARNING"]
    assert any("node009" in text and "scontrol reconfigure" in text for text in warned), \
        said.said


def test_many_new_names_wait_for_one_and_reconfigure_once(rig, monkeypatch, tmp_path):
    """
    A burst of creates coalesces into the wait: one name in the dark stops the lookups and
    the render, and once all resolve slurm is reconfigured once for the lot.
    """
    names = [f"node{number:03d}" for number in range(100)]
    nodes, dark = {name: {"group": "compute"} for name in names}, {names[0]}
    seen = _slurm(monkeypatch, nodes, tmp_path, content=None, dark=dark)
    real = socket.getaddrinfo

    def slow(host, *args, **kwargs):
        """
        A resolver that answers the dark name at once and the rest a little later.
        """
        if host not in dark:
            time.sleep(0.05)
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    _render()
    assert seen["ran"] == [], "slurm was reconfigured while node000 did not resolve"
    assert len(seen["looked"]) < len(names), "the lookups went on past the first refusal"

    dark.clear()
    _render()
    _render()
    assert len(seen["ran"]) == 1, f"not one reconfigure for the burst: {seen['ran']}"


def test_a_lookup_that_hangs_puts_the_render_off_within_the_budget(rig, monkeypatch,
                                                                    tmp_path):
    """
    The lookups run under the lock every render shares, so a resolver that does not answer
    costs a pass the budget and no more, and counts as a name that does not resolve yet.
    """
    from trixd.plugins.slurm import backend as slurm

    seen = _slurm(monkeypatch, {"node001": {"group": "compute"}}, tmp_path, content=None)
    answered = threading.Event()

    def hangs(host, *args, **kwargs):
        """
        A resolver whose server is dark, held until the test lets it go.
        """
        seen["looked"].append(host)
        answered.wait(30)
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", hangs)
    budget = getattr(slurm, "RESOLVE_BUDGET", 2.0)
    started = time.monotonic()
    try:
        _render()
        took = time.monotonic() - started
    finally:
        answered.set()

    assert seen["ran"] == [], "slurm was reconfigured while the lookup had not answered"
    assert took < budget + 1.0, f"the pass took {took:.1f} s against a {budget} s budget"


def test_a_change_with_no_new_name_reconfigures_at_once(rig, monkeypatch, tmp_path):
    """
    A pin rather than a fix test, passing on both builds: a group move or a boot storm names
    nobody new, so it costs no lookup and is not held off.
    """
    nodes, dark = {"node001": {"group": "compute"}}, set()
    seen = _slurm(monkeypatch, nodes, tmp_path, content=None, dark=dark)
    _render()
    seen["ran"].clear()
    seen["looked"].clear()

    nodes["node001"] = {"group": "gpu"}
    dark.add("node001")
    _render()

    assert len(seen["ran"]) == 1, f"a group move was held off: {seen['ran']}"
    assert seen["looked"] == [], f"a name already in the file was looked up: {seen['looked']}"
