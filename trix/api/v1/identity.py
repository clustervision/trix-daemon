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
Principals, usergroups, memberships and the external group map, over the API.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from trix.common import logging as trixlog
from trix.common.constants import API_PREFIX, ROLE_MANAGER, ROLE_NONE, ROLES
from trix.common.errors import BadRequest, Conflict, NotFound
from trix.core.security import audit, principals
from trix.core.security.gate import Caller, guard
from trix.db import models
from trix.db.engine import scoped

LOGGER = trixlog.get("identity")

router = APIRouter(prefix=f"{API_PREFIX}/identity", tags=["identity"])

WRITE: str = "identity.write"

# A principal is not only a person: a service and a node are named here too, which
# is why this table is not called user the way luna2-daemon's is.
KINDS: tuple = ("user", "service", "node")


class PrincipalIn(BaseModel):
    """
    What a caller may set on a principal.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    kind: str = "user"
    source: str = "local"
    password: str = ""
    external_id: str = ""
    enabled: Optional[bool] = None
    role: Optional[str] = None


def _principal(handle: Session, name: str) -> models.Principal:
    """
    One principal by name, or 404.
    """
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    if row is None:
        raise NotFound(f"no principal named {name}")
    return row


def _ladder(role: str) -> str:
    """
    A role must be on the ladder; anything else would sit at the bottom silently.
    """
    if role not in ROLES:
        raise BadRequest(f"{role} is not one of {', '.join(ROLES)}")
    return role


def _shown(row: models.Principal) -> dict:
    """
    A principal as the API shows it.
    """
    return {"name": row.name, "kind": row.kind, "source": row.source,
            "enabled": row.enabled, "role": row.role, "external_id": row.external_id}


@router.get("/principals", summary="Every principal this daemon knows")
def list_principals(handle: Session = Depends(scoped),
                    caller: Caller = Depends(guard(ROLE_NONE))) -> dict:
    """
    All of them, with their usergroups and roles.
    """
    rows = handle.scalars(select(models.Principal).order_by(models.Principal.name)).all()
    return {"principals": [_shown(row) for row in rows]}


@router.get("/principals/{name}", summary="One principal")
def get_principal(name: str, handle: Session = Depends(scoped),
                  caller: Caller = Depends(guard(ROLE_NONE))) -> dict:
    """
    One, by name.
    """
    return _shown(_principal(handle, name))


@router.post("/principals", status_code=201, summary="Create a principal")
def create_principal(body: PrincipalIn, handle: Session = Depends(scoped),
                     caller: Caller = Depends(guard(ROLE_MANAGER))) -> dict:
    """
    A row with a password digest is a login here; one without authenticates elsewhere.
    """
    if not body.name:
        raise BadRequest("a principal needs a name")
    if body.kind not in KINDS:
        raise BadRequest(f"{body.kind} is not one of {', '.join(KINDS)}")
    if handle.scalar(select(models.Principal).where(models.Principal.name == body.name)):
        raise Conflict(f"{body.name} already exists")

    row = models.Principal(
        name=body.name, kind=body.kind, source=body.source,
        external_id=body.external_id,
        password_digest=principals.hash_password(body.password) if body.password else "",
        enabled=True if body.enabled is None else body.enabled,
        role=_ladder(body.role) if body.role else ROLE_NONE)
    handle.add(row)
    handle.flush()
    audit.granted(caller.name, WRITE, f"principal/{body.name}", "created", handle=handle)
    return _shown(row)


@router.patch("/principals/{name}", summary="Change a principal")
def update_principal(name: str, body: PrincipalIn, handle: Session = Depends(scoped),
                     caller: Caller = Depends(guard(ROLE_MANAGER))) -> dict:
    """
    Only what the body names is changed; an absent field is left alone.
    """
    row = _principal(handle, name)
    if body.password:
        row.password_digest = principals.hash_password(body.password)
    if body.external_id:
        row.external_id = body.external_id
    if body.kind and body.kind != row.kind:
        if body.kind not in KINDS:
            raise BadRequest(f"{body.kind} is not one of {', '.join(KINDS)}")
        row.kind = body.kind
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.role is not None:
        row.role = _ladder(body.role)
    row.version += 1
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"principal/{name}", "changed", handle=handle)
    return _shown(row)


@router.delete("/principals/{name}", summary="Delete a principal")
def delete_principal(name: str, handle: Session = Depends(scoped),
                     caller: Caller = Depends(guard(ROLE_MANAGER))) -> dict:
    """
    The cache goes with it, so a token for the name is refused on its next request.
    """
    row = _principal(handle, name)
    handle.delete(row)
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"principal/{name}", "deleted", handle=handle)
    return {"deleted": name}
