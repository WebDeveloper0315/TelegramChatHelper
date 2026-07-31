"""Suggestions: things a model proposed, awaiting a person's decision.

The review queue. Slice 9e produced suggestions that existed for the length of
one command and then vanished; there was nothing to review because there was
nothing to come back to. **Accepting a row in this table records agreement and
executes nothing** — there is no executor, no scheduler, and no code path from
here to Telegram (ADR-062).

Every column, and why it is here:

* ``id`` — assigned when the suggestion is *stored*, which is what makes it
  reviewable later.
* ``account_id`` — whose it is. Cascades with the account.
* ``chat_id`` — what it is about. **NOT NULL**: a suggestion with no chat could
  not be reviewed in context, and every kind foreseen is about a conversation
  with somebody. Part of a **composite** foreign key so a suggestion cannot be
  about another account's chat (ADR-043).
* ``conversation_id`` — the bounded episode it was drawn from, when one is
  known. **NULL today, and honestly so**: generation reads a chat's recent
  messages rather than a segmented conversation (Slice 9e), so nothing supplies
  this yet. The column is created because the field was specified; it has no
  writer, which is stated here rather than discovered later.
* ``ai_call_id`` — the call that produced it, and the whole of its provenance.
  Through it a suggestion leads back to the model, the prompt version, the token
  cost and the moment (ADR-057).
* ``proposal_type`` — what kind of thing is suggested, check-constrained to a
  closed set. A reviewer decides about a title and a description whatever this
  says; only an executor needs to know the kind. `reply_draft` is the only value
  today.
* ``title`` — one line, for a listing.
* ``description`` — what a **person** reads to decide. For a reply draft, the
  draft itself. Deliberately type-agnostic: a reviewer must be able to decide
  about any kind of suggestion without any code understanding that kind.
* ``payload`` — what a **machine** would need, as JSON. Read by nothing today.
  Separate from the description so that a new proposal type needs new payload
  handling and **no new review**.
* ``status`` — `pending`, `accepted` or `dismissed`. No other states:
  `superseded` and `expired` are transitions, and this aggregate has exactly
  one.
* ``created_at`` / ``decided_at`` — the second moves with the status and cannot
  disagree with it. **No ``updated_at``**: nothing edits a suggestion, because
  an edited draft is not what the model suggested, and the record exists to say
  what it suggested when somebody agreed.

**Everything cascades from the chat, and the contrast with `memories` is
deliberate.** A memory is approved knowledge that outlives the exchange it came
from, so its provenance is `SET NULL` (migration `0012`). A suggestion is a
draft *about* a conversation and means nothing once that conversation is gone,
so it goes with it.

**Two indexes, each chosen from a repository query:**

* ``ix_suggestions_account_id_pending`` — `list_pending`, **partial** on
  `status = 'pending'`. A queue is by definition what has not been decided, and
  an index carrying every decided row would grow without bound while serving
  nothing.
* ``ix_suggestions_account_id_chat_id_created_at`` — `list_by_chat`, not
  partial: reviewing a conversation's history means seeing what was dismissed
  as well as what was kept.

Revision ID: 0014
Revises: 0013
Created: 2026-07-31

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "suggestions"
PENDING_INDEX = "ix_suggestions_account_id_pending"
CHAT_INDEX = "ix_suggestions_account_id_chat_id_created_at"


def upgrade() -> None:
    """Create the suggestions table and the two indexes its queries need."""
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("ai_call_id", sa.Integer(), nullable=False),
        sa.Column("proposal_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suggestions")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_suggestions_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "chat_id"],
            ["chats.account_id", "chats.id"],
            name=op.f("fk_suggestions_account_id_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "conversation_id"],
            ["conversations.account_id", "conversations.id"],
            name=op.f("fk_suggestions_account_id_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "ai_call_id"],
            ["ai_calls.account_id", "ai_calls.id"],
            name=op.f("fk_suggestions_account_id_ai_call_id_ai_calls"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("id > 0", name=op.f("ck_suggestions_id_positive")),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_suggestions_account_id_positive")),
        sa.CheckConstraint("chat_id > 0", name=op.f("ck_suggestions_chat_id_positive")),
        sa.CheckConstraint(
            "conversation_id IS NULL OR conversation_id > 0",
            name=op.f("ck_suggestions_conversation_id_positive"),
        ),
        sa.CheckConstraint(
            "ai_call_id > 0", name=op.f("ck_suggestions_ai_call_id_positive")
        ),
        sa.CheckConstraint(
            "proposal_type IN ('reply_draft')", name=op.f("ck_suggestions_proposal_type_known")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'dismissed')",
            name=op.f("ck_suggestions_status_known"),
        ),
        sa.CheckConstraint(
            "length(trim(title)) > 0", name=op.f("ck_suggestions_title_not_blank")
        ),
        sa.CheckConstraint(
            "length(trim(description)) > 0", name=op.f("ck_suggestions_description_not_blank")
        ),
        sa.CheckConstraint(
            "length(trim(payload)) > 0", name=op.f("ck_suggestions_payload_not_blank")
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)",
            name=op.f("ck_suggestions_decision_has_a_time"),
        ),
        sa.CheckConstraint(
            "decided_at IS NULL OR decided_at >= created_at",
            name=op.f("ck_suggestions_decided_after_created"),
        ),
    )

    op.create_index(
        op.f(PENDING_INDEX),
        TABLE,
        ["account_id", "created_at", "id"],
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        op.f(CHAT_INDEX),
        TABLE,
        ["account_id", "chat_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Drop the suggestions table.

    Discards drafts and the decisions made about them. Neither can be
    recomputed: re-running generation would produce new drafts at the cost of
    new model calls, and the decisions were a person's. No message, memory,
    conversation or AI call is touched.
    """
    op.drop_index(op.f(CHAT_INDEX), table_name=TABLE)
    op.drop_index(op.f(PENDING_INDEX), table_name=TABLE)
    op.drop_table(TABLE)
