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
Drop the usergroup, membership and object permission tables the ladder replaced.
"""

__author__      = "Sumit Sharma"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Sumit Sharma"
__email__       = "sumit.sharma@clustervision.com"
__status__      = "Production"

import sqlalchemy as sa
from alembic import op


revision = "a6b7c8d9e0f1"
down_revision = "f5b6c7d8e9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Apply this revision.
    """
    op.drop_table("usergroupmap")
    op.drop_table("membership")
    op.drop_table("usergroup")
    # SQLite on Rocky 9 has no DROP COLUMN, so each table is rebuilt around it.
    with op.batch_alter_table("principal") as batch:
        batch.drop_column("admin")
        batch.drop_column("delegate")
    with op.batch_alter_table("declaration") as batch:
        batch.drop_column("owners")
        batch.drop_column("usergroups")
        batch.drop_column("access")


def downgrade() -> None:
    """
    Undo this revision.
    """
    op.add_column("declaration", sa.Column("owners", sa.Text(), server_default="",
                                           nullable=False))
    op.add_column("declaration", sa.Column("usergroups", sa.Text(), server_default="",
                                           nullable=False))
    op.add_column("declaration", sa.Column("access", sa.String(4), server_default="770",
                                           nullable=False))
    op.add_column("principal", sa.Column("admin", sa.Boolean(), server_default=sa.false(),
                                         nullable=False))
    op.add_column("principal", sa.Column("delegate", sa.Boolean(), server_default=sa.false(),
                                         nullable=False))
    op.create_table(
        "usergroup",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("origin", sa.String(128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "membership",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("principal_id", sa.Integer(), sa.ForeignKey("principal.id"), nullable=False),
        sa.Column("usergroup_id", sa.Integer(), sa.ForeignKey("usergroup.id"), nullable=False),
        sa.Column("role", sa.String(16), server_default="reader", nullable=False),
        sa.Column("source", sa.String(32), server_default="local", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("origin", sa.String(128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("principal_id", "usergroup_id", name="uq_membership"),
    )
    op.create_table(
        "usergroupmap",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_group", sa.String(255), nullable=False),
        sa.Column("usergroup_id", sa.Integer(), sa.ForeignKey("usergroup.id"), nullable=False),
        sa.Column("role", sa.String(16), server_default="reader", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("origin", sa.String(128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source", "external_group", name="uq_usergroupmap"),
    )
