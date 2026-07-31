"""Retrieval: what a memory is worth, and how often it has been used.

Three columns on `memories` and one index, all of them added because retrieval
now exists and reads them. Nothing here is speculative — `DOMAIN_MODEL.md` §5.9
named these fields two milestones ago, and they waited until something asked for
them (ADR-060).

* ``importance`` — how much the fact matters, as the person who accepted it
  judged. NOT NULL with a default of `0.5`, which is what accepting without
  saying means. **Ranked above ``confidence``**: a person's judgement of what is
  worth knowing outranks a machine's estimate of what is true, and self-reported
  confidence is poorly calibrated (`AI_MODELS.md` §15). Existing rows take the
  default, which is correct rather than convenient — nobody said anything about
  them.
* ``retrieval_count`` — how many times this memory has been selected into a
  context. **Bookkeeping about the fact, not part of it**, which is what lets it
  change while the memory itself stays immutable. Written by a single statement
  over the selected rows, incremented in SQL rather than read-modify-written, so
  two contexts built at once cannot lose a count.
* ``last_retrieved_at`` — when it was last selected, or NULL if never.
  **Deliberately not a ranking input.** Ranking by it would make a retrieved
  memory rank higher and so be retrieved again — a feedback loop rather than a
  relevance signal. It exists to answer "is anything here ever used", which is
  the question that will eventually justify or refute this whole ranking.

Two checks keep the pair honest: the count and the timestamp move together, and
nothing can have been retrieved before it existed.

**The index is chosen by the query, not guessed.** Retrieval asks for one
account's live memories about one contact:

```sql
SELECT ... FROM memories
 WHERE account_id = ? AND contact_id IS ? AND deleted_at IS NULL
 ORDER BY created_at DESC, id DESC
```

so `ix_memories_account_id_contact_id_created_at` leads with exactly the `WHERE`
columns and follows with the order a candidate cap takes when it bites. It is
**partial** on `deleted_at IS NULL`: a forgotten memory should occupy no space
in the index every context walks.

There is no index on `importance`, `confidence` or `category`. Ranking happens
in memory over a set already bounded to one contact, and no index can serve a
five-key lexicographic order.

Revision ID: 0013
Revises: 0012
Created: 2026-07-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "memories"
INDEX = "ix_memories_account_id_contact_id_created_at"


def upgrade() -> None:
    """Add the retrieval columns and the index retrieval reads through."""
    # SQLite cannot add a CHECK to an existing table, so the batch operation
    # rebuilds it. The server defaults are what give existing rows a value; the
    # columns are NOT NULL because "unknown importance" and "unknown retrieval
    # count" are not states anything could interpret.
    with op.batch_alter_table(TABLE, schema=None) as batch:
        batch.add_column(
            sa.Column(
                "importance",
                sa.Float(),
                nullable=False,
                server_default=sa.text("0.5"),
            )
        )
        batch.add_column(
            sa.Column(
                "retrieval_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column("last_retrieved_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_check_constraint(
            "importance_in_range", "importance >= 0 AND importance <= 1"
        )
        batch.create_check_constraint("retrieval_count_not_negative", "retrieval_count >= 0")
        batch.create_check_constraint(
            "retrieval_history_agrees",
            "(retrieval_count > 0) = (last_retrieved_at IS NOT NULL)",
        )
        batch.create_check_constraint(
            "retrieved_after_created",
            "last_retrieved_at IS NULL OR last_retrieved_at >= created_at",
        )

    op.create_index(
        op.f(INDEX),
        TABLE,
        ["account_id", "contact_id", "created_at", "id"],
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    """Drop the retrieval columns and the index.

    Loses how important each memory was said to be, and how often each has been
    used. Neither can be recomputed: the first was a person's judgement and the
    second is a history. The memories themselves survive intact.
    """
    op.drop_index(op.f(INDEX), table_name=TABLE)

    with op.batch_alter_table(TABLE, schema=None) as batch:
        # Short names: the naming convention expands them, and passing the
        # expanded form would have it expanded twice.
        batch.drop_constraint("retrieved_after_created", type_="check")
        batch.drop_constraint("retrieval_history_agrees", type_="check")
        batch.drop_constraint("retrieval_count_not_negative", type_="check")
        batch.drop_constraint("importance_in_range", type_="check")
        batch.drop_column("last_retrieved_at")
        batch.drop_column("retrieval_count")
        batch.drop_column("importance")
