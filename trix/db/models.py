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
The declarative models. Replicated tables carry a version, a timestamp and an origin.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from trix.common.constants import DEFAULT_MODE, DEFAULT_ROLE, ROLE_NONE


def now() -> datetime:
    """
    Timestamps are stored in UTC, because two controllers compare them.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """
    Declarative base for every model.
    """


class Replicated:
    """
    Columns the anti-entropy sweep needs to merge two copies of a document.
    """

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    origin: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now,
                                                 nullable=False)


class Declaration(Base, Replicated):
    """
    A plugin document: rule settings, a service list, a partition, a tuning knob.
    """

    __tablename__ = "declaration"
    __table_args__ = (UniqueConstraint("plugin", "kind", "name", name="uq_declaration"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    body: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Who owns it, which organisations it is listed for, and the mode. Empty owners
    # means the ini account, exactly as an empty list does in luna2-daemon.
    owners: Mapped[str] = mapped_column(Text, default="", nullable=False)
    usergroups: Mapped[str] = mapped_column(Text, default="", nullable=False)
    access: Mapped[str] = mapped_column(String(4), default=DEFAULT_MODE, nullable=False)


class Principal(Base, Replicated):
    """
    Someone or something the daemon can name: an administrator, a service, a node.
    """

    __tablename__ = "principal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="local", nullable=False)
    password_digest: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Where this principal sits on the ladder. rootus is the ini name, not a role.
    role: Mapped[str] = mapped_column(String(16), default=ROLE_NONE, nullable=False)
    # The cluster-wide bypass, named in the audit trail, and the flag that lets a
    # portal service ask for a token on behalf of the person it has authenticated.
    admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    delegate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Usergroup(Base, Replicated):
    """
    A named set of principals.
    """

    __tablename__ = "usergroup"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


class Membership(Base, Replicated):
    """
    Which principal is in which usergroup.
    """

    __tablename__ = "membership"
    __table_args__ = (UniqueConstraint("principal_id", "usergroup_id", name="uq_membership"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principal.id"), nullable=False)
    usergroup_id: Mapped[int] = mapped_column(ForeignKey("usergroup.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=DEFAULT_ROLE, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="local", nullable=False)


class UsergroupMap(Base, Replicated):
    """
    One external group, from one source, onto a usergroup and a role in it.
    """

    __tablename__ = "usergroupmap"
    __table_args__ = (UniqueConstraint("source", "external_group", name="uq_usergroupmap"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_group: Mapped[str] = mapped_column(String(255), nullable=False)
    usergroup_id: Mapped[int] = mapped_column(ForeignKey("usergroup.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=DEFAULT_ROLE, nullable=False)


class Audit(Base):
    """
    What happened here. Local: it is a statement about this controller.
    """

    __tablename__ = "audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    principal: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    action: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    object: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Task(Base):
    """
    One queued action and where it got to. Local: it ran on this controller.
    """

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    principal: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    subsystem: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    task: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    param: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    finished: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class RenderState(Base):
    """
    When a render last ran here, and whether it worked. Local.
    """

    __tablename__ = "render_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    render: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    inputs_digest: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    rendered_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)


class PluginState(Base):
    """
    Whether a plugin is meant to be loaded here. Local, so a restart agrees.
    """

    __tablename__ = "plugin_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="loaded", nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)


REPLICATED: tuple = ("declaration", "principal", "usergroup", "membership",
                     "usergroupmap")
LOCAL: tuple = ("audit", "render_state", "plugin_state", "task")
