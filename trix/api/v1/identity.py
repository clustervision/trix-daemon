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
from trix.common.constants import API_PREFIX, DEFAULT_ROLE, ROLE_CAPS
from trix.common.errors import BadRequest, Conflict, NotFound
from trix.core.security import audit, principals
from trix.core.security.gate import Caller, guard
from trix.db import models
from trix.db.engine import scoped

LOGGER = trixlog.get("identity")

router = APIRouter(prefix=f"{API_PREFIX}/identity", tags=["identity"])

READ: str = "identity.read"
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
    admin: Optional[bool] = None
    delegate: Optional[bool] = None


class UsergroupIn(BaseModel):
    """
    What a caller may set on a usergroup.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = ""


class MemberIn(BaseModel):
    """
    A principal placed in a usergroup, with the role that caps what it may do there.
    """

    model_config = ConfigDict(extra="forbid")

    principal: str = ""
    role: str = DEFAULT_ROLE


class MapIn(BaseModel):
    """
    One external group, from one source, onto a usergroup and a role in it.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = ""
    external_group: str = ""
    usergroup: str = ""
    role: str = DEFAULT_ROLE


def _principal(handle: Session, name: str) -> models.Principal:
    """
    One principal by name, or 404.
    """
    row = handle.scalar(select(models.Principal).where(models.Principal.name == name))
    if row is None:
        raise NotFound(f"no principal named {name}")
    return row


def _usergroup(handle: Session, name: str) -> models.Usergroup:
    """
    One usergroup by name, or 404.
    """
    row = handle.scalar(select(models.Usergroup).where(models.Usergroup.name == name))
    if row is None:
        raise NotFound(f"no usergroup named {name}")
    return row


def _check_role(role: str) -> str:
    """
    A role must be one of the four; anything else would cap to nothing silently.
    """
    if role not in ROLE_CAPS:
        raise BadRequest(f"{role} is not one of {', '.join(sorted(ROLE_CAPS))}")
    return role


def _shown(row: models.Principal, handle: Session) -> dict:
    """
    A principal as the API shows it, with its usergroups and the role in each.
    """
    rows = handle.execute(
        select(models.Usergroup.name, models.Membership.role)
        .join(models.Membership, models.Membership.usergroup_id == models.Usergroup.id)
        .where(models.Membership.principal_id == row.id)).all()
    return {"name": row.name, "kind": row.kind, "source": row.source,
            "enabled": row.enabled, "admin": row.admin, "delegate": row.delegate,
            "external_id": row.external_id,
            "usergroups": {name: role for name, role in rows}}


@router.get("/principals", summary="Every principal this daemon knows")
def list_principals(handle: Session = Depends(scoped),
                    caller: Caller = Depends(guard(READ))) -> dict:
    """
    All of them, with their usergroups and roles.
    """
    rows = handle.scalars(select(models.Principal).order_by(models.Principal.name)).all()
    return {"principals": [_shown(row, handle) for row in rows]}


@router.get("/principals/{name}", summary="One principal")
def get_principal(name: str, handle: Session = Depends(scoped),
                  caller: Caller = Depends(guard(READ))) -> dict:
    """
    One, by name.
    """
    return _shown(_principal(handle, name), handle)


@router.post("/principals", status_code=201, summary="Create a principal")
def create_principal(body: PrincipalIn, handle: Session = Depends(scoped),
                     caller: Caller = Depends(guard(WRITE))) -> dict:
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
        admin=bool(body.admin), delegate=bool(body.delegate))
    handle.add(row)
    handle.flush()
    audit.granted(caller.name, WRITE, f"principal/{body.name}", "created", handle=handle)
    return _shown(row, handle)


@router.patch("/principals/{name}", summary="Change a principal")
def update_principal(name: str, body: PrincipalIn, handle: Session = Depends(scoped),
                     caller: Caller = Depends(guard(WRITE))) -> dict:
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
    for field in ("enabled", "admin", "delegate"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value)
    row.version += 1
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"principal/{name}", "changed", handle=handle)
    return _shown(row, handle)


@router.delete("/principals/{name}", summary="Delete a principal")
def delete_principal(name: str, handle: Session = Depends(scoped),
                     caller: Caller = Depends(guard(WRITE))) -> dict:
    """
    Its memberships go with it, so nothing is left pointing at a row that is gone.
    """
    row = _principal(handle, name)
    handle.query(models.Membership).filter(
        models.Membership.principal_id == row.id).delete()
    handle.delete(row)
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"principal/{name}", "deleted", handle=handle)
    return {"deleted": name}


@router.get("/usergroups", summary="Every usergroup")
def list_usergroups(handle: Session = Depends(scoped),
                    caller: Caller = Depends(guard(READ))) -> dict:
    """
    All of them, with their members and the role each holds.
    """
    out = []
    for group in handle.scalars(select(models.Usergroup).order_by(models.Usergroup.name)):
        rows = handle.execute(
            select(models.Principal.name, models.Membership.role)
            .join(models.Membership, models.Membership.principal_id == models.Principal.id)
            .where(models.Membership.usergroup_id == group.id)).all()
        out.append({"name": group.name,
                    "members": {name: role for name, role in rows}})
    return {"usergroups": out}


