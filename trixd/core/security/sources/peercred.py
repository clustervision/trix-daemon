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
The caller's own operating-system account, as the kernel reports it on trixd's socket.
The socket signs a person in; this source only answers for that person afterwards.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

from trixd.core.security.sources import pam

NO_PASSWORD: str = "peercred takes no password; connect to [AUTH] SOCKET instead"


class Source:
    """
    The account the kernel vouched for. See the README beside it.
    """

    def __init__(self, handle=None) -> None:
        """
        Take the session, for the pam source this one answers through.
        """
        self.handle = handle

    def authenticate(self, name: str = "", password: str = "") -> tuple:
        """
        Not known here: a password is never how this source signs anybody in.
        """
        return None, NO_PASSWORD

    def resolve(self, name: str = "") -> tuple:
        """
        Only an account that could log in itself, by the same rule as pam.
        """
        return pam.Source(self.handle).resolve(name)
