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
A user is a name, a password and a role: kind, source and external_id go.
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


revision = "b7c8d9e0f1a2"
down_revision = "a6b7c8d9e0f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Apply this revision.
    """
    # SQLite on Rocky 9 has no DROP COLUMN, so the table is rebuilt around it.
    with op.batch_alter_table("principal") as batch:
        batch.drop_column("kind")
        batch.drop_column("source")
        batch.drop_column("external_id")


def downgrade() -> None:
    """
    Undo this revision.
    """
    op.add_column("principal", sa.Column("kind", sa.String(16), server_default="user",
                                         nullable=False))
    op.add_column("principal", sa.Column("source", sa.String(32), server_default="local",
                                         nullable=False))
    op.add_column("principal", sa.Column("external_id", sa.String(255), server_default="",
                                         nullable=False))
