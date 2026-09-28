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
Resolving a name to a principal and its usergroups, the way nsswitch does it.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import hashlib
import hmac
import os
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from trix.common import logging as trixlog
from trix.common.settings import current as settings
from trix.db import models
from trix.core.security import chain
from trix.utils import shell

LOGGER = trixlog.get("principals")

DIGEST_SCHEME: str = "pbkdf2_sha256"
DIGEST_ROUNDS: int = 200000


def hash_password(password: str) -> str:
    """
    Store a password as a salted derivation, never as a plain digest.
    """
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, DIGEST_ROUNDS)
    return f"{DIGEST_SCHEME}${DIGEST_ROUNDS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """
    Constant-time check against a stored derivation.
    """
    try:
        scheme, rounds, salt, expected = stored.split("$")
        if scheme != DIGEST_SCHEME:
            return False
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                      bytes.fromhex(salt), int(rounds))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(derived.hex(), expected)


class Resolved:
    """
    A principal the daemon could name, and the usergroups it belongs to.
    """

    def __init__(self, name: str, kind: str = "user", source: str = "",
                 usergroups: list = None, admin: bool = False) -> None:
        """
        A principal and what is known about it.
        """
        self.name = name
        self.kind = kind
        self.source = source
        self.usergroups = usergroups or []
        self.admin = admin

    def __repr__(self) -> str:
        """
        Enough to identify the principal in a log.
        """
        return f"<Resolved {self.name} kind={self.kind} src={self.source}>"


class _Cache:
    """
    A short memory, so a burst of requests does not walk the chain each time.
    """

    def __init__(self) -> None:
        """
        A principal and what is known about it.
        """
        self.entries: dict = {}

    def get(self, key: str, ttl: int):
        """
        A cached answer, if it is still young enough.
        """
        found = self.entries.get(key)
        if found and (time.monotonic() - found[0]) < ttl:
            return found[1]
        return None

    def put(self, key: str, value) -> None:
        """
        Remember an answer against the clock.
        """
        self.entries[key] = (time.monotonic(), value)

    def clear(self) -> None:
        """
        Forget everything, which a write to the store must do.
        """
        self.entries.clear()


CACHE = _Cache()


def _from_local(handle: Session, name: str) -> Resolved:
    """
    The principal row and the usergroups it belongs to.
    """
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    if row is None or not row.enabled:
        return None
    groups = handle.scalars(
        select(models.Usergroup.name)
        .join(models.Membership, models.Membership.usergroup_id == models.Usergroup.id)
        .where(models.Membership.principal_id == row.id)).all()
    return Resolved(row.name, row.kind, "local", list(groups), row.admin)


def resolve(handle: Session, name: str) -> Resolved:
    """
    Walk the chain with no credential. First answer wins, a failing source is skipped.
    """
    cached = CACHE.get(f"user:{name}", settings().auth.cache)
    if cached is not None:
        return cached
    identity, source = chain.resolve(handle, name)
    if identity is None:
        return None
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    if row is not None and not row.enabled:
        # Read here rather than in a source, because pam and ldap know nothing of
        # this flag: disabling somebody has to cut them whatever vouched for them.
        LOGGER.warning("%s is disabled here, so the token is refused", name)
        return None
    found = Resolved(identity.get("name") or name, "user", source,
                     list(identity.get("groups") or []),
                     bool(row.admin) if row is not None else False)
    CACHE.put(f"user:{name}", found)
    return found


def authenticate(handle: Session, name: str, password: str) -> Resolved:
    """
    Walk the chain. The first source that knows the name decides, and it checks the
    password itself: this function never sees a credential verified anywhere else.
    """
    identity, source = chain.authenticate(handle, name, password)
    if identity is None:
        return None
    return _adopt(handle, identity, source)


def _adopt(handle: Session, identity: dict, source: str) -> Resolved:
    """
    Turn a verified identity into a principal, creating the row on first login.
    It carries no digest: the source holds the credential and we must not.
    """
    name = identity.get("name") or ""
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    if row is None:
        row = models.Principal(name=name, kind="user", source=source, password_digest="")
        handle.add(row)
        handle.commit()
        LOGGER.info("first login of %s through %s; principal created", name, source)
    elif not row.enabled:
        LOGGER.warning("%s is disabled here, whatever %s says", name, source)
        return None

    groups = list(identity.get("groups") or [])
    local = _from_local(handle, name)
    if local is not None:
        groups = sorted(set(groups) | set(local.usergroups))
    found = Resolved(name, row.kind, source, groups, bool(row.admin))
    CACHE.put(f"user:{name}", found)
    return found
