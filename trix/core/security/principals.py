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
from trix.common.constants import WILDCARD_CAPABILITY
from trix.common.settings import current as settings
from trix.db import models
from trix.utils import shell

LOGGER = trixlog.get('principals')

DIGEST_SCHEME: str = 'pbkdf2_sha256'
DIGEST_ROUNDS: int = 200000


def hash_password(password: str) -> str:
    """Store a password as a salted derivation, never as a plain digest."""
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, DIGEST_ROUNDS)
    return f'{DIGEST_SCHEME}${DIGEST_ROUNDS}${salt.hex()}${derived.hex()}'


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored derivation."""
    try:
        scheme, rounds, salt, expected = stored.split('$')
        if scheme != DIGEST_SCHEME:
            return False
        derived = hashlib.pbkdf2_hmac('sha256', password.encode(),
                                      bytes.fromhex(salt), int(rounds))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(derived.hex(), expected)


class Resolved:
    """A principal the daemon could name, and the usergroups it belongs to."""

    def __init__(self, name: str, kind: str = 'user', source: str = '',
                 usergroups: list = None) -> None:
        self.name = name
        self.kind = kind
        self.source = source
        self.usergroups = usergroups or []

    def __repr__(self) -> str:
        return f'<Resolved {self.name} kind={self.kind} src={self.source}>'


class _Cache:
    """A short memory, so a burst of requests does not walk the chain each time."""

    def __init__(self) -> None:
        self.entries: dict = {}

    def get(self, key: str, ttl: int):
        found = self.entries.get(key)
        if found and (time.monotonic() - found[0]) < ttl:
            return found[1]
        return None

    def put(self, key: str, value) -> None:
        self.entries[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self.entries.clear()


CACHE = _Cache()


def _from_local(handle: Session, name: str) -> Resolved:
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    if row is None or not row.enabled:
        return None
    groups = handle.scalars(
        select(models.Usergroup.name)
        .join(models.Membership, models.Membership.usergroup_id == models.Usergroup.id)
        .where(models.Membership.principal_id == row.id)).all()
    return Resolved(row.name, row.kind, 'local', list(groups))


def _from_directory(handle: Session, name: str) -> Resolved:
    """389ds through obol, which is where the cluster users are."""
    try:
        record = shell.run_json(['obol', '-J', 'user', 'show', name], timeout=20)
    except Exception as error:
        LOGGER.warning('directory source skipped for %s: %s', name, error)
        return None
    if not record:
        return None
    groups = record.get('memberOf') or record.get('groups') or []
    if isinstance(groups, str):
        groups = [groups]
    return Resolved(name, 'user', 'directory', [str(item).split(',')[0].replace('cn=', '')
                                                for item in groups])


SOURCES: dict = {
    'local': _from_local,
    'directory': _from_directory,
}


def resolve(handle: Session, name: str) -> Resolved:
    """Walk the configured chain. First answer wins, a failing source is skipped."""
    config = settings().resolve
    cached = CACHE.get(f'user:{name}', config.cache)
    if cached is not None:
        return cached
    for source in config.users:
        reader = SOURCES.get(source)
        if reader is None:
            LOGGER.warning('no resolver named %s; check RESOLVE in trix.ini', source)
            continue
        try:
            found = reader(handle, name)
        except Exception as error:
            LOGGER.error('resolver %s failed for %s: %s', source, name, error)
            continue
        if found is not None:
            CACHE.put(f'user:{name}', found)
            return found
    return None


def authenticate(handle: Session, name: str, password: str) -> Resolved:
    """Check a password against the local table, then the directory."""
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    if row is not None and row.enabled and row.password_digest:
        if verify_password(password, row.password_digest):
            return resolve(handle, name)
        return None
    if 'directory' in settings().resolve.users:
        result = shell.run(['obol', 'user', 'auth', name], timeout=20)
        if result.ok:
            return resolve(handle, name)
    return None


def capabilities(handle: Session, who: Resolved) -> set:
    """Every capability this principal holds, through its grants and usergroups."""
    if who is None:
        return set()
    if handle.scalar(select(models.Grant.id).limit(1)) is None:
        # Nothing has been granted yet, so everything is permitted. That is what a
        # cluster does today, and it is what keeps an upgrade a non-event.
        return {WILDCARD_CAPABILITY}

    principal = handle.scalar(select(models.Principal).where(models.Principal.name == who.name))
    level_ids = set()
    if principal is not None:
        level_ids.update(handle.scalars(
            select(models.Grant.level_id).where(models.Grant.principal_id == principal.id)).all())
    if who.usergroups:
        level_ids.update(handle.scalars(
            select(models.Grant.level_id)
            .join(models.Usergroup, models.Usergroup.id == models.Grant.usergroup_id)
            .where(models.Usergroup.name.in_(who.usergroups))).all())
    if not level_ids:
        return set()
    return set(handle.scalars(
        select(models.LevelCapability.capability)
        .where(models.LevelCapability.level_id.in_(level_ids))).all())


def permits(held: set, wanted: str) -> bool:
    """Whether a held capability set covers the one an endpoint declared."""
    if not wanted or WILDCARD_CAPABILITY in held or wanted in held:
        return True
    # monitoring.* covers monitoring.read without listing every verb.
    family = wanted.split('.', 1)[0]
    return f'{family}.{WILDCARD_CAPABILITY}' in held
