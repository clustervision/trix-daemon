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
The outbox: what this controller still owes a peer, for a write no replicated table and
no render carries.
"""

__author__      = "Antoine Schonewille"
__copyright__   = "Copyright 2026, TrinityX [TRIX Daemon]"
__license__     = "GPL"
__version__     = "1.0"
__maintainer__  = "Antoine Schonewille"
__email__       = "antoine.schonewille@clustervision.com"
__status__      = "Production"

import sqlalchemy as sa
from alembic import op


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Apply this revision.
    """
    op.create_table(
        "outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(64), server_default="", nullable=False),
        sa.Column("peer", sa.String(255), server_default="", nullable=False),
        sa.Column("plugin", sa.String(64), server_default="", nullable=False),
        sa.Column("path", sa.String(255), server_default="", nullable=False),
        sa.Column("method", sa.String(16), server_default="", nullable=False),
        sa.Column("target", sa.String(255), server_default="", nullable=False),
        sa.Column("payload", sa.Text(), server_default="", nullable=False),
        sa.Column("origin", sa.String(255), server_default="", nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("state", sa.String(16), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("finished", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("peer", "plugin", "path", "method", "target",
                            name="uq_outbox_target"),
    )


def downgrade() -> None:
    """
    Undo this revision.
    """
    op.drop_table("outbox")
