"""Chats: the communication graph.

The edge joining an account to a contact, and the table every later system
attaches to -- messages by ``chat_id``, synchronisation by ``telegram_chat_id``,
per-chat AI policy by ``ai_processing_mode``.

Two decisions are implemented here that are not obvious from the column list:

* The foreign key to ``contacts`` is **composite**, on ``(account_id,
  contact_id)``. The obvious ``contact_id -> contacts.id`` would permit a chat
  in one account to name a contact in another; this one cannot, because the
  pair has to exist together. It requires the unique index on
  ``contacts (account_id, id)`` added below (ADR-043).
* That foreign key **cascades** rather than setting null. ``DATABASE.md``
  version 1.0 specified ``ON DELETE SET NULL``, which cannot work: nulling
  ``contact_id`` on a private chat violates the invariant that a private chat
  names its contact, and ``PRIVACY.md`` section 7 requires a contact purge to
  remove everything referencing that person (ADR-043).

Revision ID: 0005
Revises: 0004
Created: 2026-07-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "chats"
CONTACTS = "contacts"

CONTACTS_PAIR_INDEX = "uq_contacts_account_id_id"


def upgrade() -> None:
    """Create the chats table, and the contacts index its foreign key needs."""
    # Redundant with the contacts primary key on its own. That is the point: a
    # composite foreign key can only reference columns that are unique together,
    # so this index is what makes the ownership guarantee expressible.
    op.create_index(CONTACTS_PAIR_INDEX, CONTACTS, ["account_id", "id"], unique=True)

    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        # Telegram numbers groups and channels below zero, so the check is "not
        # zero" rather than the "positive" that suits a user identifier.
        sa.Column("telegram_chat_id", sa.Integer(), nullable=False),
        sa.Column("chat_type", sa.String(length=16), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column("sync_enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "ai_processing_mode",
            sa.String(length=16),
            server_default="local_only",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chats")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_chats_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "contact_id"],
            ["contacts.account_id", "contacts.id"],
            name=op.f("fk_chats_account_id_contact_id_contacts"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("id > 0", name=op.f("ck_chats_id_positive")),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_chats_account_id_positive")),
        sa.CheckConstraint(
            "telegram_chat_id <> 0", name=op.f("ck_chats_telegram_chat_id_not_zero")
        ),
        sa.CheckConstraint(
            "contact_id IS NULL OR contact_id > 0", name=op.f("ck_chats_contact_id_positive")
        ),
        sa.CheckConstraint(
            "chat_type IN ('private', 'group', 'supergroup', 'channel', 'saved')",
            name=op.f("ck_chats_chat_type_known"),
        ),
        sa.CheckConstraint(
            "ai_processing_mode IN ('disabled', 'local_only', 'cloud_allowed')",
            name=op.f("ck_chats_ai_processing_mode_known"),
        ),
        # Both directions of one rule, so a private chat cannot exist with
        # nobody in it and a group chat cannot claim a single counterpart.
        sa.CheckConstraint(
            "(chat_type = 'private') = (contact_id IS NOT NULL)",
            name=op.f("ck_chats_contact_iff_private"),
        ),
        sa.CheckConstraint(
            "(chat_type <> 'private') = (title IS NOT NULL)",
            name=op.f("ck_chats_title_iff_not_private"),
        ),
        sa.CheckConstraint(
            "title IS NULL OR length(trim(title)) > 0",
            name=op.f("ck_chats_title_not_blank"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_chats_updated_after_created")
        ),
    )

    op.create_index(
        "uq_chats_account_id_telegram_chat_id",
        TABLE,
        ["account_id", "telegram_chat_id"],
        unique=True,
    )

    # At most one private chat per contact. Partial, because contact_id is null
    # for every other kind and many nulls must remain permitted.
    op.create_index(
        "uq_chats_account_id_contact_id",
        TABLE,
        ["account_id", "contact_id"],
        unique=True,
        sqlite_where=sa.text("contact_id IS NOT NULL"),
        postgresql_where=sa.text("contact_id IS NOT NULL"),
    )

    op.create_index(
        "ix_chats_account_id_created_at",
        TABLE,
        ["account_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Drop the chats table and the index added for its foreign key."""
    op.drop_index("ix_chats_account_id_created_at", table_name=TABLE)
    op.drop_index("uq_chats_account_id_contact_id", table_name=TABLE)
    op.drop_index("uq_chats_account_id_telegram_chat_id", table_name=TABLE)
    op.drop_table(TABLE)
    op.drop_index(CONTACTS_PAIR_INDEX, table_name=CONTACTS)
