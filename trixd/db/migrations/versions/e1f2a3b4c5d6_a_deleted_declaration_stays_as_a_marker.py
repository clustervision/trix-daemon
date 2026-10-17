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
A deleted declaration stays as a marker, so the deletion replicates to the peer.

The sweep re-adds a row that is missing here and present at the peer, so a declaration that is
deleted comes back within the interval. It is marked instead, as a principal already is, and a
marked row counts as no row on both controllers.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, ClusterVision Solutions b.v."
__license__     = "GPL"
__version__     = "2.2"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Development"

import sqlalchemy as sa
from alembic import op


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Add the marker, false for every row that already exists.
    """
    op.add_column("declaration",
                  sa.Column("deleted", sa.Boolean(), nullable=False,
                            server_default=sa.text("0")))


def downgrade() -> None:
    """
    Undo this revision.
    """
    # Without the column a marker would read as a live document again, so the markers go first.
    op.execute("DELETE FROM declaration WHERE deleted = 1")
    # SQLite on Rocky 9 has no DROP COLUMN, so the table is rebuilt around it.
    with op.batch_alter_table("declaration") as batch:
        batch.drop_column("deleted")
