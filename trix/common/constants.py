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
Values that never change and never touch the filesystem.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

DAEMON_NAME: str = 'trix-daemon'
DAEMON_VERSION: str = '1.0'
API_VERSION: str = 'v1'
API_PREFIX: str = '/api/v1'

# No plugin may claim this name: it is the one-API route to Luna and the daemon
# forwards the whole subtree under it byte for byte.
RESERVED_PLUGIN: str = 'luna'

TOKEN_HEADER: str = 'x-access-tokens'
TOKEN_ALGORITHM: str = 'EdDSA'

TOKEN_KIND_USER: str = 'user'
TOKEN_KIND_NODE: str = 'node'
TOKEN_KIND_SERVICE: str = 'service'
TOKEN_KINDS: tuple = (TOKEN_KIND_USER, TOKEN_KIND_NODE, TOKEN_KIND_SERVICE)

# The shipped level. Until a grant exists it carries every capability, which is
# how an upgraded cluster keeps behaving as it did.
DEFAULT_LEVEL: str = 'administrator'
WILDCARD_CAPABILITY: str = '*'

HTTP_VERBS: tuple = ('GET', 'POST', 'PATCH', 'DELETE')

PLUGIN_STATE_LOADED: str = 'loaded'
PLUGIN_STATE_REFUSED: str = 'refused'
PLUGIN_STATE_DISABLED: str = 'disabled'
