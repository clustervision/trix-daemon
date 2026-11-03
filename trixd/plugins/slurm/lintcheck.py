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

"""slurmlint over the texts a save writes, for the plugin's helper. Nothing of the console's:
the helper runs this module on TrinityX's Python with `-I`, where the editor's own wheels are."""

from __future__ import annotations

import re

from slurmlint.linter import lint as slurmlint

_NODESET = re.compile(r"^NodeSet=(\S+)\s+Nodes=(\S+)")


def lint(texts: dict[str, str]) -> list[str]:
    """slurmlint over the nodes and partitions as a save would write them, one message per line.
    slurmlint 0.0.3 knows no NodeSet=: each partition's nodeset is expanded to its nodes first."""
    lines = (texts["nodes"] + "\n" + texts["partitions"]).split("\n")
    sets = {m[1]: m[2] for m in (_NODESET.match(ln) for ln in lines) if m}
    checked = [
        re.sub(r"\bNodes=(\S+)", lambda m: "Nodes=" + sets.get(m[1], m[1]), ln)
        if ln.startswith("PartitionName=")
        else ln
        for ln in lines
        if not _NODESET.match(ln)
    ]
    return [
        "%s: %s" % (checked[i - 1].strip(), msg)
        for i, msg in slurmlint("\n".join(checked))["errors"]
    ]
