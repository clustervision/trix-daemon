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
Role on membership, the external group map, the identity flags and POSIX bits.
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

revision = "b7c1d2e3f4a5"
down_revision = "a63dcf3dc98e"
branch_labels = None
depends_on = None

DEFAULT_ROLE: str = "reader"


def upgrade() -> None:
    """
    Apply this revision.
    """
    op.add_column("principal", sa.Column("external_id", sa.String(255),
                                         server_default="", nullable=False))
    op.add_column("principal", sa.Column("admin", sa.Boolean(),
                                         server_default=sa.false(), nullable=False))
    op.add_column("principal", sa.Column("delegate", sa.Boolean(),
                                         server_default=sa.false(), nullable=False))

    op.add_column("membership", sa.Column("role", sa.String(16),
                                          server_default=DEFAULT_ROLE, nullable=False))
    op.add_column("membership", sa.Column("source", sa.String(32),
                                          server_default="local", nullable=False))

    op.create_table(
        "usergroupmap",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_group", sa.String(255), nullable=False),
        sa.Column("usergroup_id", sa.Integer(), sa.ForeignKey("usergroup.id"),
                  nullable=False),
        sa.Column("role", sa.String(16), server_default=DEFAULT_ROLE, nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("origin", sa.String(128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("source", "external_group", name="uq_usergroupmap"),
    )

    # The third bit is x, not u: POSIX, and the letter luna2-daemon already uses.
    for column in ("owner_bits", "group_bits", "other_bits"):
        op.execute(f"UPDATE permission SET {column} = REPLACE({column}, 'u', 'x')")


def downgrade() -> None:
    """
    Undo this revision.
    """
    op.execute("UPDATE permission SET owner_bits = REPLACE(owner_bits, 'x', 'u')")
    op.execute("UPDATE permission SET group_bits = REPLACE(group_bits, 'x', 'u')")
    op.execute("UPDATE permission SET other_bits = REPLACE(other_bits, 'x', 'u')")

    op.drop_table("usergroupmap")

    # SQLite 3.34 on Rocky 9 has no DROP COLUMN, so batch mode rebuilds the table.
    with op.batch_alter_table("membership") as batch:
        batch.drop_column("source")
        batch.drop_column("role")
    with op.batch_alter_table("principal") as batch:
        batch.drop_column("delegate")
        batch.drop_column("admin")
        batch.drop_column("external_id")
