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
389ds through obol, resolve only. obol has no verb that checks a password, so this
source never authenticates: it answers not-known and the chain continues.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from trix.utils import shell

TIMEOUT: int = 20


class Source:
    """
    389ds through obol. See the README beside it.
    """

    def __init__(self, handle=None) -> None:
        self.handle = handle

    def authenticate(self, name: str = "", password: str = "") -> tuple:
        """
        Never: obol can show a user and cannot verify one, so the chain continues.
        """
        return None, "obol cannot verify a password; pam or ldap authenticates"

    def resolve(self, name: str = "") -> tuple:
        """
        What obol holds for this name.
        """
        record = shell.run_json(["obol", "-J", "user", "show", name], timeout=TIMEOUT)
        if not record:
            return False, f"{name} is not in the directory"
        groups = record.get("memberOf") or record.get("groups") or []
        if isinstance(groups, str):
            groups = [groups]
        return True, {"name": name,
                      "external_id": str(record.get("uidNumber") or name),
                      "groups": [str(item).split(",")[0].replace("cn=", "")
                                 for item in groups]}
