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
What a caller may do: their role on the ladder against the minimum a route declares.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from sqlalchemy import select
from sqlalchemy.orm import Session

from trix.common import logging as trixlog
from trix.common.settings import current as settings
from trix.common.constants import ACL_KIND, BITS, DEFAULT_MODE, ROLES, ROLE_NONE
from trix.db import models

LOGGER = trixlog.get("access")


def mask(text: str) -> int:
    """
    The numeric value of a triplet written as rwx, ignoring the dashes.
    """
    return sum(BITS[char] for char in str(text or "") if char in BITS)


def text(triplet: int) -> str:
    """
    One octal digit written the way ls writes it.
    """
    return (("r" if triplet & 4 else "-") + ("w" if triplet & 2 else "-")
            + ("x" if triplet & 1 else "-"))


def mode_text(octal: str) -> str:
    """
    A whole mode, so a person reads rwxrwx--- rather than 770.
    """
    value = int(str(octal or DEFAULT_MODE), 8)
    return "".join(text((value >> shift) & 7) for shift in (6, 3, 0))


def acl_row(handle: Session, plugin: str) -> models.Declaration:
    """
    The row carrying a plugin's permissions, made on first use with the defaults.
    """
    row = handle.scalar(select(models.Declaration).where(
        models.Declaration.plugin == plugin,
        models.Declaration.kind == ACL_KIND,
        models.Declaration.name == ""))
    if row is None:
        row = models.Declaration(plugin=plugin, kind=ACL_KIND, name="", body={},
                                 owners="", usergroups="", access=DEFAULT_MODE)
        handle.add(row)
        handle.flush()
    return row


def is_rootus(name: str) -> bool:
    """
    The account the ini names, checked against nothing. It is the break glass and
    the system user, and every use of it is in the audit trail.
    """
    return bool(name) and name == settings().api.username


def rank(role: str) -> int:
    """
    Where a role sits on the ladder; anything unknown sits at the bottom.
    """
    return ROLES.index(role) if role in ROLES else 0


def permits(who, minimum: str) -> tuple:
    """
    Whether the caller reaches the minimum, and the role they hold. rootus always does.
    """
    held = getattr(who, "role", ROLE_NONE) or ROLE_NONE
    if who is not None and is_rootus(who.name):
        return True, held
    return rank(held) >= rank(minimum), held
