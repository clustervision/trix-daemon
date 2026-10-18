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

from trixd.common import logging as trixlog
from trixd.common.settings import current as settings
from trixd.common.constants import MINIMUMS, ROLE_NONE

LOGGER = trixlog.get("access")


def is_rootus(name: str) -> bool:
    """
    The account the ini names, checked against nothing. It is the break glass and
    the system user.

    Every use of it that goes through the gate is in the audit trail. The route to Luna is
    not guarded, so it is the one place this account does the most - the caller's own
    headers are forwarded and Luna honours the ini account as its administrator - and
    leaves nothing here. Luna's own log is where that is answerable for.
    """
    return bool(name) and name == settings().api.username


def rank(role: str) -> int:
    """
    Where a role sits on the ladder; anything unknown sits at the bottom.

    Safe for the role a caller holds, which is what the bottom is for, and not for the
    minimum a route asks: a minimum nobody recognises would sit at the bottom too and
    admit everyone. Nothing can reach that today - a manifest minimum is checked against
    the ladder when the plugin loads, and the core's routes pass constants - so this is a
    note about which direction holds, not a hole.
    """
    return MINIMUMS.index(role) if role in MINIMUMS else 0


def permits(who, minimum: str) -> tuple:
    """
    Whether the caller reaches the minimum, and the role they hold. rootus always does.

    No token kind reaches a minimum on its own. The other controller is not an exception:
    it signs in for a token of its own and is judged on the account it signed in with.
    """
    held = getattr(who, "role", ROLE_NONE) or ROLE_NONE
    if who is not None and is_rootus(who.name):
        return True, held
    return rank(held) >= rank(minimum), held
