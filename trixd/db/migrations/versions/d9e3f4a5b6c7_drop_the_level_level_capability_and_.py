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
Drop the level, level capability and grant tables the bit check replaced.
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

revision = "d9e3f4a5b6c7"
down_revision = "c8d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Apply this revision.
    """
    op.drop_table("grant")
    op.drop_table("level_capability")
    op.drop_table("level")


def downgrade() -> None:
    """
    Undo this revision.
    """
    op.create_table(
        "level",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), unique=True, nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("origin", sa.String(128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_table(
        "level_capability",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("level_id", sa.Integer(), sa.ForeignKey("level.id"), nullable=False),
        sa.Column("capability", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("origin", sa.String(128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("level_id", "capability", name="uq_level_capability"),
    )
    op.create_table(
        "grant",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("principal_id", sa.Integer(), sa.ForeignKey("principal.id"),
                  nullable=True),
        sa.Column("usergroup_id", sa.Integer(), sa.ForeignKey("usergroup.id"),
                  nullable=True),
        sa.Column("level_id", sa.Integer(), sa.ForeignKey("level.id"), nullable=False),
        sa.Column("scope", sa.String(255), server_default="", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("origin", sa.String(128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
