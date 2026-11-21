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
The slurm and genders files against the ones the packaged generators produce.

Luna's own config hooks and the OOD slurm app write these files too, through those
generators, from the same node list. Nothing coordinates the three writers, and what makes
that safe is that they all leave the same bytes: a render that writes its own spelling of
a marker, folds a hostlist its own way or moves a blank line turns every pass into two
writers overwriting each other, on files on shared storage that slurm is reconfigured over.

So the comparison here is the whole file, not the block: the markers, the administrator's
own lines outside it and the newline at the end of the file are all part of what the other
writers produce. tests/unit/golden/ holds what they produced, written by
tools/build_goldens.py on a controller that has them; tests/unit/golden_cases.py holds what
this daemon has to produce for the same input.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import difflib
import importlib.util
import os
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import hostlist
import pytest

from trixd.common.errors import BackendFailure
from trixd.plugins.slurm.hostnames import fold
from trixd.utils import files as daemon_files

HERE = Path(__file__).resolve().parent


def _by_path(name: str, path: Path):
    """
    A module beside this one, loaded by its path rather than by package.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TABLE = _by_path("golden_cases", HERE / "golden_cases.py")
CASES = TABLE.CASES
PAIRS = [(case, kind) for case in CASES for kind in TABLE.KINDS]
IDS = [f"{case['case']}-{kind}" for case, kind in PAIRS]


def _why(case: dict, kind: str, want: str, got: str) -> str:
    """
    The first lines that differ, rather than two files to read side by side.
    """
    diff = difflib.unified_diff(want.splitlines(True), got.splitlines(True),
                                "what this daemon has to write", "what the generators wrote")
    return f"{case['case']}/{TABLE.KINDS[kind]}: {case['why']}\n" + "".join(diff)


def test_every_case_has_a_file_the_generators_produced():
    """
    A case added to the table without the goldens being rebuilt would otherwise compare
    nothing and pass.
    """
    missing = [os.path.relpath(TABLE.golden(case, kind), HERE)
               for case, kind in PAIRS if not os.path.isfile(TABLE.golden(case, kind))]
    assert not missing, f"{missing}: run tools/build_goldens.py where the generators are"


@pytest.mark.parametrize("case,kind", PAIRS, ids=IDS)
def test_the_whole_file_is_what_the_generators_leave_behind(case, kind):
    """
    Byte for byte, the whole file: a block in the generators' own markers, the
    administrator's lines around it untouched, and one newline at the end.
    """
    with open(TABLE.golden(case, kind), encoding="utf-8") as handle:
        wrote = handle.read()
    ours = TABLE.whole(case, kind)
    assert ours == wrote, _why(case, kind, ours, wrote)


@pytest.mark.parametrize("case,kind", PAIRS, ids=IDS)
def test_no_file_carries_the_daemons_own_markers(case, kind):
    """
    trixd/utils/files.py writes a managed block of its own, in markers these generators do
    not know. A render has to land in the block the other writers find, so the pair in the
    table is theirs and the daemon's own pair appears in none of these files.
    """
    with open(TABLE.golden(case, kind), encoding="utf-8") as handle:
        wrote = handle.read()
    own = daemon_files.BLOCK_START.format(manager=TABLE.MANAGER)
    theirs = TABLE.BLOCK_START.format(manager=TABLE.MANAGER)
    assert own not in wrote, f"{case['case']}/{TABLE.KINDS[kind]} carries {own!r}"
    if case.get("managed", True):
        assert theirs in wrote, f"{case['case']}/{TABLE.KINDS[kind]} has no {theirs!r}"


@pytest.mark.parametrize("case", [c for c in CASES if not c.get("managed", True)],
                         ids=lambda case: case["case"])
def test_a_file_without_our_block_is_left_exactly_as_it_was(case):
    """
    The administrator's override: take the block out and the generators write nothing at
    all. They do not put one back, and neither may a render.
    """
    for kind in TABLE.KINDS:
        with open(TABLE.golden(case, kind), encoding="utf-8") as handle:
            wrote = handle.read()
        assert wrote == TABLE.before(case, kind), _why(case, kind, TABLE.before(case, kind),
                                                       wrote)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_a_set_of_names_folds_as_the_generators_fold_it(case):
    """
    The nodesets in the file, against the fold this daemon has. The two libraries disagree
    where a name carries more than one number, and a case that says so is held to the
    disagreement: both expand to the same nodes, and the bytes are the generators'.
    """
    with open(TABLE.golden(case, "nodes"), encoding="utf-8") as handle:
        wrote = [line for line in handle.read().splitlines() if line.startswith("NodeSet=")]
    groups: dict = {}
    for one in case["nodes"]:
        groups.setdefault(one["group"], []).append(one["name"])
    if not case.get("managed", True):
        assert not wrote, f"{case['case']}: a file with no block of ours carries nodesets"
        return
    assert len(wrote) == len(groups), f"{case['case']}: {wrote}"
    for line, (group, names) in zip(wrote, groups.items()):
        theirs = line.split("Nodes=", 1)[1]
        assert sorted(hostlist.expand_hostlist(theirs)) == sorted(names), \
            f"{case['case']}: {line}"
        if group in case.get("folds", {}):
            assert fold(names) != theirs, (f"{case['case']}: {group} is declared as a fold "
                                           f"the daemon's library does not reproduce, and "
                                           f"it now does: take the declaration out")
            assert sorted(hostlist.expand_hostlist(fold(names))) == sorted(names), \
                f"{case['case']}: {line}"
        else:
            assert fold(names) == theirs, (f"{case['case']}: the daemon folds {names} as "
                                           f"{fold(names)!r} and the generators as "
                                           f"{theirs!r}")


def _slurm(monkeypatch, tmp_path, case) -> dict:
    """
    Luna holding the case's nodes, and a stand-in generator that records the set it was
    handed and writes what the real one wrote for that case.
    """
    from trixd.plugins.slurm import backend as slurm
    from trixd.utils import declarations

    seen: dict = {}
    kinds = {kind: str(tmp_path / TABLE.KINDS[kind])
             for kind in ("nodes", "partitions", "gres")}
    declarations.write("slurm", "config", "", {**slurm.DEFAULT_CONFIG,
                                               "nodes_file": kinds["nodes"],
                                               "partitions_file": kinds["partitions"],
                                               "gres_file": kinds["gres"]})

    def all_configs(nodes, configs, manager):
        """
        What trinityx_config_slurm does: write each file and say it worked.
        """
        seen.update(nodes=nodes, configs=configs, manager=manager)
        for kind, path in configs.items():
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(TABLE.whole(case, kind))
        return True

    package = ModuleType("trinityx_config_slurm")
    package.Generate = lambda: SimpleNamespace(all_configs=all_configs)
    monkeypatch.setitem(sys.modules, "trinityx_config_slurm", package)
    monkeypatch.setattr(slurm, "luna",
                        lambda: SimpleNamespace(nodes=lambda: case["luna"]))
    monkeypatch.setattr(slurm.shell, "run",
                        lambda argv, **kwargs: SimpleNamespace(code=0, out="", err="",
                                                               ok=True))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.141.0.1", 0))])
    return seen


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_the_slurm_render_hands_the_generator_the_case(rig, monkeypatch, tmp_path, case):
    """
    The node list the render hands over is the one the goldens were built from, so what
    the generators wrote there is what this input produces. A node Luna holds without a
    group never reaches them.
    """
    from trixd.plugins.slurm.backend import Backend

    seen = _slurm(monkeypatch, tmp_path, case)
    answer = Backend().render_config()

    assert seen.get("nodes") == case["nodes"], f"{case['case']}: {seen.get('nodes')}"
    assert seen.get("manager") == TABLE.MANAGER, seen.get("manager")
    assert answer.get("skipped", []) == case.get("skipped", []), answer
    for kind in ("nodes", "partitions", "gres"):
        with open(seen["configs"][kind], encoding="utf-8") as handle:
            assert handle.read() == TABLE.whole(case, kind), f"{case['case']}: {kind}"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_the_genders_render_hands_the_generator_the_case(rig, monkeypatch, case):
    """
    The same node list reaches the genders generator, and a file the administrator took
    the block out of is a named failure rather than a block put back.
    """
    from trixd.plugins.genders.backend import Backend

    handed: list = []
    managed = case.get("managed", True)
    package = ModuleType("trinityx_config_genders")
    package.Generate = lambda: SimpleNamespace(
        Genders=lambda given: (handed.extend(given), managed)[1])
    monkeypatch.setitem(sys.modules, "trinityx_config_genders", package)
    monkeypatch.setattr("trixd.plugins.slurm.backend.luna",
                        lambda: SimpleNamespace(nodes=lambda: case["luna"]))

    if not managed:
        with pytest.raises(BackendFailure, match="no TrinityX managed block"):
            Backend().render_genders()
    else:
        answer = Backend().render_genders()
        assert answer.get("skipped", []) == case.get("skipped", []), answer
    assert handed == case["nodes"], f"{case['case']}: {handed}"
