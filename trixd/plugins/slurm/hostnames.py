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

"""A set of node names folded into a hostlist, with the python-hostlist Luna ships."""

from __future__ import annotations

from collections.abc import Iterable

import hostlist


def fold(names: Iterable[str]) -> str:
    """node[001-003]; names the library cannot fold go as a plain comma list."""
    names = list(names)
    try:
        return hostlist.collect_hostlist(names) if names else ""
    except hostlist.BadHostlist:
        return ",".join(names)
