"""Memories, and the timestamp a proposal's decision needs.

The other half of the lifecycle `0011` began. A proposal records what a model
said; a memory records what a person decided to believe, and **nothing reaches
this table without a decision** (ADR-019, ADR-059).

`memory_proposals` gains one column:

* ``decided_at`` -- when somebody decided, or NULL while pending. It moves with
  the status and cannot disagree with it: a check refuses a decided proposal
  with no timestamp and a pending one with a timestamp, so "has this been
  decided" has one answer however it is asked. No ``decided_by``: every decision
  in this milestone is a person's, because there is no other way to make one, so
  the column would record a constant.

`memories`, and why every column is here:

* ``id`` -- assigned when a proposal is **accepted**, not when it was extracted.
  A memory and its proposal are different things with different lifetimes, and
  one identifier for both would make the first indistinguishable from the
  second in every later reference.
* ``account_id`` -- whose memory. Cascades with the account.
* ``contact_id`` -- who the fact is about. NULL only for a conversation with no
  single counterpart (a group chat); a fact about somebody in particular always
  names them. Part of a **composite** foreign key so a memory cannot be about
  another account's contact (ADR-043), and cascading, because purging a contact
  removes everything about them (`PRIVACY.md` §7).
* ``category`` -- from the proposal, check-constrained to the closed set in
  `DOMAIN_MODEL.md` §5.9.
* ``key`` -- the comparison form of the value: case folded, punctuation
  dropped, whitespace collapsed, truncated. **Derived by the application and
  never supplied by a model.** It is what makes storing the same fact twice
  impossible. It does *not* detect contradictions — "Lives in Lisbon" and
  "Lives in Porto" are different keys — and that limitation is deliberate and
  argued in ADR-059.
* ``value`` -- the fact, in the words the model used and the person approved.
* ``confidence`` -- what the model reported when it proposed this, kept as
  recorded. A person accepting a fact says it is worth keeping, not that the
  model was certain; the two are different claims and flattening them would
  lose the one that came from a machine.
* ``source`` -- how this application came to believe it. Only ``ai_approved``
  is written today; ``user`` and ``ai_auto`` are named in the constraint
  because they are a closed vocabulary later ranking depends on, and adding a
  value later would be a migration.
* ``proposal_id`` / ``conversation_id`` / ``ai_call_id`` -- provenance. Through
  them a memory leads back to the decision, the exchange and the model call
  that produced it.
* ``created_at`` -- when it was accepted. The moment a person made it true for
  this application.
* ``deleted_at`` -- when it was forgotten. A timestamp rather than a flag,
  because retention has to ask "deleted before when" and a boolean cannot
  answer that. **No ``updated_at``**: nothing updates a memory. Correcting one
  means deleting it and accepting a new proposal, because an edit in place
  would keep the provenance while changing the fact.

**Provenance foreign keys are SET NULL, not CASCADE.** The one place in this
schema where that is right. A memory is user-approved knowledge, and it does not
stop being known because the conversation it came from was deleted — what is
lost is the trail, not the fact. `ai_calls` and `memory_proposals` cascade from
chats, so without this a chat deletion would silently erase approved memories.

**Three unique indexes, each doing a different job:**

* ``uq_memories_proposal_id`` (partial, where not null) — one memory per
  accepted proposal, so "acceptance creates exactly one memory" is a constraint
  rather than a rule, including when two decisions race.
* ``uq_memories_account_id_contact_id_category_key`` (partial, where not
  deleted and contact is known) — one fact per person. Deleting a memory frees
  its key, so a fact can be accepted again after being forgotten.
* ``uq_memories_account_id_category_key`` (partial, where not deleted and
  contact is unknown) — the same rule for group-derived facts. Two indexes
  rather than one because SQL treats NULLs as distinct, so without the second,
  identical facts from group conversations would both be stored.

**No ``importance``, ``is_pinned``, ``valid_from``/``valid_until``,
``last_retrieved_at`` or ``retrieval_count``**, all named by `DOMAIN_MODEL.md`
§5.9. Every one of them exists to serve *retrieval*, which is Slice 9d. No
``memory_revisions`` table either: revisions record edits, and nothing edits.

Revision ID: 0012
Revises: 0011
Created: 2026-07-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "memories"
PROPOSALS = "memory_proposals"


def upgrade() -> None:
    """Add decided_at to memory_proposals, and create the memories table."""
    # SQLite cannot add a CHECK constraint to an existing table, so the batch
    # operation rebuilds it. Everything else in the table definition has to be
    # restated for the rebuild to preserve it, which is why the constraints
    # below appear here as well as in 0011.
    with op.batch_alter_table(PROPOSALS, schema=None) as batch:
        batch.add_column(sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "decision_has_a_time", "(status = 'pending') = (decided_at IS NULL)"
        )
        batch.create_check_constraint(
            "decided_after_created", "decided_at IS NULL OR decided_at >= created_at"
        )

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("ai_call_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_memories_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "contact_id"],
            ["contacts.account_id", "contacts.id"],
            name=op.f("fk_memories_account_id_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["memory_proposals.id"],
            name=op.f("fk_memories_proposal_id_memory_proposals"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_memories_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["ai_call_id"],
            ["ai_calls.id"],
            name=op.f("fk_memories_ai_call_id_ai_calls"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("id > 0", name=op.f("ck_memories_id_positive")),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_memories_account_id_positive")),
        sa.CheckConstraint(
            "contact_id IS NULL OR contact_id > 0", name=op.f("ck_memories_contact_id_positive")
        ),
        sa.CheckConstraint(
            "proposal_id IS NULL OR proposal_id > 0",
            name=op.f("ck_memories_proposal_id_positive"),
        ),
        sa.CheckConstraint(
            "conversation_id IS NULL OR conversation_id > 0",
            name=op.f("ck_memories_conversation_id_positive"),
        ),
        sa.CheckConstraint(
            "ai_call_id IS NULL OR ai_call_id > 0", name=op.f("ck_memories_ai_call_id_positive")
        ),
        sa.CheckConstraint(
            "category IN ('identity', 'location', 'occupation', 'interest', 'preference', "
            "'relationship', 'important_date', 'plan', 'shared_experience', "
            "'open_question', 'constraint', 'other')",
            name=op.f("ck_memories_category_known"),
        ),
        sa.CheckConstraint(
            "source IN ('user', 'ai_approved', 'ai_auto')", name=op.f("ck_memories_source_known")
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_memories_confidence_in_range")
        ),
        sa.CheckConstraint("length(trim(key)) > 0", name=op.f("ck_memories_key_not_blank")),
        sa.CheckConstraint("length(trim(value)) > 0", name=op.f("ck_memories_value_not_blank")),
        sa.CheckConstraint(
            "deleted_at IS NULL OR deleted_at >= created_at",
            name=op.f("ck_memories_deleted_after_created"),
        ),
    )

    op.create_index(
        op.f("uq_memories_proposal_id"),
        TABLE,
        ["proposal_id"],
        unique=True,
        sqlite_where=sa.text("proposal_id IS NOT NULL"),
        postgresql_where=sa.text("proposal_id IS NOT NULL"),
    )
    op.create_index(
        op.f("uq_memories_account_id_contact_id_category_key"),
        TABLE,
        ["account_id", "contact_id", "category", "key"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL AND contact_id IS NOT NULL"),
        postgresql_where=sa.text("deleted_at IS NULL AND contact_id IS NOT NULL"),
    )
    op.create_index(
        op.f("uq_memories_account_id_category_key"),
        TABLE,
        ["account_id", "category", "key"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL AND contact_id IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL AND contact_id IS NULL"),
    )
    op.create_index(
        op.f("ix_memories_account_id_created_at"),
        TABLE,
        ["account_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Drop memories and the proposal decision timestamp.

    **Destroys approved knowledge.** Unlike every earlier downgrade in this
    project, what is lost here cannot be recomputed: a memory records a decision
    a person made, and re-running extraction would produce the proposals again
    but not the decisions about them. Every proposal also returns to ``pending``
    by losing its timestamp, so the queue reappears in full.
    """
    op.drop_index(op.f("ix_memories_account_id_created_at"), table_name=TABLE)
    op.drop_index(op.f("uq_memories_account_id_category_key"), table_name=TABLE)
    op.drop_index(op.f("uq_memories_account_id_contact_id_category_key"), table_name=TABLE)
    op.drop_index(op.f("uq_memories_proposal_id"), table_name=TABLE)
    op.drop_table(TABLE)

    with op.batch_alter_table(PROPOSALS, schema=None) as batch:
        # Short names: the naming convention expands them, and passing the
        # expanded form would have it expanded twice.
        batch.drop_constraint("decided_after_created", type_="check")
        batch.drop_constraint("decision_has_a_time", type_="check")
        batch.drop_column("decided_at")
