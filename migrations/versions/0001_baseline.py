"""Baseline schema.

Creates the infrastructure metadata table and establishes the migration
sequence. Business tables begin at revision 0002 (see ``docs/DATABASE.md``
section 7).

This revision deliberately creates something rather than being an empty
placeholder: a migration sequence whose first step is a no-op cannot be tested
in either direction, so the machinery would go unverified until the first real
schema change -- exactly when a fault in it is most expensive.

Revision ID: 0001
Revises:
Created: 2026-07-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "schema_metadata"


def upgrade() -> None:
    """Create the infrastructure metadata table."""
    op.create_table(
        TABLE,
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_schema_metadata")),
    )


def downgrade() -> None:
    """Drop the infrastructure metadata table.

    Reversible without data loss concerns: the table holds provenance about the
    database, all of which is reconstructible.
    """
    op.drop_table(TABLE)
