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

"""A node's short name (Prometheus and Alertmanager say the FQDN, Luna the short name), and a set
of names folded into a hostlist and back, with the python-hostlist Luna ships."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, TypeVar

import hostlist

V = TypeVar("V")


def short(host: Any) -> str:
    """node001.cluster -> node001."""
    return str(host or "").split(".")[0]


def by_short(values: Mapping[str, V]) -> dict[str, V]:
    """A mapping keyed by FQDN (Prometheus's node states), keyed by short name."""
    return {short(host): value for host, value in values.items()}


def fold(names: Iterable[str]) -> str:
    """node[001-003]; names the library cannot fold go as a plain comma list."""
    names = list(names)
    try:
        return hostlist.collect_hostlist(names) if names else ""
    except hostlist.BadHostlist:
        return ",".join(names)


def unfold(text: str) -> list[str]:
    """What `fold` made, back to names: node[001-003] to its three, and the plain comma list `fold` falls back to
    (a range the library refuses stays the names it was written as)."""
    try:
        return hostlist.expand_hostlist(text)
    except hostlist.BadHostlist:
        return [n for n in text.split(",") if n]


def expand(text: str) -> list[str]:
    """node[001-003] -> node001, node002, node003."""
    return hostlist.expand_hostlist(text)
