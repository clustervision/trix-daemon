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
What trixd shows Luna to be let in.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from datetime import datetime, timedelta, timezone

import jwt

from trixd.common import logging as trixlog
from trixd.common.constants import (DAEMON_NAME, LUNA_CLAIM_ID, LUNA_SUPERUSER_ID,
                                    TOKEN_ALGORITHM_SHARED, TOKEN_KIND_SERVICE)
from trixd.common.errors import Unauthenticated
from trixd.core.security import keys
from trixd.plugins.luna import config as luna_config

LOGGER = trixlog.get("luna.credentials")


def for_luna(expiry: int = 300) -> str:
    """
    What trixd shows Luna for its own calls: Luna's administrator, id 0, signed with the
    secret from luna.ini, so no Luna password is ever stored for trixd. Always HS256,
    which is what Luna verifies, whatever trixd signs its own tokens with.
    """
    secret = luna_config.current().secret
    if not secret:
        raise Unauthenticated(f"{luna_config.current().path} has no [API] SECRET_KEY")
    issued = datetime.now(timezone.utc)
    payload = {"iss": keys.current().issuer, "sub": DAEMON_NAME, "kind": TOKEN_KIND_SERVICE,
               "src": "local", "iat": issued, "exp": issued + timedelta(seconds=expiry),
               LUNA_CLAIM_ID: LUNA_SUPERUSER_ID}
    return jwt.encode(payload, secret, algorithm=TOKEN_ALGORITHM_SHARED)
