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
The controller's operating system, through PAM. On a TrinityX controller sssd is
joined to the cluster directory, so this reaches 389ds with no settings here.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import os

from trix.common.settings import current as settings

SYSTEM_SHELLS: tuple = ("nologin", "false", "")
FALLBACK_UID_MIN: int = 1000


class Source:
    """
    The controller's operating system. See the README beside it.
    """

    def __init__(self, handle=None) -> None:
        # python-pam sits on libpam, and pwd and grp are Unix only. All three are
        # imported here so this source fails loudly and the rest of the chain works.
        """
        Hold what this object needs.
        """
        import grp
        import pam
        import pwd

        self.pam = pam
        self.pwd = pwd
        self.grp = grp
        self.service = settings().auth.pam_service

    def authenticate(self, name: str = "", password: str = "") -> tuple:
        """
        A name the name service does not know is not-known; a wrong password stops here.
        """
        entry = self._passwd(name)
        if entry is None:
            return None, f"{name} is not a user of this system"
        conversation = self.pam.pam()
        if not conversation.authenticate(name, password, service=self.service):
            return False, f"PAM refused {name}: {conversation.reason}"
        return True, self._identity(entry)

    def resolve(self, name: str = "") -> tuple:
        """
        Only an account that could log in itself: a system account is not resolved.
        """
        entry = self._passwd(name)
        if entry is None:
            return False, f"{name} is not a user of this system"
        if not self._can_log_in(entry):
            return False, f"{name} is a system account"
        return True, self._identity(entry)

    def _can_log_in(self, entry) -> bool:
        """
        A login shell, and a uid at or above the system's first ordinary uid.
        """
        if os.path.basename(getattr(entry, "pw_shell", "") or "") in SYSTEM_SHELLS:
            return False
        return entry.pw_uid >= self._uid_min()

    def _uid_min(self) -> int:
        """
        UID_MIN from login.defs, or the usual 1000 when it cannot be read.
        """
        try:
            with open("/etc/login.defs", encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) == 2 and parts[0] == "UID_MIN":
                        return int(parts[1])
        except (OSError, ValueError):
            pass
        return FALLBACK_UID_MIN

    def _passwd(self, name: str):
        """
        The passwd entry for this name, or None when the name service has none.
        """
        try:
            return self.pwd.getpwnam(name)
        except KeyError:
            return None

    def _identity(self, entry) -> dict:
        """
        The name, the uid as the stable id, and every group the name service gives.
        """
        groups = []
        for gid in os.getgrouplist(entry.pw_name, entry.pw_gid):
            try:
                groups.append(self.grp.getgrgid(gid).gr_name)
            except KeyError:
                continue
        return {"name": entry.pw_name, "external_id": str(entry.pw_uid), "groups": groups}
