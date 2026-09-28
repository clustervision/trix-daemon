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
What a caller may do to an object: the class triplet, capped by their role.
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
from trix.common.constants import (ACL_KIND, BITS, DEFAULT_MODE, DEFAULT_ROLE,
                                   ROLE_CAPS)
from trix.db import models

LOGGER = trixlog.get("access")

READING: tuple = ("GET", "HEAD", "OPTIONS")

# A POST under this segment operates the thing rather than changing it, which is
# what x means: power, console, restart.
ACTION_SEGMENT: str = "/actions/"

ACTION: dict = {"r": "reading", "w": "changing", "x": "operating"}


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


def bit_for(method: str = "GET", path: str = "", declared: str = "") -> str:
    """
    The bit a request needs: the method says it, unless the manifest overrides.
    """
    if declared:
        return declared
    if str(method).upper() in READING:
        return "r"
    return "x" if ACTION_SEGMENT in str(path or "") else "w"


def names(value: str) -> list:
    """
    A comma separated column as a list, which is the shape the tree already uses.
    """
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


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


def organised(handle: Session) -> bool:
    """
    Whether anyone is in a usergroup yet. Until somebody is, everything is
    permitted, which is what a cluster does today and keeps an upgrade quiet.
    """
    return handle.scalar(select(models.Usergroup.id).limit(1)) is not None


def held(handle: Session, who, row: models.Declaration, roles: dict) -> str:
    """
    The three characters this caller effectively holds on this object.
    """
    value = int(str(row.access or DEFAULT_MODE), 8)
    if who is not None and who.name in names(row.owners):
        return text((value >> 6) & 7)

    shared = [name for name in names(row.usergroups) if name in roles]
    if shared:
        cap = max(mask(ROLE_CAPS.get(roles[name], DEFAULT_ROLE)) for name in shared)
        return text(((value >> 3) & 7) & cap)
    return text(value & 7)


def roles_of(handle: Session, name: str) -> dict:
    """
    Every usergroup this principal is in, and the role it holds in each.
    """
    rows = handle.execute(
        select(models.Usergroup.name, models.Membership.role)
        .join(models.Membership, models.Membership.usergroup_id == models.Usergroup.id)
        .join(models.Principal, models.Principal.id == models.Membership.principal_id)
        .where(models.Principal.name == name)).all()
    return {group: role for group, role in rows}


def permits(handle: Session, who, plugin: str, want: str) -> tuple:
    """
    Whether the caller holds this bit on this plugin, and what they do hold.
    """
    if who is not None and is_rootus(who.name):
        return True, "rwx"
    if who is not None and getattr(who, "admin", False):
        return True, "rwx"
    if not organised(handle):
        return True, "rwx"

    roles = roles_of(handle, who.name) if who is not None else {}
    bits = held(handle, who, acl_row(handle, plugin), roles)
    return want in bits, bits
