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
The daemon's own principal table. A row with a digest is a login in its own right;
one without authenticates elsewhere, so this answers not-known and the chain goes on.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"


class Source:
    """
    The local principal table. See the README beside it.
    """

    def __init__(self, handle=None) -> None:
        """
        Take the session this source reads the table on.
        """
        self.handle = handle

    def authenticate(self, name: str = "", password: str = "") -> tuple:
        """
        A stored digest decides: verified, or refused and the chain stops.
        """
        from trixd.core.security.principals import verify_password

        row = self._row(name)
        if row is None or not row.password_digest:
            return None, f"{name} has no password in trixd"
        if not row.enabled:
            return False, f"{name} is disabled"
        if not verify_password(password, row.password_digest):
            return False, f"incorrect password for {name}"
        return True, self._identity(row)

    def resolve(self, name: str = "") -> tuple:
        """
        A local row is known whether or not it holds a digest.
        """
        row = self._row(name)
        if row is None:
            return False, f"{name} is not a principal here"
        return True, self._identity(row)

    def _row(self, name: str):
        """
        The live principal row for this name, or None.
        """
        from trixd.core.security.principals import row_for

        if self.handle is None:
            return None
        return row_for(self.handle, name)

    def _identity(self, row) -> dict:
        """
        The name, which is all the chain needs.
        """
        return {"name": row.name}
