"""Memory proposals.

One row per candidate fact a model extracted and a person has not yet decided
about. Nothing here is believed: a proposal becomes a Memory only when somebody
accepts it, and this milestone has no way to accept one (ADR-019, ADR-058).

Every column, and why it is here:

* ``id`` -- assigned by the application, never by the model. A model able to
  name an identifier could name one already in use, and so overwrite a proposal
  a person had already read.
* ``account_id`` -- whose proposal it is. Cascades with the account.
* ``conversation_id`` -- what it was extracted from. A conversation rather than
  a message, because a fact is often assembled from several messages and naming
  one of them would be a guess about which. Part of a **composite** foreign key
  so a proposal in one account cannot cite another account's conversation
  (ADR-043), and cascading: a claim about a conversation that no longer exists
  is residue of it.
* ``ai_call_id`` -- the recorded call that produced it, and the whole of its
  provenance. Through it a proposal leads back to the model, the prompt version,
  the token cost and the moment it happened (ADR-057). Composite and cascading
  for the same reasons: a proposal whose origin had been deleted would be a
  fact with no visible source, which is the state proposals exist to prevent.
* ``category`` -- which kind of fact, from the closed set in `DOMAIN_MODEL.md`
  §5.9. Constrained rather than free text, because a category is what a user
  filters, sorts and eventually auto-approves by, and a free-text one would make
  each of those a comparison against whatever the model wrote that day.
* ``value`` -- the fact itself, in the model's words. Bounded at 500 characters
  because it goes into later prompts, where length is a budget everything else
  competes for.
* ``confidence`` -- what the model said about its own certainty. **REAL, not
  text** -- unlike a cost. Nothing sums confidences; the only operation is a
  comparison against a threshold, and the last bit of a float cannot change the
  answer to that in any way that matters. Constrained to ``[0, 1]``, because a
  value outside it is not a low confidence but a model that did not answer the
  question asked.
* ``status`` -- where it stands. Only ``pending`` is ever written today: there
  is no transition in this milestone, which is what makes ``accepted`` and
  ``rejected`` terminal in the strongest available sense. Both are named in the
  check constraint because Slice 9c writes them.
* ``evidence`` -- the text the fact was read from, verbatim. **NOT NULL**: a
  proposal without evidence is a claim with no source, and the only way to check
  an extraction without re-running it is to read what it was based on
  (`PROMPTS.md` §9.4). The application additionally verifies that this text
  actually appears in the conversation before storing the row -- a model cannot
  quote what nobody said.
* ``prompt_id`` / ``prompt_version`` -- which prompt at which revision produced
  it. Duplicated from ``ai_calls`` deliberately: "which proposals came from the
  prompt we changed last week" is a question asked of *this* table, and joining
  through an audit table to answer it would make the audit table load bearing
  for a routine query.
* ``created_at`` -- when it was extracted.

**No ``updated_at`` and no ``decided_at``.** Nothing changes a proposal in this
milestone. Slice 9c adds the single transition and the timestamp it needs; a
column written by nobody is a column kept correct by nobody.

**No ``key``, ``contact_id``, ``conflicts_with_memory_id`` or
``rejection_reason``**, all of which `DOMAIN_MODEL.md` §5.10 names. Each belongs
to a capability that does not exist yet: supersession, memories, conflict
detection, and deciding.

**A unique index on ``(account_id, conversation_id, category, value)``.** Re-
running extraction over a conversation the model has already seen must cost
nothing and change nothing. The application checks for duplicates before
writing; the index is what makes that true rather than usually true.

**Two unique indexes on existing tables.** ``uq_conversations_account_id_id``
and ``uq_ai_calls_account_id_id`` exist so the composite foreign keys above can
reference them. They are created here, by the migration that needs them, rather
than retrofitted into 0009 and 0010 -- a migration already applied is history.

Revision ID: 0011
Revises: 0010
Created: 2026-07-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "memory_proposals"


def upgrade() -> None:
    """Create the memory_proposals table and the indexes its keys need."""
    op.create_index(
        op.f("uq_conversations_account_id_id"),
        "conversations",
        ["account_id", "id"],
        unique=True,
    )
    op.create_index(
        op.f("uq_ai_calls_account_id_id"),
        "ai_calls",
        ["account_id", "id"],
        unique=True,
    )

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("ai_call_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("prompt_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_proposals")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_memory_proposals_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "conversation_id"],
            ["conversations.account_id", "conversations.id"],
            name=op.f("fk_memory_proposals_account_id_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "ai_call_id"],
            ["ai_calls.account_id", "ai_calls.id"],
            name=op.f("fk_memory_proposals_account_id_ai_call_id_ai_calls"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("id > 0", name=op.f("ck_memory_proposals_id_positive")),
        sa.CheckConstraint(
            "account_id > 0", name=op.f("ck_memory_proposals_account_id_positive")
        ),
        sa.CheckConstraint(
            "conversation_id > 0", name=op.f("ck_memory_proposals_conversation_id_positive")
        ),
        sa.CheckConstraint(
            "ai_call_id > 0", name=op.f("ck_memory_proposals_ai_call_id_positive")
        ),
        sa.CheckConstraint(
            "category IN ('identity', 'location', 'occupation', 'interest', 'preference', "
            "'relationship', 'important_date', 'plan', 'shared_experience', "
            "'open_question', 'constraint', 'other')",
            name=op.f("ck_memory_proposals_category_known"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name=op.f("ck_memory_proposals_status_known"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_memory_proposals_confidence_in_range"),
        ),
        sa.CheckConstraint(
            "length(trim(value)) > 0", name=op.f("ck_memory_proposals_value_not_blank")
        ),
        sa.CheckConstraint(
            "length(trim(evidence)) > 0", name=op.f("ck_memory_proposals_evidence_not_blank")
        ),
    )

    op.create_index(
        op.f("uq_memory_proposals_account_id_conversation_id_category_value"),
        TABLE,
        ["account_id", "conversation_id", "category", "value"],
        unique=True,
    )
    op.create_index(
        op.f("ix_memory_proposals_account_id_created_at"),
        TABLE,
        ["account_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Drop the memory_proposals table and the indexes added for it.

    Discards candidate facts that have not been decided about. No message,
    conversation or AI call is touched, and re-running extraction reproduces
    proposals -- at the cost of another model call, and of any review already
    done.
    """
    op.drop_index(op.f("ix_memory_proposals_account_id_created_at"), table_name=TABLE)
    op.drop_index(
        op.f("uq_memory_proposals_account_id_conversation_id_category_value"), table_name=TABLE
    )
    op.drop_table(TABLE)
    op.drop_index(op.f("uq_ai_calls_account_id_id"), table_name="ai_calls")
    op.drop_index(op.f("uq_conversations_account_id_id"), table_name="conversations")
