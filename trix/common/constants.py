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

DAEMON_NAME: str = "trix-daemon"
DAEMON_VERSION: str = "1.0"
API_VERSION: str = "v1"
API_PREFIX: str = f"/api/{API_VERSION}"

# No plugin may claim this name: it is the one-API route to Luna and the daemon
# forwards the whole subtree under it byte for byte.
RESERVED_PLUGIN: str = "luna"

TOKEN_HEADER: str = "x-access-tokens"

# HS256 and the shared secret are what luna2-daemon uses, so either daemon accepts
# the other's tokens. EdDSA is selectable, but only once Luna speaks it too.
TOKEN_ALGORITHM_SHARED: str = "HS256"
TOKEN_ALGORITHM_KEYPAIR: str = "EdDSA"
TOKEN_ALGORITHMS: tuple = (TOKEN_ALGORITHM_SHARED, TOKEN_ALGORITHM_KEYPAIR)
DEFAULT_TOKEN_ALGORITHM: str = TOKEN_ALGORITHM_SHARED

# Shorter than this and we complain. The installer writes 32 hexadecimal characters.
MINIMUM_SECRET_LENGTH: int = 32

# What Luna writes into its tokens. It reads back only scope and node, never id,
# and id 0 is the administrator luna.ini names rather than a row in its user table.
LUNA_CLAIM_ID: str = "id"
LUNA_CLAIM_NODE: str = "node"
LUNA_CLAIM_SCOPE: str = "scope"
LUNA_SCOPE_PROVISION: str = "provision"
LUNA_SUPERUSER_ID: int = 0

TOKEN_KIND_USER: str = "user"
TOKEN_KIND_NODE: str = "node"
TOKEN_KIND_SERVICE: str = "service"
TOKEN_KINDS: tuple = (TOKEN_KIND_USER, TOKEN_KIND_NODE, TOKEN_KIND_SERVICE)

# What a person may do inside a usergroup, capped by the role on their membership.
# The labels are stored, the bits they map to are fixed here, as luna2-daemon does.
ROLE_CAPS: dict = {"admin": "rwx", "manager": "rwx", "operator": "r-x", "reader": "r--"}
DEFAULT_ROLE: str = "reader"

# Read, change and operate. POSIX, and the same three letters luna2-daemon uses.
BITS: dict = {"r": 4, "w": 2, "x": 1}

# Owner rwx, the listed usergroups rwx, everyone else nothing. The same
# default luna2-daemon gives a node or a group.
DEFAULT_MODE: str = "770"

# The row on a plugin that carries its permissions. Per-document granularity
# is open: the columns are on every declaration, so it needs no schema change.
ACL_KIND: str = "acl"

# The shipped level. Until a grant exists it carries every capability, which is
# how an upgraded cluster keeps behaving as it did.
DEFAULT_LEVEL: str = "administrator"
WILDCARD_CAPABILITY: str = "*"

HTTP_VERBS: tuple = ("GET", "POST", "PATCH", "DELETE")

PLUGIN_STATE_LOADED: str = "loaded"
PLUGIN_STATE_REFUSED: str = "refused"
PLUGIN_STATE_DISABLED: str = "disabled"
