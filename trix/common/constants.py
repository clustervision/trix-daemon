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

# No plugin may claim this name: it is the one-API route to Luna.
RESERVED_PLUGIN: str = "luna"

TOKEN_HEADER: str = "x-access-tokens"

# HS256 is what luna2-daemon signs with, so either daemon accepts the other's tokens.
TOKEN_ALGORITHM_SHARED: str = "HS256"
TOKEN_ALGORITHM_KEYPAIR: str = "EdDSA"
TOKEN_ALGORITHMS: tuple = (TOKEN_ALGORITHM_SHARED, TOKEN_ALGORITHM_KEYPAIR)
DEFAULT_TOKEN_ALGORITHM: str = TOKEN_ALGORITHM_SHARED
MINIMUM_SECRET_LENGTH: int = 32

# Luna reads back id, scope and node; id 0 is the administrator its ini names.
LUNA_CLAIM_ID: str = "id"
LUNA_CLAIM_NODE: str = "node"
LUNA_CLAIM_SCOPE: str = "scope"
LUNA_SCOPE_PROVISION: str = "provision"
LUNA_SUPERUSER_ID: int = 0
# Luna numbers its users from 1, so this one never exists and Luna answers 401.
LUNA_NO_USER_ID: int = -1

TOKEN_KIND_USER: str = "user"
TOKEN_KIND_NODE: str = "node"
TOKEN_KIND_SERVICE: str = "service"
# Where the ini account is known from: trixd.ini itself, never a source or a row.
SOURCE_INI: str = "ini"
TOKEN_KINDS: tuple = (TOKEN_KIND_USER, TOKEN_KIND_NODE, TOKEN_KIND_SERVICE)

# The ladder, lowest first. A principal with no role holds a token and nothing more.
ROLE_NONE: str = "none"
ROLE_OPERATOR: str = "operator"
ROLE_MANAGER: str = "manager"
ROLES: tuple = (ROLE_NONE, ROLE_OPERATOR, ROLE_MANAGER)

# What only the account the ini names may do, as luna2-daemon reserves its rootus
# families. A route may declare it; a principal can never be given it.
ROLE_ROOTUS: str = "rootus"
MINIMUMS: tuple = ROLES + (ROLE_ROOTUS,)

HTTP_VERBS: tuple = ("GET", "POST", "PATCH", "DELETE")

# The changes at Luna a plugin may act on, as the pass-through recognises them.
LUNA_HOOKS: tuple = ("node.update", "node.delete")
LUNA_HOOK_SUBSYSTEM: str = "luna"

PLUGIN_STATE_LOADED: str = "loaded"
PLUGIN_STATE_REFUSED: str = "refused"
PLUGIN_STATE_DISABLED: str = "disabled"
