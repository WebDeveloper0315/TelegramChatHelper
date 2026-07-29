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
    ForeignKeyConstraint,
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

CONTACTS_TABLE: Final = "contacts"

contacts = Table(
    CONTACTS_TABLE,
    metadata,
    # A locally generated key, not the Telegram identifier. The same person can
    # be known to two accounts, so telegram_user_id is not unique in this table
    # -- only the pair below is -- and a natural key would push both columns
    # into every child table's foreign key (ADR-041).
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column(
        "account_id",
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE", name="fk_contacts_account_id_accounts"),
        nullable=False,
    ),
    Column("telegram_user_id", Integer, nullable=False),
    # The one genuinely optional column: many Telegram users have never set a
    # username. Null here means "has none", not "not decided yet".
    Column("username", String(32), nullable=True),
    Column("display_name", String(128), nullable=False),
    # Timestamps rather than booleans: retention has to ask "deleted before
    # when", and a boolean cannot answer that.
    Column("archived_at", DateTime(timezone=True), nullable=True),
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint("telegram_user_id > 0", name="telegram_user_id_positive"),
    CheckConstraint("length(trim(display_name)) > 0", name="display_name_not_blank"),
    CheckConstraint(
        "username IS NULL OR length(username) BETWEEN 5 AND 32",
        name="username_length",
    ),
    CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    # Archived and deleted are mutually exclusive states, not two flags that
    # happen to be usually apart.
    CheckConstraint(
        "archived_at IS NULL OR deleted_at IS NULL",
        name="not_archived_and_deleted",
    ),
    comment=(
        "People known to an account. Unique per (account_id, telegram_user_id): "
        "the same person known to two accounts is two contacts, because what is "
        "remembered about them differs per account."
    ),
)

# The documented invariant, made structural. Covers soft-deleted rows
# deliberately: a deleted contact still occupies the natural key, so re-adding
# the same person is refused and the caller is told to restore instead. That is
# what makes the deletion soft rather than a slow way to lose history.
Index(
    "uq_contacts_account_id_telegram_user_id",
    contacts.c.account_id,
    contacts.c.telegram_user_id,
    unique=True,
)

# The listing query: scoped by account, ordered by created_at with id as the
# keyset tiebreaker. Leading with account_id because every query this table
# serves is account-scoped, so no query benefits from an index that is not.
Index(
    "ix_contacts_account_id_created_at",
    contacts.c.account_id,
    contacts.c.created_at,
    contacts.c.id,
)

# Referenced by the composite foreign key from chats. Redundant with the primary
# key on its own, and that is the point: it is what lets another table reference
# (account_id, id) together, so a child row cannot name a contact belonging to a
# different account (ADR-043).
Index(
    "uq_contacts_account_id_id",
    contacts.c.account_id,
    contacts.c.id,
    unique=True,
)

CHATS_TABLE: Final = "chats"

