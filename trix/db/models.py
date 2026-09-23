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


def now() -> datetime:
    """Timestamps are stored in UTC, because two controllers compare them."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for every model."""


class Replicated:
    """Columns the anti-entropy sweep needs to merge two copies of a document."""

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    origin: Mapped[str] = mapped_column(String(128), default='', nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now,
                                                 nullable=False)


class Declaration(Base, Replicated):
    """A plugin document: rule settings, a service list, a partition, a tuning knob."""

    __tablename__ = 'declaration'
    __table_args__ = (UniqueConstraint('plugin', 'kind', 'name', name='uq_declaration'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plugin: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    body: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Principal(Base, Replicated):
    """Someone or something the daemon can name: an administrator, a service, a node."""

    __tablename__ = 'principal'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default='user', nullable=False)
    source: Mapped[str] = mapped_column(String(32), default='local', nullable=False)
    password_digest: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Usergroup(Base, Replicated):
    """A named set of principals."""

    __tablename__ = 'usergroup'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)


class Membership(Base, Replicated):
    """Which principal is in which usergroup."""

    __tablename__ = 'membership'
    __table_args__ = (UniqueConstraint('principal_id', 'usergroup_id', name='uq_membership'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey('principal.id'), nullable=False)
    usergroup_id: Mapped[int] = mapped_column(ForeignKey('usergroup.id'), nullable=False)


class Permission(Base, Replicated):
    """Owner, usergroup and other bits on one object a plugin owns."""

    __tablename__ = 'permission'
    __table_args__ = (UniqueConstraint('object_kind', 'object_name', name='uq_permission'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    object_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    object_name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_id: Mapped[int] = mapped_column(ForeignKey('principal.id'), nullable=True)
    usergroup_id: Mapped[int] = mapped_column(ForeignKey('usergroup.id'), nullable=True)
    owner_bits: Mapped[str] = mapped_column(String(3), default='rwu', nullable=False)
    group_bits: Mapped[str] = mapped_column(String(3), default='r', nullable=False)
    other_bits: Mapped[str] = mapped_column(String(3), default='', nullable=False)


class Level(Base, Replicated):
    """A named bundle of capabilities."""

    __tablename__ = 'level'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)


class LevelCapability(Base, Replicated):
    """One capability string inside a level."""

    __tablename__ = 'level_capability'
    __table_args__ = (UniqueConstraint('level_id', 'capability', name='uq_level_capability'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level_id: Mapped[int] = mapped_column(ForeignKey('level.id'), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)


class Grant(Base, Replicated):
    """A level given to a principal or a usergroup, optionally at a scope."""

    __tablename__ = 'grant'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey('principal.id'), nullable=True)
    usergroup_id: Mapped[int] = mapped_column(ForeignKey('usergroup.id'), nullable=True)
    level_id: Mapped[int] = mapped_column(ForeignKey('level.id'), nullable=False)
    scope: Mapped[str] = mapped_column(String(255), default='', nullable=False)


class Audit(Base):
    """What happened here. Local: it is a statement about this controller."""

    __tablename__ = 'audit'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    principal: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    action: Mapped[str] = mapped_column(String(128), default='', nullable=False)
    object: Mapped[str] = mapped_column(String(255), default='', nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), default='', nullable=False)
    detail: Mapped[str] = mapped_column(Text, default='', nullable=False)


class RenderState(Base):
    """When a render last ran here, and whether it worked. Local."""

    __tablename__ = 'render_state'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    render: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    inputs_digest: Mapped[str] = mapped_column(String(64), default='', nullable=False)
    rendered_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default='', nullable=False)


class PluginState(Base):
    """Whether a plugin is meant to be loaded here. Local, so a restart agrees."""

    __tablename__ = 'plugin_state'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), default='', nullable=False)
    state: Mapped[str] = mapped_column(String(16), default='loaded', nullable=False)
    reason: Mapped[str] = mapped_column(Text, default='', nullable=False)


REPLICATED: tuple = ('declaration', 'principal', 'usergroup', 'membership',
                     'permission', 'level', 'level_capability', 'grant')
LOCAL: tuple = ('audit', 'render_state', 'plugin_state')
