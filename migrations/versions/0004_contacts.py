"""Contacts.

The first account-scoped table holding *many* rows per account, and therefore
the first with indexes chosen to serve queries rather than only to enforce
constraints.

Three design points worth stating where they are implemented:

* The primary key is locally generated, not the Telegram identifier. The same
  person can be known to two accounts, so ``telegram_user_id`` is not unique in
  this table -- only the pair is (ADR-041).
* The unique index covers soft-deleted rows. A deleted contact still occupies
  its natural key, so re-adding the same person is refused and the caller is
  told to restore instead. An index excluding deleted rows would let the same
  person exist twice, with their history attached to whichever row happened to
  be current.
* Archived and deleted are mutually exclusive, enforced by a check constraint
  rather than by convention.

Revision ID: 0004
Revises: 0003
Created: 2026-07-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "contacts"

MIN_USERNAME_LENGTH = 5
MAX_USERNAME_LENGTH = 32


def upgrade() -> None:
    """Create the contacts table with its constraints and indexes."""
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        # The one genuinely optional column: many Telegram users have never set
        # a username. Null means "has none", not "not decided yet".
        sa.Column("username", sa.String(length=MAX_USERNAME_LENGTH), nullable=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        # Timestamps rather than booleans: retention asks "deleted before when",
        # and a boolean cannot answer that.
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contacts")),
        # ON DELETE CASCADE, so removing an account leaves no orphaned contacts.
        # Enforcement depends on PRAGMA foreign_keys=ON, which the engine applies
        # to every connection and the health check verifies.
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_contacts_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("id > 0", name=op.f("ck_contacts_id_positive")),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_contacts_account_id_positive")),
        sa.CheckConstraint(
            "telegram_user_id > 0", name=op.f("ck_contacts_telegram_user_id_positive")
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0", name=op.f("ck_contacts_display_name_not_blank")
        ),
        sa.CheckConstraint(
            f"username IS NULL OR length(username) BETWEEN "
            f"{MIN_USERNAME_LENGTH} AND {MAX_USERNAME_LENGTH}",
            name=op.f("ck_contacts_username_length"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_contacts_updated_after_created")
        ),
        sa.CheckConstraint(
            "archived_at IS NULL OR deleted_at IS NULL",
            name=op.f("ck_contacts_not_archived_and_deleted"),
        ),
    )

    # The documented invariant, made structural.
    op.create_index(
        "uq_contacts_account_id_telegram_user_id",
        TABLE,
        ["account_id", "telegram_user_id"],
        unique=True,
    )

    # The listing query: scoped by account, ordered by created_at with id as the
    # keyset tiebreaker. account_id leads because every query this table serves
    # is account-scoped, so an index that does not lead with it serves nothing.
    op.create_index(
        "ix_contacts_account_id_created_at",
        TABLE,
        ["account_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Drop the contacts table and its indexes."""
    op.drop_index("ix_contacts_account_id_created_at", table_name=TABLE)
    op.drop_index("uq_contacts_account_id_telegram_user_id", table_name=TABLE)
    op.drop_table(TABLE)
