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
The slurm and genders files case by case: what Luna holds, the file as the administrator
left it, and the whole file a render has to leave behind.

Luna's own config hooks, the OOD slurm app and these renders all write these files through
the same generators, so whatever this daemon writes has to be what the others write, down
to the byte. tools/build_goldens.py runs the packaged generators over this table on a
controller that has them and writes tests/unit/golden/; test_golden_files.py holds the
table and those files against each other. Nothing here imports a generator, so the
comparison runs where none is installed, which is everywhere the suite runs.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import os

from trixd.plugins.slurm.hostnames import fold

MANAGER: str = "TrinityX"
DEFAULTS: str = "Defaults"
# The markers the generators write, which is what these files carry. They are not the pair
# trixd/utils/files.py writes, and the block has to be the generators' one: the other two
# writers find their block by these lines.
BLOCK_START: str = "#### {manager} Managed block start ####"
BLOCK_END: str = "#### {manager} Managed block end   ####"

# What each kind is called on a controller, which is the name its golden carries here.
KINDS: dict = {"nodes": "slurm-nodes.conf", "partitions": "slurm-partitions.conf",
               "gres": "gres.conf", "genders": "genders"}

GOLDEN: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")

HEAD: list = ["# the administrator's own header, which the render leaves alone"]
TAIL: list = ["# and the administrator's own footer"]
# Inside the block before the render, so a golden proves the block was replaced rather
# than added to.
STALE: str = "NodeName=stale # GroupName=gone"