chats = Table(
    CHATS_TABLE,
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column(
        "account_id",
        Integer,
        ForeignKey("accounts.id", ondelete="CASCADE", name="fk_chats_account_id_accounts"),
        nullable=False,
    ),
    # Telegram numbers groups and channels below zero, so the check below is
    # "not zero" rather than the "positive" that suits a user identifier.
    Column("telegram_chat_id", Integer, nullable=False),
    Column("chat_type", String(16), nullable=False),
    # Null for every kind but private, and never null for private. Enforced in
    # both directions by the check constraints below.
    Column("contact_id", Integer, nullable=True),
    Column("title", String(256), nullable=True),
    Column("sync_enabled", Boolean, nullable=False, server_default=text("1")),
    Column("ai_processing_mode", String(16), nullable=False, server_default="local_only"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # A composite foreign key, not the obvious contact_id -> contacts.id. The
    # simple version would permit a chat in one account to name a contact in
    # another; this one cannot, because the pair must exist together. Cascade
    # rather than SET NULL: nulling the column would break the private-chat
    # invariant, and a contact purge must remove everything referencing them
    # (PRIVACY.md section 7, ADR-043).
    ForeignKeyConstraint(
        ["account_id", "contact_id"],
        ["contacts.account_id", "contacts.id"],
        name="fk_chats_account_id_contact_id_contacts",
        ondelete="CASCADE",
    ),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint("telegram_chat_id <> 0", name="telegram_chat_id_not_zero"),
    CheckConstraint("contact_id IS NULL OR contact_id > 0", name="contact_id_positive"),
    CheckConstraint(
        "chat_type IN ('private', 'group', 'supergroup', 'channel', 'saved')",
        name="chat_type_known",
    ),
    CheckConstraint(
        "ai_processing_mode IN ('disabled', 'local_only', 'cloud_allowed')",
        name="ai_processing_mode_known",
    ),
    # Both directions of one rule: a private chat is with exactly one person and
    # has no name of its own; every other kind has a name and no single person.
    CheckConstraint(
        "(chat_type = 'private') = (contact_id IS NOT NULL)",
        name="contact_iff_private",
    ),
    CheckConstraint(
        "(chat_type <> 'private') = (title IS NOT NULL)",
        name="title_iff_not_private",
    ),
    CheckConstraint(
        "title IS NULL OR length(trim(title)) > 0",
        name="title_not_blank",
    ),
    CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    comment=(
        "Communication containers: the edge joining an account to a contact. "
        "Messages, synchronisation state and per-chat AI policy all attach here."
    ),
)

Index(
    "uq_chats_account_id_telegram_chat_id",
    chats.c.account_id,
    chats.c.telegram_chat_id,
    unique=True,
)

# At most one private chat per contact. Partial, because contact_id is null for
# every other kind and many nulls must remain permitted. Both dialect predicates
# are given so the same index exists on PostgreSQL (ADR-016).
Index(
    "uq_chats_account_id_contact_id",
    chats.c.account_id,
    chats.c.contact_id,
    unique=True,
    sqlite_where=text("contact_id IS NOT NULL"),
    postgresql_where=text("contact_id IS NOT NULL"),
)

# The listing query: scoped by account, ordered by created_at with id as the
# keyset tiebreaker.
Index(
    "ix_chats_account_id_created_at",
    chats.c.account_id,
    chats.c.created_at,
    chats.c.id,
)

# Referenced by the composite foreign key from messages, for the same reason the
# equivalent index on contacts exists: it is what lets a child row name a chat
# and an account together, so a message cannot be filed in another account's
# chat (ADR-043).
Index(
    "uq_chats_account_id_id",
    chats.c.account_id,
    chats.c.id,
    unique=True,
)

MESSAGES_TABLE: Final = "messages"

messages = Table(
    MESSAGES_TABLE,
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("account_id", Integer, nullable=False),
    Column("chat_id", Integer, nullable=False),
    # Optional: ingestion is source-agnostic, and only Telegram issues these.
    # Its presence is what makes a message re-ingestable without duplication;
    # its absence means there is nothing to deduplicate against (ADR-045).
    Column("telegram_message_id", Integer, nullable=True),
    Column("sender_kind", String(16), nullable=False),
    Column("message_type", String(16), nullable=False, server_default="text"),
    # Conversation content. Nullable because a photo or a sticker has none.
    Column("text", Text, nullable=True),
    Column("sent_at", DateTime(timezone=True), nullable=False),
    Column("ingested_at", DateTime(timezone=True), nullable=False),
    # No updated_at. A message is an immutable factual record, and the absence
    # of the column is what says so.
    ForeignKeyConstraint(
        ["account_id", "chat_id"],
        ["chats.account_id", "chats.id"],
        name="fk_messages_account_id_chat_id_chats",
        ondelete="CASCADE",
    ),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint("chat_id > 0", name="chat_id_positive"),
    CheckConstraint(
        "telegram_message_id IS NULL OR telegram_message_id > 0",
        name="telegram_message_id_positive",
    ),
    CheckConstraint(
        "sender_kind IN ('operator', 'contact', 'system')",
        name="sender_kind_known",
    ),
    CheckConstraint(
        "message_type IN ('text', 'photo', 'voice', 'video', 'document', "
        "'sticker', 'location', 'poll', 'service', 'other')",
        name="message_type_known",
    ),
    # A text message must have text; every other kind may have a caption or
    # nothing.
    CheckConstraint(
        "message_type <> 'text' OR (text IS NOT NULL AND length(trim(text)) > 0)",
        name="text_present_for_text_messages",
    ),
    comment=(
        "The immutable factual record. Append-only: there is no update path, "
        "which is why the table has no updated_at."
    ),
)

# The idempotency guarantee, and the reason re-synchronisation is safe. Partial,
# because a message from a source that issues no identifiers has NULL here and
# many such rows must remain permitted (ADR-045).
Index(
    "uq_messages_account_id_chat_id_telegram_message_id",
    messages.c.account_id,
    messages.c.chat_id,
    messages.c.telegram_message_id,
    unique=True,
    sqlite_where=text("telegram_message_id IS NOT NULL"),
    postgresql_where=text("telegram_message_id IS NOT NULL"),
)

# The history query: one chat, newest first, with id as the keyset tiebreaker.
# Ordered by sent_at rather than insertion order, because a backfill inserts old
# messages after new ones.
Index(
    "ix_messages_account_id_chat_id_sent_at",
    messages.c.account_id,
    messages.c.chat_id,
    messages.c.sent_at,
    messages.c.id,
)

TELEGRAM_SESSIONS_TABLE: Final = "telegram_sessions"

telegram_sessions = Table(
    TELEGRAM_SESSIONS_TABLE,
    metadata,
    # The account identifier is the primary key, not a surrogate beside one.
    # Exactly one session per account, so this makes the invariant the key
    # itself (the reasoning ADR-038 applied to user_profiles).
    Column(
        "account_id",
        Integer,
        ForeignKey(
            "accounts.id", ondelete="CASCADE", name="fk_telegram_sessions_account_id_accounts"
        ),
        primary_key=True,
        autoincrement=False,
    ),
    # Two independent axes, because TDLib reports two and they vary
    # independently: a session can be authorized while reconnecting (ADR-049).
    Column("authorization_state", String(24), nullable=False, server_default="unauthorized"),
    Column("connection_state", String(24), nullable=False, server_default="offline"),
    Column("session_path", Text, nullable=False),
    # A NAME in the SecretStore, never key material. A key value in this column
    # is a security defect (SECURITY.md section 7, ADR-021).
    Column("encryption_key_ref", String(128), nullable=False),
    Column("client_version", String(32), nullable=True),
    Column("connected_at", DateTime(timezone=True), nullable=True),
    Column("last_activity_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint(
        "authorization_state IN ('unauthorized', 'waiting_phone', 'waiting_code', "
        "'waiting_password', 'ready', 'logged_out')",
        name="authorization_state_known",
    ),
    CheckConstraint(
        "connection_state IN ('offline', 'connecting', 'updating', 'ready', 'waiting_for_network')",
        name="connection_state_known",
    ),
    CheckConstraint("length(trim(session_path)) > 0", name="session_path_not_blank"),
    CheckConstraint("length(trim(encryption_key_ref)) > 0", name="key_ref_not_blank"),
    # A connection time outliving its connection would answer "how long
    # connected" with a duration that never happened. Connected means updating
    # or ready: TDLib's socket is up from 'updating' onwards.
    CheckConstraint(
        "connection_state IN ('updating', 'ready') OR connected_at IS NULL",
        name="unconnected_has_no_connection_time",
    ),
    CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    comment=(
        "Per-account Telegram session state. Holds the NAME of the store's "
        "encryption key, never the key: that lives in the OS credential store."
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
    "CHATS_TABLE",
    "CONTACTS_TABLE",
    "KEY_APPLICATION",
    "KEY_CREATED_AT",
    "KEY_CREATED_BY_VERSION",
    "MESSAGES_TABLE",
    "NAMING_CONVENTION",
    "SCHEMA_METADATA_TABLE",
    "TELEGRAM_SESSIONS_TABLE",
    "USER_PROFILES_TABLE",
    "accounts",
    "chats",
    "contacts",
    "messages",
    "metadata",
    "schema_metadata",
    "telegram_sessions",
    "user_profiles",
]
