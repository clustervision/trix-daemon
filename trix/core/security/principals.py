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
Resolving a name to a principal and its role, the way nsswitch resolves a user.
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
from trix.common.constants import ROLE_NONE, SOURCE_INI
from trix.common.settings import current as settings
from trix.db import models
from trix.core.security import access, chain

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
    A user the daemon could name, where it was found, and its role here.
    """

    def __init__(self, name: str, source: str = "", role: str = ROLE_NONE) -> None:
        """
        A principal and what is known about it.
        """
        self.name = name
        self.source = source
        self.role = role

    def __repr__(self) -> str:
        """
        Enough to identify the principal in a log.
        """
        return f"<Resolved {self.name} role={self.role} src={self.source}>"


class _Cache:
    """
    A short memory, so a burst of requests does not walk the chain each time.
    """

    def __init__(self) -> None:
        """
        Start empty.
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


def row_for(handle: Session, name: str):
    """
    The live row for a name, or None. A deleted row is only a marker for the peer.
    """
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    return None if row is None or row.deleted else row


def claim_row(handle: Session, name: str) -> tuple:
    """
    The row for a name and whether it is new: created, or brought back from deletion as
    a fresh role-none user whose version moves on, so the peer takes it over the marker.
    """
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    if row is None:
        row = models.Principal(name=name, role=ROLE_NONE, password_digest="", enabled=True)
        handle.add(row)
        return row, True
    if row.deleted:
        row.deleted = False
        row.role = ROLE_NONE
        row.password_digest = ""
        row.enabled = True
        row.version += 1
        return row, True
    return row, False


def retire_row(row: models.Principal) -> None:
    """
    Delete a user by leaving a marker the peer can see, holding no role and no password.
    """
    row.deleted = True
    row.role = ROLE_NONE
    row.password_digest = ""
    row.enabled = True
    row.version += 1


def _ini_account(name: str) -> Resolved:
    """
    rootus: known from trixd.ini alone, never from a row or a source.
    """
    return Resolved(name, SOURCE_INI)


def resolve(handle: Session, name: str) -> Resolved:
    """
    The ini account first; anyone else through the chain with no credential, where the
    first answer wins and a failing source is skipped.
    """
    if access.is_rootus(name):
        return _ini_account(name)
    cached = CACHE.get(f"user:{name}", settings().auth.cache)
    if cached is not None:
        return cached
    identity, source = chain.resolve(handle, name)
    if identity is None:
        return None
    row = row_for(handle, name)
    if row is not None and not row.enabled:
        # Read here rather than in a source, because pam and ldap know nothing of
        # this flag: disabling somebody has to cut them whatever vouched for them.
        LOGGER.warning("%s is disabled here, so the token is refused", name)
        return None
    found = Resolved(identity.get("name") or name, source,
                     row.role if row is not None else ROLE_NONE)
    CACHE.put(f"user:{name}", found)
    return found


def authenticate(handle: Session, name: str, password: str) -> Resolved:
    """
    The ini account is checked against trixd.ini and never walks the chain. Anyone else
    does: the first source that knows the name decides, and checks the password itself.
    """
    if access.is_rootus(name):
        expected = settings().api.password
        if expected and hmac.compare_digest(password.encode(), expected.encode()):
            return _ini_account(name)
        return None
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
    row, created = claim_row(handle, name)
    if created:
        handle.commit()
        LOGGER.info("first login of %s through %s; principal created", name, source)
    elif not row.enabled:
        LOGGER.warning("%s is disabled here, whatever %s says", name, source)
        return None

    found = Resolved(name, source, row.role)
    CACHE.put(f"user:{name}", found)
    return found
