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
A directory bind, for a directory not wired into the controller. FreeIPA and Active
Directory are this source with other attribute names.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from trixd.common.settings import current as settings

RECEIVE_TIMEOUT: int = 10


class Source:
    """
    A directory bind. See the README beside it.
    """

    def __init__(self, handle=None) -> None:
        """
        Hold what this object needs.
        """
        # ldap3 is pure Python; imported here so a controller without it fails this
        # source loudly and leaves the rest of the chain working.
        import ldap3

        self.ldap3 = ldap3
        config = settings().auth
        if not config.ldap_uri or not config.ldap_base:
            raise ValueError("[AUTH_LDAP] needs URI and BASE")
        self.uri = config.ldap_uri
        self.base = config.ldap_base
        self.bind_dn = config.ldap_bind_dn or None
        self.bind_password = config.ldap_bind_password or None
        self.user_attribute = config.ldap_user_attribute

    def authenticate(self, name: str = "", password: str = "") -> tuple:
        """
        An entry the search does not find is not-known; a failed bind stops the chain.
        """
        entry = self._search(name)
        if entry is None:
            return None, f"{name} is not in the directory"
        if not password or not self._bind(entry["dn"], password):
            return False, f"the directory refused the password of {name}"
        return True, self._identity(name, entry)

    def resolve(self, name: str = "") -> tuple:
        """
        The entry, with no credential checked.
        """
        entry = self._search(name)
        if entry is None:
            return False, f"{name} is not in the directory"
        return True, self._identity(name, entry)

    def _connection(self, user=None, password=None):
        """
        One bound connection, used for the search and again for the person's own bind.
        """
        server = self.ldap3.Server(self.uri, get_info=self.ldap3.NONE)
        return self.ldap3.Connection(server, user=user, password=password,
                                     auto_bind=True, receive_timeout=RECEIVE_TIMEOUT)

    def _search(self, name: str):
        """
        The name is escaped, so a syntax character searches for that character.
        """
        safe = self.ldap3.utils.conv.escape_filter_chars(name)
        with self._connection(self.bind_dn, self.bind_password) as connection:
            # No attributes asked for: the entry's DN is all the bind needs.
            connection.search(self.base, f"({self.user_attribute}={safe})")
            if not connection.entries:
                return None
            return {"dn": connection.entries[0].entry_dn}

    def _bind(self, dn: str, password: str) -> bool:
        """
        The person's own bind is the credential check.
        """
        try:
            with self._connection(dn, password):
                return True
        except self.ldap3.core.exceptions.LDAPBindError:
            return False

    def _identity(self, name: str, entry: dict) -> dict:
        """
        The login name, which is all the chain needs.
        """
        return {"name": name}
