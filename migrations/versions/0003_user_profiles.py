"""User profiles.

The first table owned by another. It establishes the pattern every later
account-scoped table follows: the account identifier as a foreign key, cascade
deletion so a removed account leaves nothing behind, and no nullable preference
columns.

Every column is ``NOT NULL`` with a default. A nullable preference forces every
reader to decide what null means, and they will not all decide the same thing --
one treats it as "unset, use the default", another as "explicitly none". Giving
each preference a real default makes that ambiguity unrepresentable.

Revision ID: 0003
Revises: 0002
Created: 2026-07-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "user_profiles"

DEFAULT_QUIET_START = 22 * 60
DEFAULT_QUIET_END = 8 * 60


def upgrade() -> None:
    """Create the user_profiles table."""
    op.create_table(
        TABLE,
        # The account identifier is both the primary key and the foreign key.
        # Exactly one profile exists per account, so this makes the invariant
        # the key itself rather than a separate unique index (ADR-038).
        sa.Column("account_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("primary_language", sa.String(length=32), server_default="en", nullable=False),
        sa.Column(
            "tone_preference", sa.String(length=16), server_default="neutral", nullable=False
        ),
        sa.Column(
            "preferred_message_length",
            sa.String(length=16),
            server_default="medium",
            nullable=False,
        ),
        sa.Column("emoji_usage", sa.String(length=16), server_default="sparing", nullable=False),
        sa.Column(
            "quiet_hours_start_minute",
            sa.Integer(),
            server_default=sa.text(str(DEFAULT_QUIET_START)),
            nullable=False,
        ),
        sa.Column(
            "quiet_hours_end_minute",
            sa.Integer(),
            server_default=sa.text(str(DEFAULT_QUIET_END)),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_user_profiles")),
        # ON DELETE CASCADE, so removing an account leaves no orphaned profile.
        # Enforcement depends on PRAGMA foreign_keys=ON, which the engine applies
        # to every connection and the health check verifies -- SQLite silently
        # ignores foreign keys otherwise, which would make this decorative.
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_user_profiles_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "tone_preference IN ('casual', 'neutral', 'formal', 'mirror_contact')",
            name=op.f("ck_user_profiles_tone_preference_known"),
        ),
        sa.CheckConstraint(
            "preferred_message_length IN ('short', 'medium', 'long')",
            name=op.f("ck_user_profiles_message_length_known"),
        ),
        sa.CheckConstraint(
            "emoji_usage IN ('none', 'sparing', 'frequent')",
            name=op.f("ck_user_profiles_emoji_usage_known"),
        ),
        sa.CheckConstraint(
            "quiet_hours_start_minute BETWEEN 0 AND 1439",
            name=op.f("ck_user_profiles_quiet_start_within_day"),
        ),
        sa.CheckConstraint(
            "quiet_hours_end_minute BETWEEN 0 AND 1439",
            name=op.f("ck_user_profiles_quiet_end_within_day"),
        ),
        # Equal bounds are ambiguous between an empty range and the whole day,
        # and the documented invariant forbids covering the whole day.
        sa.CheckConstraint(
            "quiet_hours_start_minute <> quiet_hours_end_minute",
            name=op.f("ck_user_profiles_quiet_hours_not_whole_day"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_user_profiles_updated_after_created")
        ),
    )

    # No index on account_id: it is the primary key, which SQLite and PostgreSQL
    # already index. A second index on the same column would cost writes and
    # buy nothing.


def downgrade() -> None:
    """Drop the user_profiles table."""
    op.drop_table(TABLE)
