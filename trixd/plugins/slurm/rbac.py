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

"""Luna's ladder (luna2-daemon utils/access.py): the bits a person holds on a governed row from its
`owners / usergroups / access` columns and Luna's `/whoami` for that person. The console holds the
same rules (its `rbac.py` with the two tables of its `daemonrules.py`); this copy lets the plugin
judge a person from their own Luna token. Standard library only."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: the most a usergroup role may hold of the team triplet
ROLE_CAPS: dict = {"admin": "rwx", "manager": "rwx", "operator": "r-x", "reader": "r--"}
#: the roles that lead a usergroup: they hold the hardware axis
LEADING_ROLES = ("admin", "manager")


def who_from(body: Mapping[str, Any]) -> dict:
    """`/whoami`'s answer as the fields the ladder reads (the caller checks it names a user)."""
    return {
        "user": str(body["user"]),
        "id": int(body.get("id", -1)),
        "source": str(body.get("source", "")),
        "admin": bool(body.get("admin")),
        "usergroups": dict(body.get("usergroups") or {}),
        "hardware": list(body.get("hardware") or []),
    }


def names(value: Any) -> list:
    """The names in a list Luna gives as "a, b" or as a list, in order."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _mask(bits: str, cap: str) -> str:
    """
    Takes a permission triplet such as rwx and the cap a role holds; returns the triplet with
    every bit the cap does not hold replaced by '-'.
    """
    return "".join(b if b == c else "-" for b, c in zip(bits, cap))


def _rank(cap: str) -> int:
    """
    Takes a cap such as r-x and returns how many bits it holds, so the broadest of a person's
    caps can be chosen.
    """
    return sum(1 for bit in cap if bit != "-")


def _order(role: str) -> int:
    """A role's place in ROLE_CAPS; one this copy does not know comes last (the console's copy
    raises on it: a sentence here, never a 500)."""
    order = list(ROLE_CAPS)
    return order.index(role) if role in order else len(order)


def can(row: Mapping[str, Any], who: Mapping[str, Any]) -> dict:
    """The person's bits on a governed row: admin allows; an owner holds the owner triplet; a
    member of a listed usergroup holds the team triplet capped by their best role; everyone else
    holds `other`. A row without an `access` column is one the daemon does not govern: fully
    held. An empty row is one the person may not read: nothing is held."""
    if not row:
        return {"read": False, "change": False, "operate": False, "hardware": False}
    mode = str(row.get("access") or "rwxrwxrwx")
    groups = set(names(row.get("usergroups")))
    shared = {name: role for name, role in who["usergroups"].items() if name in groups}
    if who["admin"]:
        bits = "rwx"
    elif who["user"] in names(row.get("owners")):
        bits = mode[0:3]
    elif shared:
        cap = max((ROLE_CAPS.get(role, "---") for role in shared.values()), key=_rank)
        bits = _mask(mode[3:6], cap)
    else:
        bits = mode[6:9]
    hardware = who["admin"] or any(
        role in LEADING_ROLES and name in who["hardware"] for name, role in shared.items()
    )
    return {"read": "r" in bits, "change": "w" in bits, "operate": "x" in bits,
            "hardware": hardware}


#: a bit as the daemon words it (utils/access.py ACTION, MAY)
ACTION = {"r": "reading", "w": "changing or removing", "x": "operating"}
MAY = {"r": "read", "w": "change", "x": "operate"}


def refusal(table: str, name: str, row: Mapping[str, Any], who: Mapping[str, Any],
            bit: str) -> str:
    """The daemon's sentence refusing `bit` on a row, "" when it allows; a row the person may
    not read is not there for them."""
    what = " ".join(w for w in (table, name) if w)
    held = can(row, who)
    if not held["read"]:
        return "%s is not available" % what
    if held[MAY[bit]]:
        return ""
    words = [MAY[b] for b in "rwx" if held[MAY[b]]]
    may = ", ".join(words[:-1]) + " and " + words[-1] if len(words) > 1 else words[0]
    shared = [r for g, r in who["usergroups"].items() if g in names(row.get("usergroups"))]
    if who["user"] in names(row.get("owners")):
        held_as = "owner"
    elif shared:
        held_as = min(shared, key=_order) + " role"
    else:
        held_as = "other"
    return "%s %s is not permitted: you may %s it (%s)" % (ACTION[bit], what, may, held_as)