CASES: tuple = (
    {
        "case": "one-group",
        "why": "the baseline: two nodes of one group, with the administrator's own lines "
               "above and below the block",
        "luna": {"node002": {"group": "compute"}, "node001": {"group": "compute"}},
        "nodes": [{"name": "node001", "group": "compute"},
                  {"name": "node002", "group": "compute"}],
        "defaults": {
            "nodes": ["# NodeName=compute CPUs=8 RealMemory=16000"],
            "partitions": ["# PartitionName=compute Default=YES MaxTime=1-00:00:00"],
            "gres": ["# GroupGres=compute Name=mps Count=100 no_consume=true"],
        },
        "expect": {
            "nodes": ["NodeName=node001 CPUs=8 RealMemory=16000 # GroupName=compute",
                      "NodeName=node002 CPUs=8 RealMemory=16000 # GroupName=compute"],
            "partitions": ["PartitionName=compute Nodes=compute Default=YES "
                           "MaxTime=1-00:00:00"],
            "gres": ["NodeName=node001 Name=mps Count=100 no_consume=true # "
                     "GroupName=compute",
                     "NodeName=node002 Name=mps Count=100 no_consume=true # "
                     "GroupName=compute"],
            "genders": ["node001 all,compute", "node002 all,compute"],
        },
    },
    {
        "case": "two-numeric-fields",
        "why": "names carrying two numbers fold differently in the two libraries, so a "
               "render that folds them its own way writes a file the other writers do not",
        "luna": {"r1n01": {"group": "gpu"}, "r1n02": {"group": "gpu"},
                 "r2n01": {"group": "gpu"}},
        "nodes": [{"name": "r1n01", "group": "gpu"}, {"name": "r1n02", "group": "gpu"},
                  {"name": "r2n01", "group": "gpu"}],
        # python-hostlist, which trixd folds with, answers r1n[01-02],r2n01 here. Both
        # expand to the same three nodes and both load, but the bytes differ, so a render
        # folding with it rewrites the file on every pass against the other writers.
        "folds": {"gpu": "r[1-2]n01,r1n02"},
        "defaults": {
            "nodes": ["# NodeName=gpu CPUs=16 RealMemory=64000"],
            "partitions": ["# PartitionName=gpu MaxTime=INFINITE State=UP"],
            "gres": ["# GroupGres=gpu Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1]"],
        },
        "expect": {
            "nodes": ["NodeName=r1n01 CPUs=16 RealMemory=64000 # GroupName=gpu",
                      "NodeName=r1n02 CPUs=16 RealMemory=64000 # GroupName=gpu",
                      "NodeName=r2n01 CPUs=16 RealMemory=64000 # GroupName=gpu"],
            "partitions": ["PartitionName=gpu Nodes=gpu MaxTime=INFINITE State=UP"],
            "gres": ["NodeName=r1n01 Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1] # "
                     "GroupName=gpu",
                     "NodeName=r1n02 Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1] # "
                     "GroupName=gpu",
                     "NodeName=r2n01 Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1] # "
                     "GroupName=gpu"],
            "genders": ["r1n01 all,gpu", "r1n02 all,gpu", "r2n01 all,gpu"],
        },
    },
    {
        "case": "a-node-named-after-its-group",
        "why": "a node carrying its group's name: the preset key NodeName=compute is then "
               "both the node's own and its group's, and NodeSet=compute stands beside "
               "NodeName=compute in one file",
        "luna": {"compute": {"group": "compute"}, "node001": {"group": "compute"}},
        "nodes": [{"name": "compute", "group": "compute"},
                  {"name": "node001", "group": "compute"}],
        "defaults": {
            "nodes": ["# NodeName=compute CPUs=8 RealMemory=16000"],
            "partitions": ["# PartitionName=compute Default=YES"],
            "gres": ["# GroupGres=compute Name=mps Count=100 no_consume=true"],
        },
        "expect": {
            "nodes": ["NodeName=compute CPUs=8 RealMemory=16000 # GroupName=compute",
                      "NodeName=node001 CPUs=8 RealMemory=16000 # GroupName=compute"],
            "partitions": ["PartitionName=compute Nodes=compute Default=YES"],
            "gres": ["NodeName=compute Name=mps Count=100 no_consume=true # "
                     "GroupName=compute",
                     "NodeName=node001 Name=mps Count=100 no_consume=true # "
                     "GroupName=compute"],
            "genders": ["compute all,compute", "node001 all,compute"],
        },
    },
    {
        "case": "a-node-with-no-group",
        "why": "a node Luna holds before its group is assigned never reaches either "
               "generator, so neither file names it",
        "luna": {"node001": {"group": "compute"}, "node002": {"group": None},
                 "node003": {"group": ""}},
        "nodes": [{"name": "node001", "group": "compute"}],
        "skipped": ["node002", "node003"],
        "defaults": {
            "nodes": ["# NodeName=compute CPUs=8 RealMemory=16000"],
            "partitions": ["# PartitionName=compute Default=YES"],
            "gres": ["# GroupGres=compute Name=mps Count=100 no_consume=true"],
        },
        "expect": {
            "nodes": ["NodeName=node001 CPUs=8 RealMemory=16000 # GroupName=compute"],
            "partitions": ["PartitionName=compute Nodes=compute Default=YES"],
            "gres": ["NodeName=node001 Name=mps Count=100 no_consume=true # "
                     "GroupName=compute"],
            "genders": ["node001 all,compute"],
        },
    },
    {
        "case": "three-kinds-of-group-default",
        "why": "a group whose preset is a PartitionName line, one whose preset is a "
               "GroupName line, and one with no preset at all: the three branches that "
               "decide what a node's line carries",
        "luna": {"g1n01": {"group": "parts"}, "g2n01": {"group": "named"},
                 "g3n01": {"group": "bare"}},
        "nodes": [{"name": "g1n01", "group": "parts"}, {"name": "g2n01", "group": "named"},
                  {"name": "g3n01", "group": "bare"}],
        "defaults": {
            "nodes": ["# PartitionName=parts CPUs=8 RealMemory=16000",
                      "# GroupName=named CPUs=4 RealMemory=8000"],
            "partitions": ["# PartitionName=parts Default=YES MaxTime=1-00:00:00"],
            "gres": ["# GroupGres=named Name=mps Count=100 no_consume=true"],
        },
        "expect": {
            "nodes": ["NodeName=g1n01 CPUs=8 RealMemory=16000 # GroupName=parts",
                      "NodeName=g2n01 CPUs=4 RealMemory=8000 # GroupName=named",
                      "NodeName=g3n01 # GroupName=bare"],
            "partitions": ["PartitionName=parts Nodes=parts Default=YES "
                           "MaxTime=1-00:00:00",
                           "PartitionName=named Nodes=named",
                           "PartitionName=bare Nodes=bare"],
            "gres": ["NodeName=g2n01 Name=mps Count=100 no_consume=true # "
                     "GroupName=named"],
            "genders": ["g1n01 all,parts", "g2n01 all,named", "g3n01 all,bare"],
        },
    },
    {
        "case": "two-gres-presets-for-one-group",
        "why": "a group whose gres preset is more than one line writes a line per preset "
               "per node, which is the one place a node is named more than once",
        "luna": {"node001": {"group": "gpu"}, "node002": {"group": "gpu"}},
        "nodes": [{"name": "node001", "group": "gpu"}, {"name": "node002", "group": "gpu"}],
        "defaults": {
            "nodes": ["# NodeName=gpu CPUs=16 RealMemory=64000"],
            "partitions": ["# PartitionName=gpu Default=NO"],
            "gres": ["# GroupGres=gpu Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1]",
                     "# GroupGres=gpu Name=mps Count=100 no_consume=true"],
        },
        "expect": {
            "nodes": ["NodeName=node001 CPUs=16 RealMemory=64000 # GroupName=gpu",
                      "NodeName=node002 CPUs=16 RealMemory=64000 # GroupName=gpu"],
            "partitions": ["PartitionName=gpu Nodes=gpu Default=NO"],
            "gres": ["NodeName=node001 Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1] # "
                     "GroupName=gpu",
                     "NodeName=node001 Name=mps Count=100 no_consume=true # GroupName=gpu",
                     "NodeName=node002 Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1] # "
                     "GroupName=gpu",
                     "NodeName=node002 Name=mps Count=100 no_consume=true # GroupName=gpu"],
            "genders": ["node001 all,gpu", "node002 all,gpu"],
        },
    },
    {
        "case": "no-managed-block-at-all",
        "why": "a file an administrator took the block out of is left exactly as it is: "
               "the generators refuse to put one back, which is how the override works",
        "luna": {"node001": {"group": "compute"}},
        "nodes": [{"name": "node001", "group": "compute"}],
        "managed": False,
        "defaults": {
            "nodes": ["# NodeName=compute CPUs=8 RealMemory=16000"],
            "partitions": ["# PartitionName=compute Default=YES"],
            "gres": ["# GroupGres=compute Name=mps Count=100 no_consume=true"],
        },
        "expect": {},
    },
    {
        "case": "edited-outside-the-block",
        "why": "whatever the administrator put around the block comes back byte for byte: "
               "their own directives, a line that reads like a marker and is not, and the "
               "blank lines they left at the end",
        "luna": {"node001": {"group": "compute"}},
        "nodes": [{"name": "node001", "group": "compute"}],
        "head": ["# the site's own settings, above everything",
                 "NodeName=DEFAULT State=UNKNOWN",
                 "### TrinityX Managed block start ###",
                 ""],
        "tail": ["", "# and below it", "Include /etc/slurm/site.conf", "", ""],
        "defaults": {
            "nodes": ["# NodeName=compute CPUs=8 RealMemory=16000"],
            "partitions": ["# PartitionName=compute Default=YES"],
            "gres": ["# GroupGres=compute Name=mps Count=100 no_consume=true"],
        },
        "expect": {
            "nodes": ["NodeName=node001 CPUs=8 RealMemory=16000 # GroupName=compute"],
            "partitions": ["PartitionName=compute Nodes=compute Default=YES"],
            "gres": ["NodeName=node001 Name=mps Count=100 no_consume=true # "
                     "GroupName=compute"],
            "genders": ["node001 all,compute"],
        },
    },
)


