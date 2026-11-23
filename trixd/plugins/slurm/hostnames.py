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

"""A set of node names folded into a hostlist, as the writers of the slurm files fold one."""

from __future__ import annotations

import re
from collections.abc import Iterable


# trinityx-config-slurm folds a NodeSet line on a name's FIRST run of digits and orders the
# names by prefix, then suffix, then that number. Three writers keep these files, so the bytes
# a NodeSet line carries have to be theirs.
_FIELDS = re.compile(r"([a-zA-Z_.-]+)([0-9]+)?([a-zA-Z0-9_.-]+)?")


class _Number:
    """
    A name's first run of digits, held at the width it was written at, so 001 folds back to
    001. Ordered by value alone, and against another number alone: a set mixing a name that
    carries a number with one that does not has no order, which is what sends the whole fold
    to the plain list below.
    """

    __slots__ = ("value", "width")

    def __init__(self, digits: str) -> None:
        self.value, self.width = int(digits), len(digits)

    def __str__(self) -> str:
        return str(self.value).rjust(self.width, "0")

    def __eq__(self, other) -> bool:
        if not isinstance(other, _Number):
            return NotImplemented
        return (self.value, self.width) == (other.value, other.width)

    def __lt__(self, other) -> bool:
        if not isinstance(other, _Number):
            raise TypeError("a number is ordered against another number alone")
        return self.value < other.value

    def after(self) -> "_Number":
        """The next number at the same width; a run does not cross into a wider one."""
        later = str(self.value + 1)
        if len(later) > self.width:
            raise ValueError("the next number is wider than this one")
        return _Number(later.rjust(self.width, "0"))


def _split(name: str) -> tuple:
    """The name as prefix, first number and whatever follows it."""
    prefix, digits, suffix = _FIELDS.search(name).groups()
    return prefix, _Number(digits) if digits else None, suffix


def _ranges(numbers: list) -> str:
    """001-004,009: the numbers of one prefix and suffix, as runs."""
    numbers = sorted(numbers)
    spans, start, end = [], numbers[0], numbers[0]
    for number in numbers[1:]:
        if number == end.after():
            end = number
            continue
        spans.append(str(start) if start == end else f"{start}-{end}")
        start = end = number
    spans.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(spans)


def _collect(names: list) -> str:
    """The names in order, each run of one prefix and suffix as one bracketed term."""
    parts = list(map(_split, names))
    parts.sort(key=lambda part: (part[0], part[2] or "", part[1]))
    runs: list = []
    for prefix, number, suffix in parts:
        if runs and runs[-1][0] == prefix and runs[-1][2] == suffix:
            runs[-1][1].append(number)
            continue
        runs.append([prefix, [number] if number else [], suffix])
    return ",".join(prefix + _term(numbers) + (suffix or "")
                    for prefix, numbers, suffix in runs)


def _term(numbers: list) -> str:
    """One number as it was written, more than one in brackets, none at all nothing."""
    if len(numbers) == 1:
        return str(numbers[0])
    return f"[{_ranges(numbers)}]" if numbers else ""


def fold_nodeset(names: Iterable[str]) -> str:
    """node[001-003], folded as the other writers of the slurm files fold a NodeSet line."""
    names = list(names)
    try:
        return _collect(names) if names else ""
    except (AttributeError, TypeError, ValueError):
        # What the generators' own filter answers for a set it cannot fold - a name carrying
        # no letters, a run that changes width partway - and these bytes have to be theirs.
        return ",".join(names)
