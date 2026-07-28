"""Accounts.

The ownership root. Every account-owned table added later carries an
``account_id`` referencing this one, which is why it is the first business
migration: adding an ownership root after the tables it owns is a rewrite, not
an addition.

Revision ID: 0002
Revises: 0001
Created: 2026-07-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "accounts"


def upgrade() -> None:
    """Create the accounts table and its constraints."""
    op.create_table(
        TABLE,
        # Not autoincrement: identifiers come from the application's generator
        # so an account can be fully constructed and validated before it is
        # saved, rather than acquiring its identity as a side effect of insert.
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("timezone", sa.String(length=64), server_default="UTC", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        # The schema restates the entity's invariants so that a row written by
        # any route -- a future migration, a repair script, another process --
        # cannot violate what the domain guarantees in memory.
        sa.CheckConstraint("id > 0", name=op.f("ck_accounts_id_positive")),
        sa.CheckConstraint(
            "telegram_user_id > 0", name=op.f("ck_accounts_telegram_user_id_positive")
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0", name=op.f("ck_accounts_display_name_not_blank")
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_accounts_updated_after_created")
        ),
    )

    op.create_index(
        "uq_accounts_telegram_user_id",
        TABLE,
        ["telegram_user_id"],
        unique=True,
    )

    # The single-active invariant, enforced by the database rather than by every
    # caller remembering to deactivate first. A partial unique index permits
    # many inactive rows and at most one active one.
    op.create_index(
        "uq_accounts_single_active",
        TABLE,
        ["is_active"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    """Drop the accounts table.

    Destroys account records and, once later migrations exist, would be blocked
    by their foreign keys -- which is the intended protection. Reversible here
    because nothing yet references it.
    """
    op.drop_index("uq_accounts_single_active", table_name=TABLE)
    op.drop_index("uq_accounts_telegram_user_id", table_name=TABLE)
    op.drop_table(TABLE)
