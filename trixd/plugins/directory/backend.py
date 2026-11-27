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
from trixd.common.errors import BackendFailure, BadRequest, NotFound
from trixd.core.security import principals
from trixd.utils import shell

LOGGER = trixlog.get("directory")

OBOL: str = "obol"
TIMEOUT: int = 30
# How obol's error line opens for a name the directory does not hold.
MISSING: str = "[LookupError]"

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
    "commonname": "--cn",
    "surname": "--sn",
    "givenname": "--givenName",
    "expiry": "--expire",
    "groups": "--groups",
}

GROUP_FLAGS: dict = {
    "gid": "--gid",
    "members": "--users",
}

# What a user's passwordState says: whether a password is set, never the hash.
CONFIGURED: str = "configured"
UNSET: str = "unset"


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
    if password == "":
        raise BadRequest("password must not be empty")
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
            value = ",".join(_named(item) for item in value)
        argv += [flag, str(value)]
    return argv


def _named(name: str) -> str:
    """
    A name obol may be handed. obol puts it into an LDAP filter as it stands, reads one that
    opens with a dash as an option, and splits a list of them on the comma.
    """
    if (not isinstance(name, str) or not principals.LOGIN_NAME.fullmatch(name)
            or name.startswith("-")):
        raise BadRequest(f"{name} is not a name the directory takes")
    return name


def _held(kind: str, name: str) -> bool:
    """
    Whether the directory holds the user or group. Only obol's own miss is a no: any other
    failure is the directory not answering, which must not read as the record being absent.
    """
    result = shell.run([OBOL, "-J", kind, "show", _named(name)], TIMEOUT)
    if not result.ok and MISSING not in result.err:
        # Journal only, as shell.run does for a checked command: obol can echo its bind
        # credentials on a failure.
        LOGGER.error("%s exited %s: %s", OBOL, result.code, result.err or result.out)
        raise BackendFailure(f"{OBOL} failed with exit status {result.code}; "
                             f"the reason is in the journal")
    return result.ok


def _normalise(record: dict) -> dict:
    """
    What every obol record passes through on its way out.

    389ds names the membership attribute differently from openldap; the users application
    reads "member", so answer with it whichever is underneath. The password hash obol
    answers with the rest of the record is dropped: nothing above the directory manages a
    user with it. Matched whatever its case, because obol and 389ds decide that case and a
    change there must not put the hash back into the answer.
    """
    if not isinstance(record, dict):
        return record
    record = {key: value for key, value in record.items() if key.lower() != "userpassword"}
    if "member" not in record and "uniqueMember" in record:
        record["member"] = record.pop("uniqueMember")
    return record


def _user(record: dict) -> dict:
    """
    A user on its way out: the hash is dropped, and whether there was one is answered as
    passwordState, which is all the users application shows of a password.
    """
    shown = _normalise(record)
    if isinstance(record, dict):
        held = any(key.lower() == "userpassword" and value for key, value in record.items())
        shown["passwordState"] = CONFIGURED if held else UNSET
    return shown


class Backend:
    """
    One method per handler the manifest declares, and nothing else.
    """

    def list_users(self) -> list:
        """
        Every user obol can see.
        """
        records = shell.run_json([OBOL, "-J", "user", "list"], TIMEOUT) or []
        return [_user(item) for item in records] if isinstance(records, list) else records

    def get_user(self, name: str) -> dict:
        """
        One user, or 404.
        """
        if not _held("user", name):
            raise NotFound(f"no user named {name}")
        return _user(shell.run_json([OBOL, "-J", "user", "show", name], TIMEOUT) or {})

    def create_user(self, body: dict) -> dict:
        """
        Create a user. The name comes from the body, not from the path.
        """
        name = (body or {}).get("username")
        if not name:
            raise BadRequest("username is required")
        if _held("user", name):
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
        if not _held("user", name):
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
        if not _held("user", name):
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
        if not _held("group", name):
            raise NotFound(f"no group named {name}")
        return _normalise(shell.run_json([OBOL, "-J", "group", "show", name], TIMEOUT) or {})

    def create_group(self, body: dict) -> dict:
        """
        Create a group.
        """
        name = (body or {}).get("groupname")
        if not name:
            raise BadRequest("groupname is required")
        if _held("group", name):
            raise BadRequest(f"group {name} already exists")
        shell.run([OBOL, "group", "add", name] + _flags(body, GROUP_FLAGS), TIMEOUT, check=True)
        LOGGER.info("created group %s", name)
        return self.get_group(name)

    def update_group(self, name: str, body: dict) -> dict:
        """
        Change the fields that were sent.
        """
        if not _held("group", name):
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
        if not _held("group", name):
            raise NotFound(f"no group named {name}")
        shell.run([OBOL, "group", "delete", name], TIMEOUT, check=True)
        LOGGER.info("deleted group %s", name)
        return {"message": f"group {name} deleted"}
