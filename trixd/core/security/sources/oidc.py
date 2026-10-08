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
OpenID Connect, named in the design and not built: this source never answers.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

NOT_YET: str = "oidc is named in [AUTH] CHAIN but no provider is configured yet"


class Source:
    """
    OpenID Connect. The chain may name it today; it answers once a provider exists.
    """

    def __init__(self, handle=None) -> None:
        """
        Nothing to set up until there is a provider to talk to.
        """
        self.handle = handle

    def authenticate(self, name: str = "", password: str = "") -> tuple:
        """
        Not known here, so the chain continues with the next source.
        """
        return None, NOT_YET

    def resolve(self, name: str = "") -> tuple:
        """
        Not known here, for the same reason.
        """
        return False, NOT_YET
