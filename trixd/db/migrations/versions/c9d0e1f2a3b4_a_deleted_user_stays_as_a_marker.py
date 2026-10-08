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
A deleted user stays as a marker, so the deletion replicates to the peer.
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


revision = "c9d0e1f2a3b4"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Apply this revision.
    """
    op.add_column("principal", sa.Column("deleted", sa.Boolean(), server_default=sa.text("0"),
                                         nullable=False))


def downgrade() -> None:
    """
    Undo this revision.
    """
    # Without the column a marker would read as a live user again, so the markers go first.
    op.execute("DELETE FROM principal WHERE deleted = 1")
    # SQLite on Rocky 9 has no DROP COLUMN, so the table is rebuilt around it.
    with op.batch_alter_table("principal") as batch:
        batch.drop_column("deleted")
