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
from pathlib import Path
from types import SimpleNamespace

import hostlist
import pytest

from trixd.common.errors import BackendFailure
from trixd.plugins.slurm.hostnames import fold_nodeset
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
    The nodesets in the file, against the fold the render writes them with. It folds on a
    name's first number where python-hostlist folds on its last, so every group of every
    case is held to the generators' own bytes rather than to an equivalent hostlist.
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
        assert fold_nodeset(names) == theirs, \
            (f"{case['case']}: the render folds {names} as {fold_nodeset(names)!r} and "
             f"the generators as {theirs!r}")


@pytest.mark.parametrize("names,folded", TABLE.NODESETS,
                         ids=[folded for _names, folded in TABLE.NODESETS])
def test_the_fold_answers_what_the_generators_answer(names, folded):
    """
    The fold rule itself, past the corners the eight cases' own node names reach: these are
    what the generators' hostlist filter answers, so a set it will not fold has to come back
    as the plain list it comes back as there, rather than as a bracketed term of our own.
    """
    assert fold_nodeset(names) == folded, f"{names} folds as {fold_nodeset(names)!r}"
    assert sorted(hostlist.expand_hostlist(folded)) == sorted(names), \
        f"{folded} is not the nodes it was folded from"


def _golden(case: dict, kind: str) -> str:
    """
    The file the generators left for this case.
    """
    with open(TABLE.golden(case, kind), encoding="utf-8") as handle:
        return handle.read()


def _slurm(monkeypatch, tmp_path, case) -> dict:
    """
    The three files as the administrator left them, Luna holding the case's nodes, and
    scontrol and the resolver answering. Nothing stands in for a generator: the render
    builds these files itself, which is what the comparison below is of.
    """
    from trixd.plugins.slurm import backend as slurm
    from trixd.utils import declarations

    paths = {kind: str(tmp_path / TABLE.KINDS[kind])
             for kind in ("nodes", "partitions", "gres")}
    for kind, path in paths.items():
        with open(path, "w", encoding="utf-8", newline=chr(10)) as handle:
            handle.write(TABLE.before(case, kind))
    declarations.write("slurm", "config", "", {**slurm.DEFAULT_CONFIG,
                                               "nodes_file": paths["nodes"],
                                               "partitions_file": paths["partitions"],
                                               "gres_file": paths["gres"]})
    monkeypatch.setattr(slurm, "luna",
                        lambda: SimpleNamespace(nodes=lambda: case["luna"]))
    monkeypatch.setattr(slurm.shell, "run",
                        lambda argv, **kwargs: SimpleNamespace(code=0, out="", err="",
                                                               ok=True))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.141.0.1", 0))])
    return paths


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_the_slurm_render_writes_what_the_generators_wrote(rig, monkeypatch, tmp_path, case):
    """
    The render over the file as the administrator left it, against the file the generators
    left for the same input, byte for byte. This is what the ticket is held to: the bytes
    come from the plugin's own renderer, not from the table beside it, and a node Luna
    holds without a group reaches neither.
    """
    from trixd.plugins.slurm.backend import Backend

    paths = _slurm(monkeypatch, tmp_path, case)
    if case.get("managed", True):
        answer = Backend().render_config()
        assert answer.get("skipped", []) == case.get("skipped", []), answer
        assert answer.get("changed") is True, answer
    else:
        with pytest.raises(BackendFailure, match="has no TrinityX managed block"):
            Backend().render_config()
    for kind, path in paths.items():
        with open(path, encoding="utf-8") as handle:
            wrote = handle.read()
        assert wrote == _golden(case, kind), _why(case, kind, wrote, _golden(case, kind))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_the_slurm_render_writes_no_second_block(rig, monkeypatch, tmp_path, case):
    """
    A writer that does not find the generators' markers appends a block of its own instead
    of replacing theirs, and every node is then in the file twice. Two passes over one file
    leave one block, with the daemon's own pair of markers nowhere in it.
    """
    from trixd.plugins.slurm.backend import Backend

    if not case.get("managed", True):
        pytest.skip("a file with no block of ours is never written")
    paths = _slurm(monkeypatch, tmp_path, case)
    Backend().render_config()
    Backend().render_config()
    for kind, path in paths.items():
        with open(path, encoding="utf-8") as handle:
            wrote = handle.read()
        assert wrote.count(TABLE.BLOCK_START.format(manager=TABLE.MANAGER)) == 1, \
            f"{case['case']}/{kind}: a second block was added"
        assert daemon_files.BLOCK_START.format(manager=TABLE.MANAGER) not in wrote, \
            f"{case['case']}/{kind}: the daemon's own markers reached a slurm file"
        assert wrote == _golden(case, kind), _why(case, kind, wrote, _golden(case, kind))


def _genders(monkeypatch, tmp_path, case) -> str:
    """
    The genders file as the administrator left it, and Luna holding the case's nodes.
    Nothing stands in for a generator: the render builds the file itself, which is what the
    comparison below is of.
    """
    from trixd.plugins.genders import backend as genders

    path = str(tmp_path / TABLE.KINDS["genders"])
    with open(path, "w", encoding="utf-8", newline=chr(10)) as handle:
        handle.write(TABLE.before(case, "genders"))
    monkeypatch.setitem(genders.WRITES, "genders_file", path)
    monkeypatch.setattr("trixd.plugins.slurm.backend.luna",
                        lambda: SimpleNamespace(nodes=lambda: case["luna"]))
    return path


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_the_genders_render_writes_what_the_generators_wrote(rig, monkeypatch, tmp_path, case):
    """
    The render over the file as the administrator left it, against the file the generator
    left for the same input, byte for byte. This is what the ticket is held to: the bytes
    come from the plugin's own renderer, not from the table beside it, and a node Luna holds
    without a group reaches neither.
    """
    from trixd.plugins.genders.backend import Backend

    path = _genders(monkeypatch, tmp_path, case)
    if case.get("managed", True):
        answer = Backend().render_genders()
        assert answer.get("skipped", []) == case.get("skipped", []), answer
        assert answer["nodes"] == len(case["nodes"]), answer
    else:
        with pytest.raises(BackendFailure, match="has no TrinityX managed block"):
            Backend().render_genders()
    with open(path, encoding="utf-8") as handle:
        wrote = handle.read()
    assert wrote == _golden(case, "genders"), \
        _why(case, "genders", wrote, _golden(case, "genders"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["case"])
def test_the_genders_render_writes_no_second_block(rig, monkeypatch, tmp_path, case):
    """
    A writer that does not find the generator's markers appends a block of its own instead
    of replacing it, and every node is then in the file twice - which libgenders reads as a
    duplicate rather than as a mistake. Two passes over one file leave one block, with the
    daemon's own pair of markers nowhere in it.
    """
    from trixd.plugins.genders.backend import Backend

    if not case.get("managed", True):
        pytest.skip("a file with no block of ours is never written")
    path = _genders(monkeypatch, tmp_path, case)
    Backend().render_genders()
    Backend().render_genders()
    with open(path, encoding="utf-8") as handle:
        wrote = handle.read()
    assert wrote.count(TABLE.BLOCK_START.format(manager=TABLE.MANAGER)) == 1, \
        f"{case['case']}: a second block was added"
    assert daemon_files.BLOCK_START.format(manager=TABLE.MANAGER) not in wrote, \
        f"{case['case']}: the daemon's own markers reached the genders file"
    assert wrote == _golden(case, "genders"), \
        _why(case, "genders", wrote, _golden(case, "genders"))
