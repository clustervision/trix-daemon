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
The one table RBAC has, over the API: a user, a password, a role.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import re
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from trixd.common import logging as trixlog
from trixd.common.constants import API_PREFIX, ROLE_ROOTUS, ROLES
from trixd.common.errors import BadRequest, NotFound
from trixd.common.settings import current as settings
from trixd.core.security import access, audit, principals
from trixd.core.security.gate import Caller, guard
from trixd.db import models
from trixd.db.engine import scoped

LOGGER = trixlog.get("identity")

router = APIRouter(prefix=f"{API_PREFIX}/identity", tags=["identity"])

WRITE: str = "identity.write"

# A login name as the chain's sources know it; an Active Directory one carries an @.
NAME = re.compile(r"^[A-Za-z0-9_.@-]{1,255}$")


class UserIn(BaseModel):
    """
    What a caller may set on a user; a field left out stays as it is.
    """

    model_config = ConfigDict(extra="forbid")

    role: Optional[str] = None
    password: Optional[str] = None
    enabled: Optional[bool] = None


def _named(name: str) -> str:
    """
    A name the API may write: well formed, and never the account the ini names.
    """
    if not NAME.match(name or ""):
        raise BadRequest(f"{name} is not a login name")
    if access.is_rootus(name):
        raise BadRequest(f"{name} is the account the ini names: it is rootus, set there")
    return name


def _ladder(role: str) -> str:
    """
    A role must be on the ladder; anything else would sit at the bottom silently.
    """
    if role not in ROLES:
        raise BadRequest(f"{role} is not one of {', '.join(ROLES)}")
    return role


def _shown(row: models.Principal) -> dict:
    """
    A user as the API shows it: whether it has a password here, never the password.
    """
    return {"name": row.name, "role": row.role, "enabled": row.enabled,
            "local": bool(row.password_digest)}


@router.get("/users", summary="Every user and the role each holds")
def list_users(handle: Session = Depends(scoped),
               caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    The ini account first, then every live row; anyone without a row is none.
    """
    rows = handle.scalars(select(models.Principal)
                          .where(models.Principal.deleted.is_(False))
                          .order_by(models.Principal.name)).all()
    users = [{"name": settings().api.username, "role": ROLE_ROOTUS, "enabled": True,
              "local": False}]
    # A row left under the ini name by an older release is not a user: skip it.
    users += [_shown(row) for row in rows if not access.is_rootus(row.name)]
    return {"users": users}


@router.put("/users/{name}", summary="Give a user a role, a password, or both")
def put_user(name: str, body: UserIn, handle: Session = Depends(scoped),
             caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    Creates the user when it is new, and changes only what the body names when it is not.
    """
    _named(name)
    row, created = principals.claim_row(handle, name)
    if body.role is not None:
        row.role = _ladder(body.role)
    if body.password:
        row.password_digest = principals.hash_password(body.password)
    if body.enabled is not None:
        row.enabled = body.enabled
    if not created:
        row.version += 1
    handle.flush()
    audit.granted(caller.name, WRITE, f"user/{name}", "created" if created else "changed",
                  handle=handle)
    # Committed, then forgotten: cleared first, a request in between caches the old row.
    handle.commit()
    principals.CACHE.clear()
    return _shown(row)


@router.delete("/users/{name}", summary="Remove a user, and with it their role")
def delete_user(name: str, handle: Session = Depends(scoped),
                caller: Caller = Depends(guard(ROLE_ROOTUS))) -> dict:
    """
    The role and any password here go at once. A local user can no longer sign in; a
    PAM or LDAP user still can, and holds role none.
    """
    _named(name)
    row = principals.row_for(handle, name)
    if row is None:
        raise NotFound(f"no user named {name}")
    principals.retire_row(row)
    audit.granted(caller.name, WRITE, f"user/{name}", "deleted", handle=handle)
    handle.commit()
    principals.CACHE.clear()
    return {"deleted": name}
