"""Conversations.

Bounded episodes of interaction, derived from stored messages.

Three things worth stating where they are implemented:

* **Messages carry no ``conversation_id``.** Membership is the time range: a
  message belongs to the conversation whose ``[started_at, ended_at]`` contains
  its ``sent_at``. ``Message`` is append-only and its repository has no update
  path at all (ADR-046), so assigning a conversation to a stored message would
  be exactly the mutation that discipline forbids -- and conversations within a
  chat do not overlap, so the range already *is* the membership. Recorded as
  ADR-056.
* **``ended_at`` is not nullable, and there is no ``is_open``.**
  ``DOMAIN_MODEL.md`` version 1.0 made the first nullable to express the second.
  A conversation is derived from messages that already exist, so it always has a
  last one; and whether it may still grow depends on how long ago it ended --
  on *now* -- which no stored flag can keep true without a job to correct it.
* **The unique index is ``(account_id, chat_id, started_at)``**, not the partial
  one on ``is_open`` the diagram specified. Two conversations in one chat cannot
  begin at the same instant, which combined with each being a contiguous run is
  what makes "conversations do not overlap" structural.

``initiated_by`` and ``dominant_language`` from ``DOMAIN_MODEL.md`` section 5.7
are **not** created. The first is recomputable from the first message's sender
and nothing reads it until relationship metrics; the second needs language
detection, which this slice may not introduce.

**A requirement on whatever references this table next.** Summaries, plans and
analyses attach to a Conversation from Milestone 8, and this table's rows are
deleted whenever re-segmentation leaves one describing no messages. Those
foreign keys must cascade, or that delete has to start refusing.

Revision ID: 0009
Revises: 0008
Created: 2026-07-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "conversations"


def upgrade() -> None:
    """Create the conversations table."""
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.ForeignKeyConstraint(
            ["account_id", "chat_id"],
            ["chats.account_id", "chats.id"],
            name=op.f("fk_conversations_account_id_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("id > 0", name=op.f("ck_conversations_id_positive")),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_conversations_account_id_positive")),
        sa.CheckConstraint("chat_id > 0", name=op.f("ck_conversations_chat_id_positive")),
        sa.CheckConstraint(
            "ended_at >= started_at", name=op.f("ck_conversations_ends_after_it_begins")
        ),
        sa.CheckConstraint(
            "message_count > 0", name=op.f("ck_conversations_holds_at_least_one_message")
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_conversations_updated_after_created")
        ),
    )

    op.create_index(
        op.f("uq_conversations_account_id_chat_id_started_at"),
        TABLE,
        ["account_id", "chat_id", "started_at"],
        unique=True,
    )
    op.create_index(
        op.f("ix_conversations_account_id_chat_id_started_at"),
        TABLE,
        ["account_id", "chat_id", "started_at", "id"],
    )


def downgrade() -> None:
    """Drop the conversations table.

    Removes derived state, not user data. Every message stays exactly where it
    was; the next ``tgassist conversation rebuild`` recomputes the same
    boundaries from the same messages, because that is what segmentation being a
    pure function means.
    """
    op.drop_index(op.f("ix_conversations_account_id_chat_id_started_at"), table_name=TABLE)
    op.drop_index(op.f("uq_conversations_account_id_chat_id_started_at"), table_name=TABLE)
    op.drop_table(TABLE)
