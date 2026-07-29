"""Telegram sessions.

One row per account, recording where its encrypted local store lives and what
standing it has with Telegram.

Two things worth stating where they are implemented:

* **Two state columns, not one.** ``DOMAIN_MODEL.md`` version 1.0 specified a
  single ``state``. TDLib reports authorization and connection separately and
  they vary independently, so one column cannot express *authorized but
  reconnecting* -- the ordinary condition after a network blip (ADR-049).
* **``encryption_key_ref`` holds a name, never a key.** The key lives in the
  operating system credential store. A key value in this column is a security
  defect, not a shortcut (``SECURITY.md`` section 7).

Revision ID: 0007
Revises: 0006
Created: 2026-07-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "telegram_sessions"


def upgrade() -> None:
    """Create the telegram_sessions table."""
    op.create_table(
        TABLE,
        sa.Column("account_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "authorization_state",
            sa.String(length=24),
            server_default="unauthorized",
            nullable=False,
        ),
        sa.Column(
            "connection_state", sa.String(length=24), server_default="offline", nullable=False
        ),
        sa.Column("session_path", sa.Text(), nullable=False),
        sa.Column("encryption_key_ref", sa.String(length=128), nullable=False),
        sa.Column("client_version", sa.String(length=32), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_telegram_sessions")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name=op.f("fk_telegram_sessions_account_id_accounts"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("account_id > 0", name=op.f("ck_telegram_sessions_account_id_positive")),
        sa.CheckConstraint(
            "authorization_state IN ('unauthorized', 'waiting_phone', 'waiting_code', "
            "'waiting_password', 'ready', 'logged_out')",
            name=op.f("ck_telegram_sessions_authorization_state_known"),
        ),
        sa.CheckConstraint(
            "connection_state IN ('offline', 'connecting', 'updating', 'ready', "
            "'waiting_for_network')",
            name=op.f("ck_telegram_sessions_connection_state_known"),
        ),
        sa.CheckConstraint(
            "length(trim(session_path)) > 0",
            name=op.f("ck_telegram_sessions_session_path_not_blank"),
        ),
        sa.CheckConstraint(
            "length(trim(encryption_key_ref)) > 0",
            name=op.f("ck_telegram_sessions_key_ref_not_blank"),
        ),
        sa.CheckConstraint(
            "connection_state IN ('updating', 'ready') OR connected_at IS NULL",
            name=op.f("ck_telegram_sessions_unconnected_has_no_connection_time"),
        ),
        sa.CheckConstraint(
            "updated_at >= created_at",
            name=op.f("ck_telegram_sessions_updated_after_created"),
        ),
    )

    # No index on account_id: it is the primary key, which is already indexed.


def downgrade() -> None:
    """Drop the telegram_sessions table.

    Removes the record, not the store on disk. A downgrade that deleted a user's
    encrypted session directory would destroy data a schema change has no
    business touching; re-authentication is the recovery, and that is cheap.
    """
    op.drop_table(TABLE)
