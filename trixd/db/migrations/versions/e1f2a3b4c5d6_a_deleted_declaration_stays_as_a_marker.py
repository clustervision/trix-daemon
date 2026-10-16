"""a deleted declaration stays as a marker

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5

The sweep re-adds a row that is missing here and present at the peer, so a declaration that is
deleted comes back within the interval. It is marked instead, as a principal already is, and a
marked row counts as no row on both controllers.
"""
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
