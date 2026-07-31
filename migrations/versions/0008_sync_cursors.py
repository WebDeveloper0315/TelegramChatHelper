"""Sync cursors.

One row per chat, recording how far its history backfill has got.

Three things worth stating where they are implemented:

* **The chat identifier is the primary key.** ``DATABASE.md``'s diagram gave
  this table a surrogate ``id`` with a unique ``chat_id`` beside it. There is
  exactly one cursor per chat, so a surrogate would be a second name for one
  row -- the reasoning ADR-038 applied to ``user_profiles`` and migration
  ``0007`` applied to ``telegram_sessions``. Recorded as ADR-054.
* **The foreign key is composite.** ``(account_id, chat_id) -> chats``, not
  ``chat_id -> chats``, so a cursor for one account's chat cannot be attached to
  another's (ADR-043). It reuses the ``uq_chats_account_id_id`` index that
  migration ``0005`` created for exactly this.
* **Both ends of the range or neither.** A cursor naming a floor with no ceiling
  would describe a range whose extent nobody can state, so a check constraint
  refuses it rather than trusting the entity to be the only writer.

``consecutive_failures`` and ``last_error`` from ``DOMAIN_MODEL.md`` section
5.22 are **not** created. The first drives backoff and notifications, neither of
which exists; the second was dropped by ADR-050. Both are one additive migration
away, and neither records anything that could not be reconstructed later.

Revision ID: 0008
Revises: 0007
Created: 2026-07-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "sync_cursors"


def upgrade() -> None:
    """Create the sync_cursors table."""
    op.create_table(
        TABLE,
        sa.Column("chat_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("oldest_synced_message_id", sa.Integer(), nullable=True),
        sa.Column("newest_synced_message_id", sa.Integer(), nullable=True),
        sa.Column(
            "backfill_complete", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("backfill_horizon", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", name=op.f("pk_sync_cursors")),
        sa.ForeignKeyConstraint(
            ["account_id", "chat_id"],
            ["chats.account_id", "chats.id"],
            name=op.f("fk_sync_cursors_account_id_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("chat_id > 0", name=op.f("ck_sync_cursors_chat_id_positive")),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_sync_cursors_account_id_positive")),
        sa.CheckConstraint(
            "oldest_synced_message_id IS NULL OR oldest_synced_message_id > 0",
            name=op.f("ck_sync_cursors_oldest_synced_message_id_positive"),
        ),
        sa.CheckConstraint(
            "newest_synced_message_id IS NULL OR newest_synced_message_id > 0",
            name=op.f("ck_sync_cursors_newest_synced_message_id_positive"),
        ),
        sa.CheckConstraint(
            "(oldest_synced_message_id IS NULL) = (newest_synced_message_id IS NULL)",
            name=op.f("ck_sync_cursors_range_has_both_ends"),
        ),
        sa.CheckConstraint(
            "oldest_synced_message_id IS NULL "
            "OR oldest_synced_message_id <= newest_synced_message_id",
            name=op.f("ck_sync_cursors_oldest_not_after_newest"),
        ),
    )

    # No index beyond the primary key. The only query is by chat, which the key
    # serves. The scheduler's "which chats are pending" query arrives with the
    # scheduler, and its index should be chosen by it.


def downgrade() -> None:
    """Drop the sync_cursors table.

    Removes the bookmarks, not the messages. A downgraded database still holds
    everything that was synchronised; the next upgrade starts every chat's
    backfill from the newest message again, and re-reads what is already stored.
    That costs network traffic and nothing else, because every message write is
    idempotent (ADR-045).
    """
    op.drop_table(TABLE)
