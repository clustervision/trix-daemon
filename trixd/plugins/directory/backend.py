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
Users and groups in 389ds, through the obol command line tool.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from trixd.common import logging as trixlog
from trixd.common.errors import BadRequest, NotFound
from trixd.utils import shell

LOGGER = trixlog.get("directory")

OBOL: str = "obol"
TIMEOUT: int = 30

# The password is deliberately absent: obol would take it as --password, where it sits in
# /proc for the life of the call, readable by anyone with a login on the controller. It goes
# in through _apart below instead.
USER_FLAGS: dict = {
    "uid": "--uid",
    "gid": "--gid",
    "shell": "--shell",
    "home": "--home",
    "mail": "--mail",
    "phone": "--phone",
    "surname": "--sn",
    "givenname": "--givenName",
    "expiry": "--expire",
    "groups": "--groups",
}

GROUP_FLAGS: dict = {
    "gid": "--gid",
    "members": "--users",
}


def _apart(body: dict, mapping: dict) -> tuple:
    """
    The obol arguments, and the password to hand over on standard input rather than in them.

    obol takes a password as an argument or from a prompt. An argument is world-readable
    while the command runs, so the prompt is used and the value goes down a pipe. The log
    masking beside this is what says the value was always known to be secret; it covered
    the log and not the process table.
    """
    sent = dict(body or {})
    password = sent.pop("password", None)
    argv = _flags(sent, mapping)
    if password:
        argv.append("--prompt-password")
    return argv, (f"{password}\n" if password else "")


def _flags(body: dict, mapping: dict) -> list:
    """
    Turn the validated body into obol arguments, skipping what was not sent.
    """
    argv = []
    for field, flag in mapping.items():
        value = body.get(field)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        argv += [flag, str(value)]
    return argv


def _normalise(record: dict) -> dict:
    """
    389ds names the membership attribute differently from openldap. The users
    application reads "member", so answer with it whichever is underneath.
    """
    if isinstance(record, dict) and "member" not in record and "uniqueMember" in record:
        record = dict(record)
        record["member"] = record.pop("uniqueMember")
    return record


class Backend:
    """
    One method per handler the manifest declares, and nothing else.
    """

    def list_users(self) -> list:
        """
        Every user obol can see.
        """
        return shell.run_json([OBOL, "-J", "user", "list"], TIMEOUT) or []

    def get_user(self, name: str) -> dict:
        """
        One user, or 404.
        """
        result = shell.run([OBOL, "-J", "user", "show", name], TIMEOUT)
        if not result.ok:
            raise NotFound(f"no user named {name}")
        return _normalise(shell.run_json([OBOL, "-J", "user", "show", name], TIMEOUT) or {})

    def create_user(self, body: dict) -> dict:
        """
        Create a user. The name comes from the body, not from the path.
        """
        name = (body or {}).get("username")
        if not name:
            raise BadRequest("username is required")
        if shell.run([OBOL, "-J", "user", "show", name], TIMEOUT).ok:
            raise BadRequest(f"user {name} already exists")
        argv, secret = _apart(body, USER_FLAGS)
        shell.run([OBOL, "user", "add", name] + argv, TIMEOUT, check=True,
                  secret_input=secret)
        LOGGER.info("created user %s", name)
        return self.get_user(name)

    def update_user(self, name: str, body: dict) -> dict:
        """
        Change the fields that were sent, and leave the rest alone.
        """
        if not shell.run([OBOL, "-J", "user", "show", name], TIMEOUT).ok:
            raise NotFound(f"no user named {name}")
        argv, secret = _apart(body, USER_FLAGS)
        if not argv:
            raise BadRequest("nothing to change")
        shell.run([OBOL, "user", "modify", name] + argv, TIMEOUT, check=True,
                  secret_input=secret)
        LOGGER.info("updated user %s", name)
        return self.get_user(name)

    def delete_user(self, name: str) -> dict:
        """
        Remove a user.
        """
        if not shell.run([OBOL, "-J", "user", "show", name], TIMEOUT).ok:
            raise NotFound(f"no user named {name}")
        shell.run([OBOL, "user", "delete", name], TIMEOUT, check=True)
        LOGGER.info("deleted user %s", name)
        return {"message": f"user {name} deleted"}

    def list_groups(self) -> list:
        """
        Every group obol can see.
        """
        records = shell.run_json([OBOL, "-J", "group", "list"], TIMEOUT) or []
        return [_normalise(item) for item in records] if isinstance(records, list) else records

    def get_group(self, name: str) -> dict:
        """
        One group, or 404.
        """
        if not shell.run([OBOL, "-J", "group", "show", name], TIMEOUT).ok:
            raise NotFound(f"no group named {name}")
        return _normalise(shell.run_json([OBOL, "-J", "group", "show", name], TIMEOUT) or {})

    def create_group(self, body: dict) -> dict:
        """
        Create a group.
        """
        name = (body or {}).get("groupname")
        if not name:
            raise BadRequest("groupname is required")
        if shell.run([OBOL, "-J", "group", "show", name], TIMEOUT).ok:
            raise BadRequest(f"group {name} already exists")
        shell.run([OBOL, "group", "add", name] + _flags(body, GROUP_FLAGS), TIMEOUT, check=True)
        LOGGER.info("created group %s", name)
        return self.get_group(name)

    def update_group(self, name: str, body: dict) -> dict:
        """
        Change the fields that were sent.
        """
        if not shell.run([OBOL, "-J", "group", "show", name], TIMEOUT).ok:
            raise NotFound(f"no group named {name}")
        argv = _flags(body, GROUP_FLAGS)
        if not argv:
            raise BadRequest("nothing to change")
        shell.run([OBOL, "group", "modify", name] + argv, TIMEOUT, check=True)
        LOGGER.info("updated group %s", name)
        return self.get_group(name)

    def delete_group(self, name: str) -> dict:
        """
        Remove a group.
        """
        if not shell.run([OBOL, "-J", "group", "show", name], TIMEOUT).ok:
            raise NotFound(f"no group named {name}")
        shell.run([OBOL, "group", "delete", name], TIMEOUT, check=True)
        LOGGER.info("deleted group %s", name)
        return {"message": f"group {name} deleted"}
