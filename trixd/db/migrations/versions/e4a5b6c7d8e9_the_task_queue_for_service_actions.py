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
The task queue, so a service action is answered before the unit has settled.
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


revision = "e4a5b6c7d8e9"
down_revision = "d9e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Apply this revision.
    """
    op.create_table(
        "task",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(64), unique=True, nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("principal", sa.String(255), server_default="", nullable=False),
        sa.Column("subsystem", sa.String(64), server_default="", nullable=False),
        sa.Column("task", sa.String(64), server_default="", nullable=False),
        sa.Column("param", sa.String(255), server_default="", nullable=False),
        sa.Column("state", sa.String(16), server_default="queued", nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("finished", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """
    Undo this revision.
    """
    op.drop_table("task")