@router.post("/usergroups", status_code=201, summary="Create a usergroup")
def create_usergroup(body: UsergroupIn, handle: Session = Depends(scoped),
                     caller: Caller = Depends(guard(WRITE))) -> dict:
    """
    The organisation an object is listed against; the role lives on the membership.
    """
    if not body.name:
        raise BadRequest("a usergroup needs a name")
    if handle.scalar(select(models.Usergroup).where(models.Usergroup.name == body.name)):
        raise Conflict(f"{body.name} already exists")
    handle.add(models.Usergroup(name=body.name))
    audit.granted(caller.name, WRITE, f"usergroup/{body.name}", "created", handle=handle)
    return {"name": body.name, "members": {}}


@router.delete("/usergroups/{name}", summary="Delete a usergroup")
def delete_usergroup(name: str, handle: Session = Depends(scoped),
                     caller: Caller = Depends(guard(WRITE))) -> dict:
    """
    Its memberships and its group map rows go with it.
    """
    row = _usergroup(handle, name)
    handle.query(models.Membership).filter(
        models.Membership.usergroup_id == row.id).delete()
    handle.query(models.UsergroupMap).filter(
        models.UsergroupMap.usergroup_id == row.id).delete()
    handle.delete(row)
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"usergroup/{name}", "deleted", handle=handle)
    return {"deleted": name}


@router.post("/usergroups/{name}/members", status_code=201, summary="Add a member")
def add_member(name: str, body: MemberIn, handle: Session = Depends(scoped),
               caller: Caller = Depends(guard(WRITE))) -> dict:
    """
    One person, one usergroup, one role. Adding twice sets the role instead.
    """
    group = _usergroup(handle, name)
    who = _principal(handle, body.principal)
    role = _check_role(body.role)

    row = handle.scalar(select(models.Membership).where(
        models.Membership.principal_id == who.id,
        models.Membership.usergroup_id == group.id))
    if row is None:
        row = models.Membership(principal_id=who.id, usergroup_id=group.id, role=role)
        handle.add(row)
    else:
        row.role = role
        row.version += 1
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"usergroup/{name}",
                  f"{body.principal} is {role}", handle=handle)
    return {"usergroup": name, "principal": body.principal, "role": role}


@router.delete("/usergroups/{name}/members/{principal}", summary="Remove a member")
def remove_member(name: str, principal: str, handle: Session = Depends(scoped),
                  caller: Caller = Depends(guard(WRITE))) -> dict:
    """
    Take one principal out of one usergroup.
    """
    group = _usergroup(handle, name)
    who = _principal(handle, principal)
    removed = handle.query(models.Membership).filter(
        models.Membership.principal_id == who.id,
        models.Membership.usergroup_id == group.id).delete()
    if not removed:
        raise NotFound(f"{principal} is not a member of {name}")
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"usergroup/{name}", f"{principal} removed", handle=handle)
    return {"usergroup": name, "removed": principal}


@router.get("/usergroupmap", summary="How external groups map onto usergroups")
def list_map(handle: Session = Depends(scoped),
             caller: Caller = Depends(guard(READ))) -> dict:
    """
    One row per external group: two may feed one usergroup with different roles.
    """
    rows = handle.execute(
        select(models.UsergroupMap.source, models.UsergroupMap.external_group,
               models.Usergroup.name, models.UsergroupMap.role)
        .join(models.Usergroup,
              models.Usergroup.id == models.UsergroupMap.usergroup_id)).all()
    return {"usergroupmap": [{"source": source, "external_group": group,
                              "usergroup": name, "role": role}
                             for source, group, name, role in rows]}


@router.post("/usergroupmap", status_code=201, summary="Map an external group")
def create_map(body: MapIn, handle: Session = Depends(scoped),
               caller: Caller = Depends(guard(WRITE))) -> dict:
    """
    An unmapped external group grants nothing, which is why this row is the link.
    """
    if not body.source or not body.external_group:
        raise BadRequest("a map row needs a source and an external group")
    group = _usergroup(handle, body.usergroup)
    role = _check_role(body.role)

    row = handle.scalar(select(models.UsergroupMap).where(
        models.UsergroupMap.source == body.source,
        models.UsergroupMap.external_group == body.external_group))
    if row is None:
        row = models.UsergroupMap(source=body.source, external_group=body.external_group,
                                  usergroup_id=group.id, role=role)
        handle.add(row)
    else:
        row.usergroup_id = group.id
        row.role = role
        row.version += 1
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"usergroupmap/{body.external_group}",
                  f"{body.usergroup} as {role}", handle=handle)
    return {"source": body.source, "external_group": body.external_group,
            "usergroup": body.usergroup, "role": role}


@router.delete("/usergroupmap/{source}/{external_group:path}", summary="Unmap a group")
def delete_map(source: str, external_group: str, handle: Session = Depends(scoped),
               caller: Caller = Depends(guard(WRITE))) -> dict:
    """
    From then on that external group grants nothing, and that is logged at login.
    """
    removed = handle.query(models.UsergroupMap).filter(
        models.UsergroupMap.source == source,
        models.UsergroupMap.external_group == external_group).delete()
    if not removed:
        raise NotFound(f"{external_group} is not mapped for {source}")
    principals.CACHE.clear()
    audit.granted(caller.name, WRITE, f"usergroupmap/{external_group}", "removed", handle=handle)
    return {"source": source, "removed": external_group}
