"""AI calls.

One row per model invocation, including the ones that failed.

Every column, and why it is here:

* ``account_id`` -- whose call it was. Cascades: an account's instrumentation
  goes with the account.
* ``chat_id`` -- the chat whose content the call was about, or NULL for a task
  that is not about a conversation. Present because the privacy gate is *per
  chat* (ADR-024), so a record without it could not be audited against the
  permission that allowed it. Part of a **composite** foreign key, so a call in
  one account cannot name another account's chat, and **cascading**: a record
  derived from a chat the user deleted is residue of that chat. SET NULL is not
  available here -- nulling a composite key nulls every column in it, including
  the NOT NULL ``account_id``. The consequence is deliberate and worth stating:
  deleting a chat removes its calls from the spend history.
* ``vendor`` and ``model_identifier`` -- which model answered, verbatim. An
  expensive call has to be traceable to the exact model that made it, and a
  provider may route to a revision other than the one that was asked for.
* ``data_boundary`` -- whether the call sent content off the device. Stored
  rather than derived from the vendor: it is the fact a privacy audit asks for,
  and the vendor-to-boundary mapping is code that may change.
* ``prompt_id`` and ``prompt_version`` -- recorded from the very first call.
  The question they answer -- "did the output change because the model changed
  or because we changed the prompt?" -- can only be answered by data that was
  already being collected when the change happened (ADR-057).
* ``task_kind`` -- what the call was for. Free text, not a check constraint: the
  set grows with every later milestone, and a constraint would make adding a
  task kind a migration.
* ``input_tokens`` / ``output_tokens`` -- what it consumed. NULL means
  *unreported*, which is not zero. Zero is a claim that a call was free.
* ``estimated_cost`` / ``cost_currency`` -- what it is estimated to have cost.
  **Text, not REAL**: this is money in fractions of a cent accumulated over many
  rows, and binary floating point is exactly where that drifts. NULL when the
  model is unpriced or the tokens were unreported.
* ``latency_ms`` -- how long it took, including a timeout's full wait. The
  measurement this table exists for (ADR-029 §6).
* ``outcome`` -- how it ended, from this application's side. Written for every
  outcome: success-only instrumentation hides exactly the expensive cases.
* ``finish_reason`` -- why the model stopped, in its own words. Distinct from
  the outcome: a response can finish for the reason ``length`` and still be a
  successful call.
* ``response_digest`` -- a truncated SHA-256 of the response. What deterministic
  replay compares, and what content deliberately is not.
* ``response_text`` -- the response itself, and **normally NULL**. Written only
  when ``ai.store_responses`` is on, which the production profile refuses, in
  exactly the arrangement ``logging.diagnostic_mode`` already has.

**No prompt text, ever, under any setting.** A prompt carries the conversation
content the task assembled, and `SECURITY.md` §9 makes no exception for
instrumentation.

**No ``updated_at``.** An AI call is an immutable record of an instant, and the
absence of the column is what says so. `DOMAIN_MODEL.md` §5.25's ``retry_count``
and ``related_entity_*`` are not created either: nothing retries, and nothing
relates a call to an entity until slice 9b produces one.

**No ``ai_providers`` table.** `DOMAIN_MODEL.md` §5.24 specifies one; nothing
yet enumerates models at runtime, and configuration already names the one in
use. It arrives with the code that needs to choose between several.

Revision ID: 0010
Revises: 0009
Created: 2026-07-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ai_calls"


def upgrade() -> None:
    """Create the ai_calls table."""
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("model_identifier", sa.String(length=128), nullable=False),
        sa.Column("data_boundary", sa.String(length=16), nullable=False),
        sa.Column("prompt_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("task_kind", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.String(length=32), nullable=True),
        sa.Column("cost_currency", sa.String(length=3), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("finish_reason", sa.String(length=24), nullable=True),
        sa.Column("response_digest", sa.String(length=64), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_calls")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_ai_calls_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "chat_id"],
            ["chats.account_id", "chats.id"],
            name=op.f("fk_ai_calls_account_id_chat_id_chats"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("id > 0", name=op.f("ck_ai_calls_id_positive")),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_ai_calls_account_id_positive")),
        sa.CheckConstraint(
            "chat_id IS NULL OR chat_id > 0", name=op.f("ck_ai_calls_chat_id_positive")
        ),
        sa.CheckConstraint(
            "data_boundary IN ('local', 'external')",
            name=op.f("ck_ai_calls_data_boundary_known"),
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'timeout', 'rate_limited', 'provider_error', "
            "'malformed', 'cancelled', 'refused')",
            name=op.f("ck_ai_calls_outcome_known"),
        ),
        sa.CheckConstraint(
            "finish_reason IS NULL OR finish_reason IN "
            "('stop', 'length', 'content_filter', 'other')",
            name=op.f("ck_ai_calls_finish_reason_known"),
        ),
        sa.CheckConstraint(
            "(outcome = 'success') = (finish_reason IS NOT NULL)",
            name=op.f("ck_ai_calls_success_records_a_finish_reason"),
        ),
        sa.CheckConstraint("latency_ms >= 0", name=op.f("ck_ai_calls_latency_not_negative")),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name=op.f("ck_ai_calls_input_tokens_not_negative"),
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_ai_calls_output_tokens_not_negative"),
        ),
        sa.CheckConstraint(
            "(estimated_cost IS NULL) = (cost_currency IS NULL)",
            name=op.f("ck_ai_calls_cost_has_a_currency"),
        ),
        sa.CheckConstraint(
            "response_text IS NULL OR response_digest IS NOT NULL",
            name=op.f("ck_ai_calls_stored_response_has_a_digest"),
        ),
    )

    op.create_index(
        op.f("ix_ai_calls_account_id_created_at"),
        TABLE,
        ["account_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Drop the ai_calls table.

    Removes instrumentation, not user data: no message, contact or conversation
    is touched. What is lost is the record of what has been spent, which cannot
    be recomputed -- so a downgrade should be taken as deliberately discarding a
    spending history rather than as a reversible step.
    """
    op.drop_index(op.f("ix_ai_calls_account_id_created_at"), table_name=TABLE)
    op.drop_table(TABLE)