def nodesets(case: dict) -> list:
    """
    One NodeSet per group, in the order the groups first appear, folded as trixd folds a
    set of names. `folds` names a group whose fold trixd's own library does not reproduce.
    """
    groups: dict = {}
    for one in case["nodes"]:
        groups.setdefault(one["group"], []).append(one["name"])
    return [f"NodeSet={group} Nodes={case.get('folds', {}).get(group) or fold(names)}"
            for group, names in groups.items()]


def block(case: dict, kind: str) -> list:
    """
    The lines of the managed block. The templates open with a newline of their own, so the
    block begins with a blank line; one that renders nothing at all leaves no lines, and
    the two markers then sit together.
    """
    entries = list(case["expect"].get(kind) or [])
    if kind == "nodes" and entries:
        entries += ["", "# Nodesets", *nodesets(case)]
    return ["", *entries] if entries else []


def before(case: dict, kind: str) -> str:
    """
    The file as the administrator left it: their own lines, the Defaults block the
    generator reads its presets from, and a stale block of ours for the render to replace.
    """
    lines = list(case.get("head", HEAD))
    presets = case["defaults"].get(kind) or []
    if presets:
        lines += [BLOCK_START.format(manager=DEFAULTS), *presets,
                  BLOCK_END.format(manager=DEFAULTS)]
    if case.get("managed", True):
        lines += [BLOCK_START.format(manager=MANAGER), STALE,
                  BLOCK_END.format(manager=MANAGER)]
    lines += list(case.get("tail", TAIL))
    return "\n".join(lines) + "\n"


def whole(case: dict, kind: str) -> str:
    """
    The whole file a render has to leave behind: the administrator's text with our block's
    lines replaced, and the one newline at the end of the file that the writer puts there.
    """
    text = before(case, kind)
    if not case.get("managed", True):
        return text
    start = BLOCK_START.format(manager=MANAGER) + "\n"
    end = BLOCK_END.format(manager=MANAGER) + "\n"
    head, rest = text.split(start, 1)
    tail = rest.split(end, 1)[1]
    return head + start + "".join(line + "\n" for line in block(case, kind)) + end + tail


def golden(case: dict, kind: str) -> str:
    """
    Where the file the generators produced for this case is kept.
    """
    return os.path.join(GOLDEN, case["case"], KINDS[kind])
