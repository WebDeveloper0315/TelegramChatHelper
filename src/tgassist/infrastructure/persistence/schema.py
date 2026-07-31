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
    Float,
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

CONVERSATIONS_TABLE: Final = "conversations"

conversations = Table(
    CONVERSATIONS_TABLE,
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("account_id", Integer, nullable=False),
    Column("chat_id", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    # Never null. A conversation is derived from messages that already exist, so
    # it always has a last one. DOMAIN_MODEL 1.0 made this nullable to mean
    # "still open"; openness depends on *now* and is therefore asked of the
    # entity rather than stored (ADR-056).
    Column("ended_at", DateTime(timezone=True), nullable=False),
    Column("message_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["account_id", "chat_id"],
        ["chats.account_id", "chats.id"],
        name="fk_conversations_account_id_chat_id_chats",
        ondelete="CASCADE",
    ),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint("chat_id > 0", name="chat_id_positive"),
    CheckConstraint("ended_at >= started_at", name="ends_after_it_begins"),
    # A conversation is a run of messages. An empty one is a row that should
    # have been deleted, and permitting it would let a segmentation bug survive
    # as data.
    CheckConstraint("message_count > 0", name="holds_at_least_one_message"),
    CheckConstraint("updated_at >= created_at", name="updated_after_created"),
    comment=(
        "Bounded episodes of interaction, derived from messages. Membership is "
        "the time range: a message belongs to the conversation whose span "
        "contains its sent_at, so messages carry no conversation_id (ADR-056)."
    ),
)

# Two conversations in one chat cannot begin at the same instant. Combined with
# each one being a contiguous run, that is what makes "conversations do not
# overlap" structural rather than checked -- and it replaces the partial unique
# index on is_open that DATABASE.md specified for a column that is now derived.
Index(
    "uq_conversations_account_id_chat_id_started_at",
    conversations.c.account_id,
    conversations.c.chat_id,
    conversations.c.started_at,
    unique=True,
)

# The listing query, and the window read a segmentation pass begins with. Both
# ask for one chat's conversations ordered by when they began, which is also the
# order the unique index above already holds them in -- so this exists for the
# descending listing and for the keyset tiebreaker.
Index(
    "ix_conversations_account_id_chat_id_started_at",
    conversations.c.account_id,
    conversations.c.chat_id,
    conversations.c.started_at,
    conversations.c.id,
)

SYNC_CURSORS_TABLE: Final = "sync_cursors"

sync_cursors = Table(
    SYNC_CURSORS_TABLE,
    metadata,
    # The chat identifier is the primary key, not a surrogate beside one.
    # Exactly one cursor per chat, so this makes the invariant the key itself --
    # the reasoning ADR-038 applied to user_profiles and ADR-054 applies here.
    Column("chat_id", Integer, primary_key=True, autoincrement=False),
    # Carried so the foreign key can be composite. A simple chat_id reference
    # would let one account's cursor name another account's chat (ADR-043).
    Column("account_id", Integer, nullable=False),
    # Where the next fetch continues from. NULL means "nothing stored yet",
    # which is also what "start at the newest" means to the gateway.
    Column("oldest_synced_message_id", Integer, nullable=True),
    Column("newest_synced_message_id", Integer, nullable=True),
    Column("backfill_complete", Boolean, nullable=False, server_default=text("0")),
    # What "complete" meant. Without it a later run configured to reach further
    # back cannot tell that its predecessor stopped early (ADR-054).
    Column("backfill_horizon", DateTime(timezone=True), nullable=True),
    Column("last_sync_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # No created_at: nothing asks when a chat was first synchronised, and
    # last_sync_at is the time that means something.
    ForeignKeyConstraint(
        ["account_id", "chat_id"],
        ["chats.account_id", "chats.id"],
        name="fk_sync_cursors_account_id_chat_id_chats",
        ondelete="CASCADE",
    ),
    CheckConstraint("chat_id > 0", name="chat_id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint(
        "oldest_synced_message_id IS NULL OR oldest_synced_message_id > 0",
        name="oldest_synced_message_id_positive",
    ),
    CheckConstraint(
        "newest_synced_message_id IS NULL OR newest_synced_message_id > 0",
        name="newest_synced_message_id_positive",
    ),
    # The two ends of one range: either both are set or neither is. A floor with
    # no ceiling describes a range whose extent nobody can state.
    CheckConstraint(
        "(oldest_synced_message_id IS NULL) = (newest_synced_message_id IS NULL)",
        name="range_has_both_ends",
    ),
    CheckConstraint(
        "oldest_synced_message_id IS NULL OR oldest_synced_message_id <= newest_synced_message_id",
        name="oldest_not_after_newest",
    ),
    comment=(
        "How far each chat's history backfill has got. Written in the same "
        "transaction as the messages it accounts for, which is the whole of "
        "what makes an interrupted backfill resumable (ADR-050)."
    ),
)

# No index beyond the primary key. The only query is by chat, which the key
# serves; the scoped read adds account_id as a predicate on a single row rather
# than as a scan. A (account_id, backfill_complete) index arrives with the
# scheduler that lists pending chats, chosen by that query rather than guessed.

AI_CALLS_TABLE: Final = "ai_calls"

ai_calls = Table(
    AI_CALLS_TABLE,
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("account_id", Integer, nullable=False),
    # The chat whose content the call was about, or NULL for a task that is not
    # about a conversation. Present because the privacy gate is per chat
    # (ADR-024), so a record without it could not be audited against the
    # permission that allowed it.
    Column("chat_id", Integer, nullable=True),
    # Which model answered, recorded verbatim. An expensive call has to be
    # traceable to the exact model that made it, and a provider may route to a
    # revision other than the one asked for.
    Column("vendor", String(32), nullable=False),
    Column("model_identifier", String(128), nullable=False),
    # Whether this call sent content off the device. Stored rather than derived
    # from the vendor, because it is the fact a privacy audit asks for and the
    # vendor-to-boundary mapping is code that may change.
    Column("data_boundary", String(16), nullable=False),
    # Which prompt, at which revision. Recorded from the first call so that
    # "did the output change because the model changed or because we changed
    # the prompt" is answerable later (ADR-057).
    Column("prompt_id", String(64), nullable=False),
    Column("prompt_version", String(32), nullable=False),
    # What the call was for. Free text rather than a check constraint: the set
    # grows with every later milestone, and a constraint would make adding a
    # task kind a migration.
    Column("task_kind", String(64), nullable=False),
    # What it consumed, as far as the provider reported. NULL means unreported,
    # which is not zero: zero is a claim that a call was free.
    Column("input_tokens", Integer, nullable=True),
    Column("output_tokens", Integer, nullable=True),
    # What it is estimated to have cost. Text rather than REAL, because this is
    # money in fractions of a cent accumulated over many rows, and binary
    # floating point is exactly where that drifts. NULL when the model is
    # unpriced or the tokens were unreported.
    Column("estimated_cost", String(32), nullable=True),
    Column("cost_currency", String(3), nullable=True),
    # How long it took, including a timeout's full wait. The measurement this
    # table exists for (ADR-029 section 6).
    Column("latency_ms", Integer, nullable=False),
    # How the call ended, from this application's side. Written for every
    # outcome including the failures: success-only instrumentation hides
    # exactly the expensive cases.
    Column("outcome", String(24), nullable=False),
    # Why the model stopped, in its own words. NULL unless it answered.
    Column("finish_reason", String(24), nullable=True),
    # A truncated SHA-256 of the response. What deterministic replay compares,
    # and what content deliberately is not (SECURITY.md section 9).
    Column("response_digest", String(64), nullable=True),
    # The response itself, and normally NULL. Written only when
    # ai.store_responses is on, which the production profile refuses -- the same
    # arrangement logging.diagnostic_mode has (ADR-057).
    Column("response_text", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # No updated_at. An AI call is an immutable record of an instant, and the
    # absence of the column is what says so.
    ForeignKeyConstraint(
        ["account_id"],
        ["accounts.id"],
        name="fk_ai_calls_account_id_accounts",
        ondelete="CASCADE",
    ),
    # Composite, so an AI call in one account cannot name another account's
    # chat (ADR-043). Cascade rather than SET NULL, for two reasons: nulling a
    # composite key nulls *every* column in it, including the NOT NULL
    # account_id, which would make deleting a chat with AI calls fail outright;
    # and a record derived from a deleted chat is residue of it, which is what
    # every other child of ``chats`` already cascades to avoid
    # (PRIVACY.md section 7).
    ForeignKeyConstraint(
        ["account_id", "chat_id"],
        ["chats.account_id", "chats.id"],
        name="fk_ai_calls_account_id_chat_id_chats",
        ondelete="CASCADE",
    ),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint("chat_id IS NULL OR chat_id > 0", name="chat_id_positive"),
    CheckConstraint("data_boundary IN ('local', 'external')", name="data_boundary_known"),
    CheckConstraint(
        "outcome IN ('success', 'timeout', 'rate_limited', 'provider_error', "
        "'malformed', 'cancelled', 'refused')",
        name="outcome_known",
    ),
    CheckConstraint(
        "finish_reason IS NULL OR finish_reason IN ('stop', 'length', 'content_filter', 'other')",
        name="finish_reason_known",
    ),
    # A successful call is one the model answered, and a model that answered
    # said why it stopped. Both directions, so "succeeded" cannot mean two
    # different things.
    CheckConstraint(
        "(outcome = 'success') = (finish_reason IS NOT NULL)",
        name="success_records_a_finish_reason",
    ),
    CheckConstraint("latency_ms >= 0", name="latency_not_negative"),
    CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="input_tokens_not_negative"),
    CheckConstraint(
        "output_tokens IS NULL OR output_tokens >= 0", name="output_tokens_not_negative"
    ),
    # A cost is an amount in a currency. Half of one is not a cost.
    CheckConstraint(
        "(estimated_cost IS NULL) = (cost_currency IS NULL)", name="cost_has_a_currency"
    ),
    # A stored response is always accompanied by its digest, so a row with text
    # can always be compared against one without.
    CheckConstraint(
        "response_text IS NULL OR response_digest IS NOT NULL",
        name="stored_response_has_a_digest",
    ),
    comment=(
        "One row per model invocation, including failures. Metadata only: no "
        "prompt text ever, and response text only when diagnostics are "
        "explicitly enabled (SECURITY.md section 9, ADR-057)."
    ),
)

# The listing query: one account's calls, newest first, with id as the keyset
# tiebreaker.
Index(
    "ix_ai_calls_account_id_created_at",
    ai_calls.c.account_id,
    ai_calls.c.created_at,
    ai_calls.c.id,
)

# No index on (vendor, created_at) yet. A per-provider cost report will want
# one, and it should be chosen by that query rather than guessed a milestone
# early (DATABASE.md section 20).

# Referenced by the composite foreign key from memory_proposals, for the same
# reason the equivalent index on chats exists: it is what lets a proposal name a
# conversation and an account together, so a proposal cannot cite another
# account's conversation (ADR-043).
Index(
    "uq_conversations_account_id_id",
    conversations.c.account_id,
    conversations.c.id,
    unique=True,
)

# Referenced by the composite foreign key from memory_proposals. A proposal's
# provenance is its AI call, and provenance that could point into another
# account's audit trail would be worse than none.
Index(
    "uq_ai_calls_account_id_id",
    ai_calls.c.account_id,
    ai_calls.c.id,
    unique=True,
)

MEMORY_PROPOSALS_TABLE: Final = "memory_proposals"

memory_proposals = Table(
    MEMORY_PROPOSALS_TABLE,
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("account_id", Integer, nullable=False),
    # What it was extracted from. A conversation rather than a message: a fact
    # is often assembled from several messages, and naming one of them would be
    # a guess about which.
    Column("conversation_id", Integer, nullable=False),
    # The call that produced it, and the whole of its provenance. Through it a
    # proposal leads back to the model, the token cost and the moment it
    # happened (ADR-057).
    Column("ai_call_id", Integer, nullable=False),
    # Which kind of fact, from the closed set in DOMAIN_MODEL section 5.9.
    # Constrained rather than free text, because a category is what a user
    # filters and eventually auto-approves by.
    Column("category", String(32), nullable=False),
    # The fact itself, in the model's words. Bounded: it goes into later
    # prompts, where length is a budget everything else competes for.
    Column("value", String(500), nullable=False),
    # What the model said about its own certainty. REAL rather than text,
    # unlike a cost: nothing sums confidences, and the comparison it exists for
    # is against a threshold.
    Column("confidence", Float, nullable=False),
    # Where it stands. Only 'pending' is written today: there is no transition
    # in this milestone, which is what makes the other two terminal (ADR-058).
    Column("status", String(16), nullable=False),
    # The text it was read from, verbatim. Required, never nullable: a proposal
    # without evidence is a claim with no source, and the only way to check an
    # extraction without re-running it is to read what it was based on.
    Column("evidence", Text, nullable=False),
    # Which prompt at which revision. Duplicated from the AI call deliberately:
    # "which proposals came from the prompt we changed last week" is asked of
    # this table, and joining through an audit table to answer it would make the
    # audit table load bearing for a routine query.
    Column("prompt_id", String(64), nullable=False),
    Column("prompt_version", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # When somebody decided, or NULL while pending. Moves with the status and
    # cannot disagree with it: the check below refuses a decided proposal with
    # no timestamp and a pending one with a timestamp, so "has this been
    # decided" has one answer however it is asked (ADR-059).
    Column("decided_at", DateTime(timezone=True), nullable=True),
    # Still no updated_at, and no decided_by. A proposal has exactly one
    # transition, and every decision in this milestone is a person's -- there is
    # no other way to make one -- so a decided_by column would record a
    # constant. Auto-approval is the feature that gives it a second value.
    ForeignKeyConstraint(
        ["account_id"],
        ["accounts.id"],
        name="fk_memory_proposals_account_id_accounts",
        ondelete="CASCADE",
    ),
    # Composite, so a proposal in one account cannot cite another account's
    # conversation (ADR-043). Cascading, because a proposal is derived from the
    # conversation: when the conversation goes, the claim about it goes.
    ForeignKeyConstraint(
        ["account_id", "conversation_id"],
        ["conversations.account_id", "conversations.id"],
        name="fk_memory_proposals_account_id_conversation_id_conversations",
        ondelete="CASCADE",
    ),
    # Composite for the same reason, and cascading because a proposal whose
    # provenance had been deleted would be a fact with no visible origin --
    # which is exactly the state proposals exist to prevent.
    ForeignKeyConstraint(
        ["account_id", "ai_call_id"],
        ["ai_calls.account_id", "ai_calls.id"],
        name="fk_memory_proposals_account_id_ai_call_id_ai_calls",
        ondelete="CASCADE",
    ),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint("conversation_id > 0", name="conversation_id_positive"),
    CheckConstraint("ai_call_id > 0", name="ai_call_id_positive"),
    CheckConstraint(
        "category IN ('identity', 'location', 'occupation', 'interest', 'preference', "
        "'relationship', 'important_date', 'plan', 'shared_experience', "
        "'open_question', 'constraint', 'other')",
        name="category_known",
    ),
    CheckConstraint("status IN ('pending', 'accepted', 'rejected')", name="status_known"),
    # A confidence outside zero to one is not a low confidence: it is a model
    # that did not answer the question asked.
    CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_range"),
    CheckConstraint("length(trim(value)) > 0", name="value_not_blank"),
    # The rule that keeps unfalsifiable claims out of the queue.
    CheckConstraint("length(trim(evidence)) > 0", name="evidence_not_blank"),
    CheckConstraint(
        "(status = 'pending') = (decided_at IS NULL)",
        name="decision_has_a_time",
    ),
    CheckConstraint(
        "decided_at IS NULL OR decided_at >= created_at",
        name="decided_after_created",
    ),
    comment=(
        "AI-extracted candidate facts awaiting a human decision. Nothing here "
        "is believed: a proposal becomes a Memory only when a person accepts "
        "it (ADR-019, ADR-058)."
    ),
)

# One fact per conversation. Re-running extraction over a conversation the model
# has already seen must cost nothing and change nothing, and the application
# checks for duplicates before writing -- but the index is what makes it true
# rather than usually true, including when two extractions run at once.
Index(
    "uq_memory_proposals_account_id_conversation_id_category_value",
    memory_proposals.c.account_id,
    memory_proposals.c.conversation_id,
    memory_proposals.c.category,
    memory_proposals.c.value,
    unique=True,
)

# The review queue: one account's proposals, newest first, with id as the keyset
# tiebreaker.
Index(
    "ix_memory_proposals_account_id_created_at",
    memory_proposals.c.account_id,
    memory_proposals.c.created_at,
    memory_proposals.c.id,
)

# The duplicate check reads one conversation's proposals. Served by the unique
# index above, whose leading columns are the same -- so no second index.

MEMORIES_TABLE: Final = "memories"

memories = Table(
    MEMORIES_TABLE,
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("account_id", Integer, nullable=False),
    # Who the fact is about. NULL only when it came from a conversation with no
    # single counterpart -- a group chat. A fact about somebody in particular
    # always names them, which is what makes the uniqueness below per person.
    Column("contact_id", Integer, nullable=True),
    Column("category", String(32), nullable=False),
    # The comparison form of the value, derived by the application and never by
    # a model. What makes storing the same fact twice impossible (ADR-059).
    Column("key", String(120), nullable=False),
    # The fact, in the words the model used and the person approved.
    Column("value", String(500), nullable=False),
    # What the model reported when it proposed this. Kept as recorded rather
    # than raised to certainty: a person accepting a fact says it is worth
    # keeping, not that the model was certain.
    Column("confidence", Float, nullable=False),
    # How this application came to believe it. Only 'ai_approved' is written
    # today: accepting a proposal is the only route into this table.
    Column("source", String(16), nullable=False),
    # The proposal a person accepted. Unique, so "exactly one memory per
    # accepted proposal" is a constraint rather than a rule.
    Column("proposal_id", Integer, nullable=True),
    Column("conversation_id", Integer, nullable=True),
    # The model invocation that produced the fact. Through it a memory leads
    # back to the model, the prompt version and the cost (ADR-057).
    Column("ai_call_id", Integer, nullable=True),
    # How much the fact matters, as the person who accepted it judged. Ranked
    # above confidence: a person's judgement of what is worth knowing outranks a
    # machine's estimate of what is true (ADR-060).
    Column("importance", Float, nullable=False, server_default=text("0.5")),
    # When it was accepted -- not when it was proposed. This is the moment a
    # person made it true for this application.
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Bookkeeping about the fact rather than part of it, which is why these can
    # change while the memory stays immutable. Written by one statement over the
    # selected rows, so a context of twenty memories costs one write.
    Column("retrieval_count", Integer, nullable=False, server_default=text("0")),
    # Deliberately not a ranking input: ranking by it would make a retrieved
    # memory rank higher and so be retrieved again, which is a feedback loop
    # rather than a relevance signal (ADR-060).
    Column("last_retrieved_at", DateTime(timezone=True), nullable=True),
    # A timestamp, not a flag: retention has to ask "deleted before when", and a
    # boolean cannot answer that. There is no updated_at, because nothing
    # updates a memory -- correcting one means deleting it and accepting a new
    # proposal (ADR-059).
    Column("deleted_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ["account_id"],
        ["accounts.id"],
        name="fk_memories_account_id_accounts",
        ondelete="CASCADE",
    ),
    # Composite, so a memory in one account cannot be about another account's
    # contact (ADR-043). Cascading: purging a contact removes everything about
    # them (PRIVACY.md section 7).
    ForeignKeyConstraint(
        ["account_id", "contact_id"],
        ["contacts.account_id", "contacts.id"],
        name="fk_memories_account_id_contact_id_contacts",
        ondelete="CASCADE",
    ),
    # Provenance. SET NULL rather than CASCADE, and this is the one place in the
    # schema where that is right: a memory is *user-approved knowledge*, and it
    # does not stop being known because the conversation it came from was
    # deleted. What is lost is the trail, not the fact -- so the columns are
    # nullable and the row survives.
    ForeignKeyConstraint(
        ["proposal_id"],
        ["memory_proposals.id"],
        name="fk_memories_proposal_id_memory_proposals",
        ondelete="SET NULL",
    ),
    ForeignKeyConstraint(
        ["conversation_id"],
        ["conversations.id"],
        name="fk_memories_conversation_id_conversations",
        ondelete="SET NULL",
    ),
    ForeignKeyConstraint(
        ["ai_call_id"],
        ["ai_calls.id"],
        name="fk_memories_ai_call_id_ai_calls",
        ondelete="SET NULL",
    ),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint("contact_id IS NULL OR contact_id > 0", name="contact_id_positive"),
    CheckConstraint("proposal_id IS NULL OR proposal_id > 0", name="proposal_id_positive"),
    CheckConstraint(
        "conversation_id IS NULL OR conversation_id > 0", name="conversation_id_positive"
    ),
    CheckConstraint("ai_call_id IS NULL OR ai_call_id > 0", name="ai_call_id_positive"),
    CheckConstraint(
        "category IN ('identity', 'location', 'occupation', 'interest', 'preference', "
        "'relationship', 'important_date', 'plan', 'shared_experience', "
        "'open_question', 'constraint', 'other')",
        name="category_known",
    ),
    CheckConstraint("source IN ('user', 'ai_approved', 'ai_auto')", name="source_known"),
    CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_in_range"),
    CheckConstraint("length(trim(key)) > 0", name="key_not_blank"),
    CheckConstraint("length(trim(value)) > 0", name="value_not_blank"),
    CheckConstraint("deleted_at IS NULL OR deleted_at >= created_at", name="deleted_after_created"),
    CheckConstraint("importance >= 0 AND importance <= 1", name="importance_in_range"),
    CheckConstraint("retrieval_count >= 0", name="retrieval_count_not_negative"),
    # The count and the timestamp move together, so "has this ever been used"
    # has one answer however it is asked.
    CheckConstraint(
        "(retrieval_count > 0) = (last_retrieved_at IS NOT NULL)",
        name="retrieval_history_agrees",
    ),
    CheckConstraint(
        "last_retrieved_at IS NULL OR last_retrieved_at >= created_at",
        name="retrieved_after_created",
    ),
    comment=(
        "Facts a person approved for long-term retention. The other half of the "
        "lifecycle memory_proposals begins: nothing reaches this table without "
        "a decision (ADR-019, ADR-059)."
    ),
)

# One memory per accepted proposal. What makes "accepting a proposal creates
# exactly one memory" a constraint rather than a rule somebody has to keep --
# including when two decisions race. Partial, because proposal_id is nullable:
# a memory whose proposal was deleted keeps existing, and several such rows must
# remain permitted.
Index(
    "uq_memories_proposal_id",
    memories.c.proposal_id,
    unique=True,
    sqlite_where=text("proposal_id IS NOT NULL"),
    postgresql_where=text("proposal_id IS NOT NULL"),
)

# One fact per person, among the memories that still exist. Deleting a memory
# frees its key, so a fact can be accepted again after being forgotten -- which
# is the only route to a correction, since nothing edits a memory.
#
# Two indexes rather than one, because SQL treats NULLs as distinct: without the
# second, two identical facts from group conversations would both be stored.
Index(
    "uq_memories_account_id_contact_id_category_key",
    memories.c.account_id,
    memories.c.contact_id,
    memories.c.category,
    memories.c.key,
    unique=True,
    sqlite_where=text("deleted_at IS NULL AND contact_id IS NOT NULL"),
    postgresql_where=text("deleted_at IS NULL AND contact_id IS NOT NULL"),
)

Index(
    "uq_memories_account_id_category_key",
    memories.c.account_id,
    memories.c.category,
    memories.c.key,
    unique=True,
    sqlite_where=text("deleted_at IS NULL AND contact_id IS NULL"),
    postgresql_where=text("deleted_at IS NULL AND contact_id IS NULL"),
)

# The listing query: one account's memories, newest first, with id as the keyset
# tiebreaker.
Index(
    "ix_memories_account_id_created_at",
    memories.c.account_id,
    memories.c.created_at,
    memories.c.id,
)

# The retrieval query, and shaped by it rather than guessed: retrieval asks for
# one account's live memories about one contact. The leading columns are the
# whole of the WHERE clause, and created_at follows because a candidate cap
# takes the newest when it bites (ADR-060).
#
# Partial on deleted_at, so a forgotten memory occupies no space in the index a
# retrieval walks -- an account that has forgotten a lot should not pay for it
# on every context it builds.
#
# No index on importance, confidence or category: ranking happens in memory,
# over a set already bounded to one contact, and an index cannot serve a
# five-key lexicographic order anyway.
Index(
    "ix_memories_account_id_contact_id_created_at",
    memories.c.account_id,
    memories.c.contact_id,
    memories.c.created_at,
    memories.c.id,
    sqlite_where=text("deleted_at IS NULL"),
    postgresql_where=text("deleted_at IS NULL"),
)

SUGGESTIONS_TABLE: Final = "suggestions"

suggestions = Table(
    SUGGESTIONS_TABLE,
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("account_id", Integer, nullable=False),
    # What it is about. Never null: a suggestion with no chat could not be
    # reviewed in context, and every kind foreseen is about a conversation with
    # somebody.
    Column("chat_id", Integer, nullable=False),
    # The bounded episode it was drawn from, when one is known. NULL today:
    # generation reads a chat's recent messages rather than a segmented
    # conversation, so nothing yet supplies this (ADR-062).
    Column("conversation_id", Integer, nullable=True),
    # The call that produced it, and the whole of its provenance. Through it a
    # suggestion leads back to the model, the prompt version and the cost.
    Column("ai_call_id", Integer, nullable=False),
    # What kind of thing is suggested. Constrained, because a reviewer decides
    # about a title and a description whatever this says and only an executor
    # needs to know the kind -- so the set must stay closed and known.
    Column("proposal_type", String(32), nullable=False),
    # One line, for a listing.
    Column("title", String(200), nullable=False),
    # What a person reads to decide. For a reply draft, the draft itself:
    # type-agnostic on purpose, so a reviewer can decide about any kind of
    # suggestion without any code understanding that kind.
    Column("description", Text, nullable=False),
    # What a machine would need, as JSON. Read by nothing today. Deliberately
    # separate from the description: a new proposal type needs new payload
    # handling, and needs no new review (ADR-062).
    Column("payload", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Moves with the status and cannot disagree with it. No updated_at: a
    # suggestion has exactly one transition, and nothing edits one -- an edited
    # draft is not what the model suggested.
    Column("decided_at", DateTime(timezone=True), nullable=True),
    ForeignKeyConstraint(
        ["account_id"],
        ["accounts.id"],
        name="fk_suggestions_account_id_accounts",
        ondelete="CASCADE",
    ),
    # Composite, so a suggestion in one account cannot be about another
    # account's chat (ADR-043). Cascading, and here the contrast with `memories`
    # is deliberate: a memory is approved knowledge that outlives the exchange
    # it came from, but a suggestion is a draft *about* a conversation and means
    # nothing once that conversation is gone.
    ForeignKeyConstraint(
        ["account_id", "chat_id"],
        ["chats.account_id", "chats.id"],
        name="fk_suggestions_account_id_chat_id_chats",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["account_id", "conversation_id"],
        ["conversations.account_id", "conversations.id"],
        name="fk_suggestions_account_id_conversation_id_conversations",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["account_id", "ai_call_id"],
        ["ai_calls.account_id", "ai_calls.id"],
        name="fk_suggestions_account_id_ai_call_id_ai_calls",
        ondelete="CASCADE",
    ),
    CheckConstraint("id > 0", name="id_positive"),
    CheckConstraint("account_id > 0", name="account_id_positive"),
    CheckConstraint("chat_id > 0", name="chat_id_positive"),
    CheckConstraint(
        "conversation_id IS NULL OR conversation_id > 0", name="conversation_id_positive"
    ),
    CheckConstraint("ai_call_id > 0", name="ai_call_id_positive"),
    CheckConstraint("proposal_type IN ('reply_draft')", name="proposal_type_known"),
    CheckConstraint("status IN ('pending', 'accepted', 'dismissed')", name="status_known"),
    CheckConstraint("length(trim(title)) > 0", name="title_not_blank"),
    CheckConstraint("length(trim(description)) > 0", name="description_not_blank"),
    CheckConstraint("length(trim(payload)) > 0", name="payload_not_blank"),
    # The status and the timestamp move together, so "has this been decided" has
    # one answer however it is asked.
    CheckConstraint("(status = 'pending') = (decided_at IS NULL)", name="decision_has_a_time"),
    CheckConstraint("decided_at IS NULL OR decided_at >= created_at", name="decided_after_created"),
    comment=(
        "Things a model proposed doing, awaiting a person's decision. Accepting "
        "one records agreement and executes nothing: there is no executor, and "
        "no code path from this table to Telegram (ADR-062)."
    ),
)

# The review queue: one account's *pending* suggestions, newest first. Partial,
# because that is the query -- a queue is by definition what has not been
# decided, and an index carrying every decided row would grow without bound
# while serving nothing.
Index(
    "ix_suggestions_account_id_pending",
    suggestions.c.account_id,
    suggestions.c.created_at,
    suggestions.c.id,
    sqlite_where=text("status = 'pending'"),
    postgresql_where=text("status = 'pending'"),
)

# One chat's suggestions, decided or not, newest first. Not partial: reviewing a
# conversation's history means seeing what was dismissed as well as what was
# kept.
Index(
    "ix_suggestions_account_id_chat_id_created_at",
    suggestions.c.account_id,
    suggestions.c.chat_id,
    suggestions.c.created_at,
    suggestions.c.id,
)

# Keys used in schema_metadata.
KEY_CREATED_AT: Final = "created_at"
KEY_CREATED_BY_VERSION: Final = "created_by_version"
KEY_APPLICATION: Final = "application"

APPLICATION_NAME: Final = "tgassist"

__all__ = [
    "ACCOUNTS_TABLE",
    "AI_CALLS_TABLE",
    "APPLICATION_NAME",
    "CHATS_TABLE",
    "CONTACTS_TABLE",
    "CONVERSATIONS_TABLE",
    "KEY_APPLICATION",
    "KEY_CREATED_AT",
    "KEY_CREATED_BY_VERSION",
    "MEMORIES_TABLE",
    "MEMORY_PROPOSALS_TABLE",
    "MESSAGES_TABLE",
    "NAMING_CONVENTION",
    "SCHEMA_METADATA_TABLE",
    "SUGGESTIONS_TABLE",
    "SYNC_CURSORS_TABLE",
    "TELEGRAM_SESSIONS_TABLE",
    "USER_PROFILES_TABLE",
    "accounts",
    "ai_calls",
    "chats",
    "contacts",
    "conversations",
    "memories",
    "memory_proposals",
    "messages",
    "metadata",
    "schema_metadata",
    "suggestions",
    "sync_cursors",
    "telegram_sessions",
    "user_profiles",
]
