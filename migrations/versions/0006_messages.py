"""Messages: the immutable factual record.

The table everything else is eventually derived from, and the first with no
``updated_at`` -- its absence is what says a message cannot change.

Three decisions are implemented here that are not obvious from the column list:

* ``telegram_message_id`` is **nullable**, and its unique index is **partial**.
  Ingestion accepts messages from any source and only Telegram issues
  identifiers; requiring one would make the pipeline Telegram-specific, and a
  non-partial index would permit only a single source-less message per chat
  (ADR-045).
* The foreign key to ``chats`` is **composite**, on ``(account_id, chat_id)``,
  so a message cannot be filed in another account's chat (ADR-043). It requires
  the unique index on ``chats (account_id, id)`` added below.
* There is no ``deleted_at``. Nothing deletes a message: retention is Milestone
  10, purge is Milestone 11, remote-deletion mirroring is Milestone 3. Adding
  the column now would put a filter nothing writes into every history query.

Revision ID: 0006
Revises: 0005
Created: 2026-07-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "messages"
CHATS = "chats"

CHATS_PAIR_INDEX = "uq_chats_account_id_id"


def upgrade() -> None:
    """Create the messages table, and the chats index its foreign key needs."""
    op.create_index(CHATS_PAIR_INDEX, CHATS, ["account_id", "id"], unique=True)

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("sender_kind", sa.String(length=16), nullable=False),
        sa.Column("message_type", sa.String(length=16), server_default="text", nullable=False),
        # Conversation content. Nullable because a photo or sticker has none.
        sa.Column("text", sa.Text(), nullable=True),
        # Distinct concepts, both required: a backfill ingests a message from
        # years ago today, and conflating them makes every timing analysis wrong
        # and every sync diagnostic useless.
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.ForeignKeyConstraint(
            ["account_id", "chat_id"],
            ["chats.account_id", "chats.id"],
            name=op.f("fk_messages_account_id_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("id > 0", name=op.f("ck_messages_id_positive")),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_messages_account_id_positive")),
        sa.CheckConstraint("chat_id > 0", name=op.f("ck_messages_chat_id_positive")),
        sa.CheckConstraint(
            "telegram_message_id IS NULL OR telegram_message_id > 0",
            name=op.f("ck_messages_telegram_message_id_positive"),
        ),
        sa.CheckConstraint(
            "sender_kind IN ('operator', 'contact', 'system')",
            name=op.f("ck_messages_sender_kind_known"),
        ),
        sa.CheckConstraint(
            "message_type IN ('text', 'photo', 'voice', 'video', 'document', "
            "'sticker', 'location', 'poll', 'service', 'other')",
            name=op.f("ck_messages_message_type_known"),
        ),
        sa.CheckConstraint(
            "message_type <> 'text' OR (text IS NOT NULL AND length(trim(text)) > 0)",
            name=op.f("ck_messages_text_present_for_text_messages"),
        ),
    )

    # The idempotency guarantee. Partial, so that many messages without an
    # external identifier remain permitted in one chat.
    op.create_index(
        "uq_messages_account_id_chat_id_telegram_message_id",
        TABLE,
        ["account_id", "chat_id", "telegram_message_id"],
        unique=True,
        sqlite_where=sa.text("telegram_message_id IS NOT NULL"),
        postgresql_where=sa.text("telegram_message_id IS NOT NULL"),
    )

    # The history query: one chat, ordered by sent_at with id as the keyset
    # tiebreaker.
    op.create_index(
        "ix_messages_account_id_chat_id_sent_at",
        TABLE,
        ["account_id", "chat_id", "sent_at", "id"],
    )


def downgrade() -> None:
    """Drop the messages table and the index added for its foreign key."""
    op.drop_index("ix_messages_account_id_chat_id_sent_at", table_name=TABLE)
    op.drop_index("uq_messages_account_id_chat_id_telegram_message_id", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_index(CHATS_PAIR_INDEX, table_name=CHATS)
