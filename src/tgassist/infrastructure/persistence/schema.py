"""Schema definition.

Table definitions live here and are the target Alembic compares against when
autogenerating a migration. Business tables arrive with Milestone 1, derived
from ``docs/DOMAIN_MODEL.md``; this module currently holds only the
infrastructure table the persistence layer itself needs.

Conventions are declared once, in :data:`NAMING_CONVENTION`. Without them SQLite
invents constraint names, and an unnamed constraint cannot be dropped by a
migration -- a problem that surfaces the first time a constraint needs changing,
by which point every user has a database full of anonymous constraints.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
)

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata: Final = MetaData(naming_convention=NAMING_CONVENTION)

SCHEMA_METADATA_TABLE: Final = "schema_metadata"

schema_metadata = Table(
    SCHEMA_METADATA_TABLE,
    metadata,
    Column("key", String(64), primary_key=True),
    Column("value", Text, nullable=False),
    comment=(
        "Infrastructure metadata about the database itself. Not a business "
        "table: it records which application wrote this file and when, so that "
        "backups can embed provenance and a restore can refuse an incompatible "
        "file before overwriting anything."
    ),
)

ACCOUNTS_TABLE: Final = "accounts"

accounts = Table(
    ACCOUNTS_TABLE,
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("telegram_user_id", Integer, nullable=False),
    Column("display_name", String(128), nullable=False),
    Column("timezone", String(64), nullable=False, server_default="UTC"),
    Column("is_active", Boolean, nullable=False, server_default=text("0")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("telegram_user_id > 0", name="telegram_user_id_positive"),
    CheckConstraint("length(trim(display_name)) > 0", name="display_name_not_blank"),
    CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    comment=(
        "Telegram accounts this installation acts on behalf of. The ownership "
        "root: every other account-owned table carries account_id."
    ),
)

Index(
    "uq_accounts_telegram_user_id",
    accounts.c.telegram_user_id,
    unique=True,
)

# The single-active invariant, made structural rather than conventional.
#
# A partial unique index permits many inactive rows and at most one active one,
# so a second activation fails at the database rather than depending on every
# future caller remembering to deactivate first. Both dialect predicates are
# given because the same index must exist on PostgreSQL (ADR-016).
Index(
    "uq_accounts_single_active",
    accounts.c.is_active,
    unique=True,
    sqlite_where=text("is_active = 1"),
    postgresql_where=text("is_active"),
)

USER_PROFILES_TABLE: Final = "user_profiles"

user_profiles = Table(
    USER_PROFILES_TABLE,
    metadata,
    # The account identifier is the primary key, not a surrogate alongside one.
    # Exactly one profile exists per account, so this makes the invariant the
    # key itself rather than a unique index that could be dropped (ADR-038).
    Column(
        "account_id",
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE", name="fk_user_profiles_account_id_accounts"),
        primary_key=True,
        autoincrement=False,
    ),
    Column("primary_language", String(32), nullable=False, server_default="en"),
    Column("tone_preference", String(16), nullable=False, server_default="neutral"),
    Column("preferred_message_length", String(16), nullable=False, server_default="medium"),
    Column("emoji_usage", String(16), nullable=False, server_default="sparing"),
    Column("quiet_hours_start_minute", Integer, nullable=False, server_default=text("1320")),
    Column("quiet_hours_end_minute", Integer, nullable=False, server_default=text("480")),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "tone_preference IN ('casual', 'neutral', 'formal', 'mirror_contact')",
        name="tone_preference_known",
    ),
    CheckConstraint(
        "preferred_message_length IN ('short', 'medium', 'long')",
        name="message_length_known",
    ),
    CheckConstraint(
        "emoji_usage IN ('none', 'sparing', 'frequent')",
        name="emoji_usage_known",
    ),
    CheckConstraint("quiet_hours_start_minute BETWEEN 0 AND 1439", name="quiet_start_within_day"),
    CheckConstraint("quiet_hours_end_minute BETWEEN 0 AND 1439", name="quiet_end_within_day"),
    CheckConstraint(
        "quiet_hours_start_minute <> quiet_hours_end_minute", name="quiet_hours_not_whole_day"
    ),
    CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    comment=(
        "Operator preferences, exactly one row per account. Every column is NOT "
        "NULL with a default: a preference that can be null forces every reader "
        "to decide what null means, and they will not all decide the same thing."
    ),
)

# Keys used in schema_metadata.
KEY_CREATED_AT: Final = "created_at"
KEY_CREATED_BY_VERSION: Final = "created_by_version"
KEY_APPLICATION: Final = "application"

APPLICATION_NAME: Final = "tgassist"

__all__ = [
    "ACCOUNTS_TABLE",
    "APPLICATION_NAME",
    "KEY_APPLICATION",
    "KEY_CREATED_AT",
    "KEY_CREATED_BY_VERSION",
    "NAMING_CONVENTION",
    "SCHEMA_METADATA_TABLE",
    "USER_PROFILES_TABLE",
    "accounts",
    "metadata",
    "schema_metadata",
    "user_profiles",
]
