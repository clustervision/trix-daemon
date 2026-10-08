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
Owners, usergroups and access onto the declaration; the permission table goes.
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

revision = "c8d2e3f4a5b6"
down_revision = "b7c1d2e3f4a5"
branch_labels = None
depends_on = None

DEFAULT_MODE: str = "770"


def upgrade() -> None:
    """
    Apply this revision.
    """
    op.add_column("declaration", sa.Column("owners", sa.Text(),
                                           server_default="", nullable=False))
    op.add_column("declaration", sa.Column("usergroups", sa.Text(),
                                           server_default="", nullable=False))
    op.add_column("declaration", sa.Column("access", sa.String(4),
                                           server_default=DEFAULT_MODE, nullable=False))

    # Nothing ever read this table. The three columns above are where the agreed
    # design puts permissions: on the object, so deleting it takes them with it.
    op.drop_table("permission")


def downgrade() -> None:
    """
    Undo this revision.
    """
    op.create_table(
        "permission",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("object_kind", sa.String(64), nullable=False),
        sa.Column("object_name", sa.String(255), nullable=False),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("principal.id"), nullable=True),
        sa.Column("usergroup_id", sa.Integer(), sa.ForeignKey("usergroup.id"),
                  nullable=True),
        sa.Column("owner_bits", sa.String(3), server_default="rwx", nullable=False),
        sa.Column("group_bits", sa.String(3), server_default="r", nullable=False),
        sa.Column("other_bits", sa.String(3), server_default="", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("origin", sa.String(128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("object_kind", "object_name", name="uq_permission"),
    )

    # SQLite 3.34 on Rocky 9 has no DROP COLUMN, so batch mode rebuilds the table.
    with op.batch_alter_table("declaration") as batch:
        batch.drop_column("access")
        batch.drop_column("usergroups")
        batch.drop_column("owners")
