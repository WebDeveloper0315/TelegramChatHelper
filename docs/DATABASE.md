# DATABASE.md

# Telegram AI Conversation Assistant

Database Design Specification

Version: 2.0

Status: Active

Last Updated: 2026-07-28

Database Engine: SQLite (MVP, ADR-007)

Access Layer: SQLAlchemy Core + hand-written repositories (ADR-015)

Migrations: Alembic (ADR-015)

Future Support: PostgreSQL (ADR-016)

---

# 1. Design Principles

The database should be:

- Derived from `DOMAIN_MODEL.md`, never the reverse
- Normalized where practical
- Easy to migrate, back up and encrypt
- Fast to query under the access patterns in §20
- Portable to PostgreSQL without rewriting repositories

Business logic never depends on SQL implementation details. All access goes through repositories (ADR-004), which return domain objects and never rows.

**This document is derived from `DOMAIN_MODEL.md`.** Where the two disagree, the domain model is correct.

---

# 2. Conventions

| Concern | Rule |
|---|---|
| Table names | `snake_case`, plural |
| Primary keys | `id INTEGER PRIMARY KEY` (64-bit rowid alias); portable to `BIGSERIAL` |
| External identifiers | Prefixed `telegram_`; never used as primary keys |
| Timestamps | UTC ISO-8601 text in SQLite → `TIMESTAMPTZ` in PostgreSQL. Never local time, never naive |
| Booleans | `INTEGER` 0/1 in SQLite → `BOOLEAN` in PostgreSQL |
| Enumerations | `TEXT` with a `CHECK` constraint; the constraint is the schema-level copy of a domain enum |
| JSON payloads | `TEXT` containing JSON; validated by the application, never queried with SQLite-specific JSON operators in portable code |
| Soft delete | `deleted_at TIMESTAMP NULL` |
| Audit columns | `created_at NOT NULL`, `updated_at NOT NULL` where the row is mutable |
| Ownership | `account_id` on every account-owned table (ADR-016, multi-account readiness) |
| Foreign keys | Always declared, always enforced (`PRAGMA foreign_keys=ON`) |

**Connection configuration**, applied to every connection:

```
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
```

Pragmas are **read back after connecting**, not assumed. A pragma that silently failed to apply -- `journal_mode=WAL` on a filesystem without shared-memory support, for instance -- looks identical to one that worked, right up until concurrent access starts corrupting data.

Writes are serialized through a single dedicated thread (ADR-013), so `SQLITE_BUSY` should never occur in normal operation; the busy timeout exists for backup and maintenance overlap.

## Connection and transaction model

Established by ADR-034, which records the consequences the threading decision implies:

1. **Exactly one connection**, held for the process lifetime, via `StaticPool`. One thread implies one connection, and an in-memory database lives inside its connection, so a second connection would be a second, empty database.
2. **Units of work serialize on a lock.** One connection holds one transaction, so a second concurrent use case waits rather than failing. Acquisition is bounded at 30 seconds, turning the pathological overlapping-transaction case into a named error rather than a hang.
3. **Every read outside a unit of work releases its autobegun transaction.** SQLAlchemy 2.0 opens a transaction on any statement; on a connection held for the process lifetime that would block the next explicit `begin()`.
4. **Reads serialize behind writes.** This forfeits WAL's concurrent-reader capability and is the model's significant limitation. ADR-034 states exactly what must be measured at Milestone 13 before a reader pool is adopted.

---

# 3. Entity Relationship Diagram

```mermaid
erDiagram
    accounts ||--|| user_profiles : has
    accounts ||--o| telegram_sessions : authenticates
    accounts ||--o{ chats : owns
    accounts ||--o{ contacts : knows
    accounts ||--o{ notifications : receives
    accounts ||--o{ audit_log : records
    accounts ||--o{ retention_policies : configures
    accounts ||--o{ settings : stores
    accounts ||--o{ ai_calls : bills

    contacts ||--o{ memories : has
    contacts ||--o{ memory_proposals : pending
    contacts ||--o{ goals : pursues
    contacts ||--|| relationship_profiles : measured
    contacts ||--o{ style_profiles : writes
    contacts ||--o| chats : "private chat"

    chats ||--o{ messages : contains
    chats ||--o{ conversations : segmented
    chats ||--|| sync_cursors : tracked

    conversations ||--o{ messages : spans
    conversations ||--o{ conversation_summaries : summarized
    conversations ||--o{ conversation_plans : planned
    conversations ||--o{ reply_suggestions : suggests
    conversations ||--o{ behavior_recommendations : paced

    messages ||--o{ attachments : carries
    messages ||--o{ analyses : analyzed
    messages ||--o{ memory_proposals : sources
    messages ||--o{ reply_suggestions : "replied to"

    memories ||--o{ memory_revisions : revised
    memory_proposals ||--o{ memory_revisions : produces

    embeddings }o--|| embedding_models : uses

    plugins ||--o{ plugin_data : stores

    accounts {
        int id PK
        int telegram_user_id UK
        text phone_number_hash
        text display_name
        text timezone
        int is_active
        timestamp created_at
        timestamp updated_at
        timestamp last_authenticated_at
    }

    user_profiles {
        int account_id PK
        text primary_language
        text tone_preference
        text preferred_message_length
        text emoji_usage
        int quiet_hours_start_minute
        int quiet_hours_end_minute
        timestamp created_at
        timestamp updated_at
    }

    telegram_sessions {
        int account_id PK
        text authorization_state
        text connection_state
        text session_path
        text encryption_key_ref
        text client_version
        timestamp connected_at
        timestamp last_activity_at
        timestamp created_at
        timestamp updated_at
    }

    contacts {
        int id PK
        int account_id FK
        int telegram_user_id
        text username
        text display_name
        timestamp archived_at
        timestamp deleted_at
        timestamp created_at
        timestamp updated_at
    }

    chats {
        int id PK
        int account_id FK
        int telegram_chat_id
        text chat_type
        int contact_id FK
        text title
        int sync_enabled
        text ai_processing_mode
        timestamp created_at
        timestamp updated_at
    }

    conversations {
        int id PK
        int account_id FK
        int chat_id FK
        timestamp started_at
        timestamp ended_at
        int message_count
        timestamp created_at
        timestamp updated_at
    }

    messages {
        int id PK
        int account_id FK
        int chat_id FK
        int conversation_id FK
        int telegram_message_id
        text sender_kind
        int is_outgoing
        text message_type
        text text
        int reply_to_message_id FK
        timestamp sent_at
        timestamp edited_at
        timestamp ingested_at
        int is_deleted_remotely
        timestamp deleted_at
    }

    attachments {
        int id PK
        int message_id FK
        text attachment_type
        text filename
        text mime_type
        int size_bytes
        text storage_path
        int is_downloaded
    }

    memories {
        int id PK
        int account_id FK
        int contact_id FK
        text category
        text key
        text value
        real confidence
        real importance
        text provenance
        int source_message_id FK
        int is_pinned
        timestamp last_retrieved_at
        timestamp deleted_at
    }

    memory_proposals {
        int id PK
        int account_id FK
        int contact_id FK
        text category
        text key
        text value
        real confidence
        int source_message_id FK
        text prompt_version
        text model_identifier
        text status
        int conflicts_with_memory_id FK
        timestamp decided_at
    }

    memory_revisions {
        int id PK
        int memory_id FK
        text previous_value
        text new_value
        text reason
        text changed_by
        int source_proposal_id FK
        timestamp created_at
    }

    goals {
        int id PK
        int account_id FK
        int contact_id FK
        text goal_type
        text title
        text description
        int priority
        text status
        timestamp target_date
        timestamp deleted_at
    }

    relationship_profiles {
        int id PK
        int account_id FK
        int contact_id FK UK
        real interaction_frequency
        real reciprocity_ratio
        int median_response_time_operator
        int median_response_time_contact
        real conversation_depth
        text engagement_trend
        int sample_size
        timestamp computed_at
    }

    style_profiles {
        int id PK
        int account_id FK
        text owner_kind
        int contact_id FK
        int median_message_length
        text formality
        real emoji_rate
        real question_rate
        int sample_size
        timestamp computed_at
    }

    conversation_summaries {
        int id PK
        int account_id FK
        int conversation_id FK
        text summary_text
        text key_topics
        text open_questions
        int first_message_id FK
        int last_message_id FK
        text prompt_version
        text model_identifier
        int analysis_version
        timestamp superseded_at
    }

    conversation_plans {
        int id PK
        int account_id FK
        int conversation_id FK
        text objective
        text topics_to_introduce
        text topics_to_avoid
        text reasoning
        real confidence
        text prompt_version
        int is_stale
    }

    reply_suggestions {
        int id PK
        int account_id FK
        int conversation_id FK
        int in_reply_to_message_id FK
        text primary_text
        text alternatives
        text reasoning
        real confidence
        text recommended_action
        text context_snapshot
        int plan_id FK
        text prompt_version
        text status
        int sent_message_id FK
        timestamp decided_at
    }

    behavior_recommendations {
        int id PK
        int account_id FK
        int conversation_id FK
        int suggested_delay_seconds
        timestamp suggested_send_at
        text rationale
        text suggested_length
        int should_split
        text rule_version
    }

    analyses {
        int id PK
        int account_id FK
        text subject_kind
        int subject_id
        text analysis_type
        text result
        real confidence
        int analysis_version
        text prompt_version
        text model_identifier
        text input_fingerprint
    }

    embeddings {
        int id PK
        int account_id FK
        text owner_kind
        int owner_id
        int embedding_model_id FK
        blob vector
        int dimension
        text content_fingerprint
        timestamp created_at
    }

    embedding_models {
        int id PK
        text provider
        text model_name
        int dimension
        text normalization
        int is_active
    }

    sync_cursors {
        int chat_id PK
        int account_id FK
        int oldest_synced_message_id
        int newest_synced_message_id
        int backfill_complete
        timestamp backfill_horizon
        timestamp last_sync_at
        timestamp updated_at
    }

    notifications {
        int id PK
        int account_id FK
        text notification_type
        text severity
        text title
        text body
        int is_read
        int is_dismissed
        timestamp created_at
    }

    ai_providers {
        int id PK
        text provider_name
        text provider_kind
        text model_identifier
        text endpoint
        text api_key_ref
        text capabilities
        int context_window_tokens
        text data_boundary
        int is_enabled
        int priority
    }

    ai_calls {
        int id PK
        int account_id FK
        int chat_id FK
        text vendor
        text model_identifier
        text data_boundary
        text prompt_id
        text prompt_version
        text task_kind
        int input_tokens
        int output_tokens
        text estimated_cost
        text cost_currency
        int latency_ms
        text outcome
        text finish_reason
        text response_digest
        text response_text
        timestamp created_at
    }

    plugins {
        int id PK
        text plugin_name UK
        text version
        text api_version_range
        text entry_point
        int is_enabled
        text declared_permissions
        text last_error
    }

    plugin_data {
        int id PK
        int plugin_id FK
        text key
        text value
        timestamp updated_at
    }

    settings {
        int id PK
        int account_id FK
        text key
        text value_json
        timestamp updated_at
    }

    retention_policies {
        int id PK
        int account_id FK
        text scope
        int chat_id FK
        int retention_days
        text action
        int is_enabled
        timestamp last_applied_at
    }

    audit_log {
        int id PK
        int account_id FK
        text event_type
        text actor
        text summary
        text related_entity_kind
        int related_entity_id
        timestamp created_at
    }
```

---

# 4. Table Reference

Tables are grouped by concern. For each: purpose, notable columns, constraints and indexes. Column lists mirror `DOMAIN_MODEL.md` §5 and are not repeated exhaustively where the ER diagram already shows them.

## 4.1 Identity and Session

### `accounts`

Ownership root for every other account-scoped table. Created by migration `0002` so multi-account support (`PROJECT_SPEC.md` §4.11) never requires a breaking migration.

Columns: `id`, `telegram_user_id`, `display_name`, `timezone`, `is_active`, `created_at`, `updated_at`.

- Primary key `id` is **not autoincrement**: identifiers come from the application's generator, so an account can be fully constructed and validated before it is saved rather than acquiring its identity as a side effect of insert.
- Unique index: `telegram_user_id`
- **Partial unique index** `uq_accounts_single_active` on `is_active WHERE is_active = 1`. This is what makes the single-active invariant structural: many inactive rows are permitted and at most one active one, so a second activation fails at the database rather than depending on every caller remembering to deactivate first. Declared with both `sqlite_where` and `postgresql_where` so the same index exists on either dialect (ADR-016).
- Check constraints: `id > 0`, `telegram_user_id > 0`, `length(trim(display_name)) > 0`, `updated_at >= created_at`. These restate the entity's invariants so a row written by any other route cannot violate them.
- `phone_number_hash` and `last_authenticated_at` arrive with Milestone 2 (ADR-037).

### `user_profiles`

The operator's reply preferences, one row per account. Created by migration `0003`.

Columns: `account_id`, `primary_language`, `tone_preference`, `preferred_message_length`, `emoji_usage`, `quiet_hours_start_minute`, `quiet_hours_end_minute`, `created_at`, `updated_at`.

- **`account_id` is both the primary key and the foreign key** (ADR-038). One profile per account is therefore structural rather than a separate unique index, and no additional index on `account_id` exists — the primary key already serves every lookup this table has.
- FK: `account_id → accounts(id) ON DELETE CASCADE`. Deleting an account removes its profile without application code having to remember. SQLite enforces this only with `PRAGMA foreign_keys = ON`, which the engine applies per connection; a test asserts the pragma is on, because without it the cascade would be decorative.
- Every column is `NOT NULL` with a server default, so the table has no nullable column standing in for "not decided yet". A preference always has a value; a default is a value.
- Quiet hours are stored as **two integer minute offsets** rather than as times or text. A quiet period normally wraps midnight, and `22:00–08:00` compared as a pair of naive times looks empty. Integers compare correctly and need no parsing.
- Enumerations are stored as their **string values**, not ordinals: an ordinal silently changes meaning if a member is inserted mid-enum, and it makes the file unreadable to anyone opening it.
- Check constraints: `account_id > 0`; `tone_preference IN ('casual','neutral','formal','mirror_contact')`; `preferred_message_length IN ('short','medium','long')`; `emoji_usage IN ('none','sparing','frequent')`; both minute columns within `[0, 1440)`; `quiet_hours_start_minute <> quiet_hours_end_minute`; `updated_at >= created_at`.
- Access is through a repository **scoped at construction** (ADR-039): no method accepts an account identifier, so no query can be issued without its scope.

### `telegram_sessions`

Where an account stands with Telegram, and where its encrypted local store
lives. Created by migration `0007`.

Columns: `account_id`, `authorization_state`, `connection_state`,
`session_path`, `encryption_key_ref`, `client_version`, `connected_at`,
`last_activity_at`, `created_at`, `updated_at`.

- **`account_id` is both the primary key and the foreign key**, as in `user_profiles`. One session per account is therefore structural rather than a separate unique index, and no additional index exists — the primary key serves every lookup this table has.
- FK: `account_id → accounts(id) ON DELETE CASCADE`. A session cannot outlive its account.
- **Two state columns, not one** (ADR-049). Version 1.0 of this document specified a single `state`; TDLib reports authorization and connection separately and they vary independently, so one column cannot express *authorized but reconnecting*.
- Check: `authorization_state IN ('unauthorized','waiting_phone','waiting_code','waiting_password','ready','logged_out')`.
- Check: `connection_state IN ('offline','connecting','updating','ready','waiting_for_network')`. Two tests assert that every member of each enumeration is storable, because the enums and these constraints would otherwise drift apart silently.
- Check: `connection_state IN ('updating','ready') OR connected_at IS NULL` — a session that is not connected cannot carry a connection time. The socket is up from `updating` onwards, which is why that state counts as connected.
- Check: `account_id > 0`; `length(trim(session_path)) > 0`; `length(trim(encryption_key_ref)) > 0`; `updated_at >= created_at`.
- `client_version` records which TDLib last wrote the store, because a store written by a newer TDLib may not be readable by an older one. Nullable: nothing has written the store before the first login.
- `encryption_key_ref` holds a `SecretStore` **name** only (ADR-021), capped at 128 characters to match the entity. The key itself lives in the operating system credential store. **A key value in this column is a security defect**, and there is no column it could hide in — a test asserts that neither the table nor the entity has a field a key would fit.
- Access is through a repository **scoped at construction** (ADR-039), with no `delete`: a session goes with its account, by cascade, and logging out is a transition rather than a deletion.
- A downgrade drops the row, not the store on disk. Deleting a user's encrypted session directory is not a schema change's business; re-authentication is the recovery.

## 4.2 People and Chats

### `contacts`

People known to an account. Created by migration `0004`, and the first table
with many rows per account -- so the first whose indexes are chosen to serve
queries rather than only to enforce constraints.

Columns: `id`, `account_id`, `telegram_user_id`, `username`, `display_name`,
`archived_at`, `deleted_at`, `created_at`, `updated_at`.

- Primary key `id` is **locally generated, not the Telegram identifier** and not autoincrement (ADR-041). The same person can be known to two accounts, so `telegram_user_id` is not unique here; a natural key would have to be the pair, and every child table would carry both columns.
- **Unique: `(account_id, telegram_user_id)`** — the documented invariant, made structural. Deliberately **not partial**: it covers soft-deleted rows, because a deleted contact still holds that person's history and a second row for them would split it. Re-adding a deleted contact is therefore refused and the caller is told to restore instead.
- Index: `(account_id, created_at, id)` — the listing query, scoped and ordered with the keyset tiebreaker. `account_id` leads because every query this table serves is account-scoped, so an index that does not lead with it serves nothing.
- FK: `account_id → accounts(id) ON DELETE CASCADE`.
- `username` is the one genuinely nullable column: many Telegram users have never set one, so null means "has none" rather than "not decided yet".
- Soft delete: `deleted_at`. Archive: `archived_at`. Mutually exclusive, enforced by a check constraint rather than by convention (ADR-042).
- Check constraints: `id > 0`, `account_id > 0`, `telegram_user_id > 0`, `length(trim(display_name)) > 0`, `username IS NULL OR length(username) BETWEEN 5 AND 32`, `updated_at >= created_at`, `archived_at IS NULL OR deleted_at IS NULL`.
- **No index on `(account_id, username)`** yet: nothing looks a contact up by handle. It arrives with the search that needs it, measured against `DATABASE.md` §20 rather than added on the assumption that it will be wanted.
- Access is through a repository **scoped at construction** (ADR-039).
- **Written by synchronisation from Milestone 2.7**, which touches only `username`, `display_name` and `updated_at` — the fields Telegram owns. It never writes `archived_at` or `deleted_at`, and it never clears one: a contact the operator deleted is not resurrected by anything Telegram says (ADR-053). The unique index over soft-deleted rows is what makes that decision enforceable rather than merely intended.

### `chats`

The communication graph's edge: the container joining an account to a contact.
Created by migration `0005`, which also adds the `contacts (account_id, id)`
index its foreign key needs.

Columns: `id`, `account_id`, `telegram_chat_id`, `chat_type`, `contact_id`,
`title`, `sync_enabled`, `ai_processing_mode`, `created_at`, `updated_at`.

- Primary key `id` is locally generated, not the Telegram identifier (ADR-041).
- **Unique: `(account_id, telegram_chat_id)`.** Two accounts may record the same Telegram chat — both can be in one group — but one account may not record it twice.
- **Partial unique: `(account_id, contact_id) WHERE contact_id IS NOT NULL`.** A contact has at most one private chat. Partial because `contact_id` is null for every other kind, and many nulls must remain permitted.
- Index: `(account_id, created_at, id)` — the listing query and its keyset tiebreaker.
- FK: `account_id → accounts(id) ON DELETE CASCADE`.
- **FK: `(account_id, contact_id) → contacts(account_id, id) ON DELETE CASCADE`** — composite, and this is the point. A simple `contact_id → contacts(id)` would permit a chat in one account to name a contact in another; requiring the pair to exist together makes that unrepresentable (ADR-043). It cascades rather than setting null: `PRIVACY.md` §7 requires a contact purge to remove everything referencing them, and nulling the column would violate the private-chat invariant below.
- Check: `telegram_chat_id <> 0` — **not** `> 0`. Telegram numbers groups and channels below zero.
- Check: `chat_type IN ('private','group','supergroup','channel','saved')`
- Check: `ai_processing_mode IN ('disabled','local_only','cloud_allowed')`, default `'local_only'`
- Check: `(chat_type = 'private') = (contact_id IS NOT NULL)` and `(chat_type <> 'private') = (title IS NOT NULL)` — both directions, so neither a private chat with nobody in it nor a group chat claiming a single counterpart can be written.
- Check: `id > 0`, `account_id > 0`, `contact_id IS NULL OR contact_id > 0`, `title IS NULL OR length(trim(title)) > 0`, `updated_at >= created_at`.
- **No index on `(account_id, last_message_at)` or `(account_id, sync_enabled)`** yet: neither column exists, and both arrive with the query that needs them (Milestone 3).

- **Written by synchronisation from Milestone 2.7**, which sets `sync_enabled` and `ai_processing_mode` when a chat is first discovered and never afterwards. Both are the operator's, and a run that rewrote `ai_processing_mode` would be a privacy defect rather than a bug (ADR-053). The only column synchronisation updates on an existing row is `title`, and never on a private chat, whose name belongs to its contact.
- **The `saved` chat type is reached from Milestone 2.7.** Telegram's Saved Messages is a private chat with the operator, which the composite foreign key and the operator-identity rule together make unstorable as one (ADR-052).
- **One transaction writes a `contacts` row and its `chats` row together.** The composite foreign key makes the pair the atomic unit: a private chat cannot be inserted before the contact it names exists, so an interrupted run must leave neither rather than the first.

**Referencing this table.** Every later table in the graph — messages first — should reference `(account_id, chat_id)` compositely, and needs a `chats (account_id, id)` index added in the migration that creates it (ADR-043).

### `conversations`

Bounded episodes of interaction, **derived from messages**. Created by migration
`0009`, and the only table in this schema whose rows this application deletes as
a matter of course: a stale conversation is a wrong answer about messages that
are still there, not history.

Columns: `id`, `account_id`, `chat_id`, `started_at`, `ended_at`,
`message_count`, `created_at`, `updated_at`.

- **Messages carry no `conversation_id`, and there is no join table.** A message belongs to the conversation whose `[started_at, ended_at]` contains its `sent_at`. `messages` is append-only and its repository has no update path (ADR-046); storing the link would reopen that, and would make every rebuild an O(messages) write instead of an O(conversations) one (ADR-056).
- **FK: `(account_id, chat_id) → chats(account_id, id) ON DELETE CASCADE`** — composite, so a conversation in one account's chat cannot be attached to another's (ADR-043). It reuses the `uq_chats_account_id_id` index migration `0005` created.
- **Unique: `(account_id, chat_id, started_at)`.** This *is* the non-overlap guarantee: each conversation is a contiguous run, so two beginning at the same instant is the only way two could overlap. It replaces version 1.0's partial unique index on `is_open`.
- Index: `(account_id, chat_id, started_at, id)` — the listing, and the keyset tiebreaker.
- Check: `ended_at >= started_at`, and `ended_at` is **not nullable**. A conversation derived from messages that already exist always has a last one; version 1.0 made it nullable to mean "still open", and openness is now asked of the entity against an instant rather than stored (ADR-056).
- Check: `message_count > 0`. An empty conversation is a row that should have been deleted, and permitting it would let a segmentation bug survive as data.
- **No `is_open`, `initiated_by` or `dominant_language`.** The first is derived; the other two are deferred to the milestones that read them.
- **A requirement on whatever references this table next.** Summaries, plans and analyses attach to a Conversation from Milestone 8, and these rows are deleted whenever re-segmentation leaves one describing no messages. Those foreign keys must cascade, or that delete has to start refusing.

### `messages`

The immutable factual record, and the largest table. Created by migration
`0006`, which also adds the `chats (account_id, id)` index its foreign key
needs. Every index here is justified by an access pattern in §20.

Columns: `id`, `account_id`, `chat_id`, `telegram_message_id`, `sender_kind`,
`message_type`, `text`, `sent_at`, `ingested_at`.

- **No `updated_at` and no `deleted_at`.** The table is append-only; the absence of the columns is the statement (ADR-046).
- **Partial unique: `(account_id, chat_id, telegram_message_id) WHERE telegram_message_id IS NOT NULL`** — the idempotency guarantee for re-synchronisation. Partial because ingestion is source-agnostic and only Telegram issues identifiers; a non-partial index would reject the second message from every other source (ADR-045).
- Index: `(account_id, chat_id, sent_at, id)` — the history query, ordered by when a message was **sent** rather than ingested, because a backfill inserts old messages after new ones. `id` is the keyset tiebreaker.
- **FK: `(account_id, chat_id) → chats(account_id, id) ON DELETE CASCADE`** — composite, so a message cannot be filed in another account's chat (ADR-043). Deleting a contact therefore reaches messages through two cascades, which is what `PRIVACY.md` §7 promises.
- Check: `sender_kind IN ('operator','contact','system')`
- Check: `message_type IN ('text','photo','voice','video','document','sticker','location','poll','service','other')`
- Check: `message_type <> 'text' OR (text IS NOT NULL AND length(trim(text)) > 0)` — a text message has text; every other kind may have a caption or nothing.
- Check: `id > 0`, `account_id > 0`, `chat_id > 0`, `telegram_message_id IS NULL OR telegram_message_id > 0`
- **Not yet present:** the `(conversation_id, sent_at)`, `(chat_id, is_outgoing, sent_at DESC)` and `(account_id, sent_at DESC)` indexes. None of those columns or queries exists; each index arrives with the feature that reads it, measured rather than assumed.

`text` is conversation content and is redacted from logs by the sensitivity
policy (`SECURITY.md` §9). The bare key `text` is matched **exactly** rather than
as a fragment, because `context` — a structural key on every application error —
contains it.

### `messages_fts`

FTS5 virtual table over `messages.text`, kept in sync by triggers, supporting `MessageSearchPort`. Isolated in a SQLite-specific migration; the PostgreSQL implementation uses `tsvector` (ADR-016 §4).

### `attachments`

- Index: `message_id`
- FK: `message_id → messages(id) ON DELETE CASCADE`
- Check: `storage_path IS NOT NULL OR is_downloaded = 0`

## 4.3 Knowledge

### `memories`

- **Unique (partial): `(account_id, contact_id, category, key) WHERE deleted_at IS NULL`** — the deduplication constraint
- Index: `(contact_id, importance DESC)`, `(contact_id, last_retrieved_at DESC)`, `(account_id, category)`
- FK: `contact_id → contacts(id) ON DELETE CASCADE`, `source_message_id → messages(id) ON DELETE SET NULL`
- Check: `provenance IN ('USER','AI_APPROVED','AI_AUTO','IMPORTED')`
- Check: `confidence BETWEEN 0 AND 1`, `importance BETWEEN 0 AND 1`
- Check: `provenance NOT IN ('AI_APPROVED','AI_AUTO') OR source_message_id IS NOT NULL`

### `memory_proposals`

- Index: `(account_id, status, created_at)` — the review queue
- Index: `(contact_id, status)`
- FK: `contact_id → contacts(id) ON DELETE CASCADE`, `source_message_id → messages(id) ON DELETE SET NULL`, `conflicts_with_memory_id → memories(id) ON DELETE SET NULL`
- Check: `status IN ('pending','approved','rejected','superseded','expired')`

### `memory_revisions`

Append-only.

- Index: `(memory_id, created_at DESC)`
- FK: `memory_id → memories(id) ON DELETE CASCADE`, `source_proposal_id → memory_proposals(id) ON DELETE SET NULL`
- Check: `reason IN ('user_edit','superseded_by_proposal','merge','correction')`

### `goals`

- **Partial unique: `(account_id, contact_id) WHERE status = 'active' AND deleted_at IS NULL`** — enforces one active goal per contact
- Index: `(contact_id, status)`
- FK: `contact_id → contacts(id) ON DELETE CASCADE`
- Check: `status IN ('active','paused','achieved','abandoned')`

### `relationship_profiles`

- Unique: `contact_id`
- FK: `contact_id → contacts(id) ON DELETE CASCADE`
- Check: `engagement_trend IN ('rising','stable','declining','insufficient_data')`

### `style_profiles`

- Unique: `(account_id, owner_kind, contact_id)`
- Check: `(owner_kind = 'contact') = (contact_id IS NOT NULL)`
- Check: `formality IN ('informal','neutral','formal')`

## 4.4 AI Artifacts

### `conversation_summaries`

- **Partial unique: `conversation_id WHERE superseded_at IS NULL`**
- Index: `(conversation_id, created_at DESC)`
- FK: `conversation_id → conversations(id) ON DELETE CASCADE`

### `conversation_plans`

- Index: `(conversation_id, created_at DESC)`
- FK: `conversation_id → conversations(id) ON DELETE CASCADE`

### `reply_suggestions`

- Index: `(conversation_id, created_at DESC)`, `(account_id, status)`
- FK: `conversation_id → conversations(id) ON DELETE CASCADE`, `plan_id → conversation_plans(id) ON DELETE SET NULL`, `sent_message_id → messages(id) ON DELETE SET NULL`
- Check: `status IN ('offered','accepted','edited_and_sent','dismissed','expired')`
- Check: `recommended_action IN ('send','review','clarify','write_manually','wait')`
- `context_snapshot` stores identifiers of the memories, summary and messages used — **not their content**, which would duplicate data and complicate deletion.

### `suggestions`

Things a model proposed, awaiting a person's decision. **Nothing in this
application acts on a row here** — accepting one records agreement and executes
nothing (ADR-062). Created by migration `0014`.

- FKs: `account_id → accounts(id)`, and **composite** `(account_id, chat_id) →
  chats`, `(account_id, conversation_id) → conversations`, `(account_id,
  ai_call_id) → ai_calls`, so a suggestion cannot be about another account's
  chat (ADR-043). **All `ON DELETE CASCADE`, and the contrast with `memories` is
  deliberate**: a memory is approved knowledge that outlives the exchange it came
  from, so its provenance is `SET NULL`; a suggestion is a draft *about* a
  conversation and means nothing once that conversation is gone.
- **Partial index `(account_id, created_at, id) WHERE status = 'pending'`** for
  the queue. A queue is by definition what has not been decided, and an index
  carrying every decided row would grow without bound while serving nothing.
- Index `(account_id, chat_id, created_at, id)`, **not** partial: reviewing a
  conversation means seeing what was dismissed as well as what was kept.
- Check: `status IN ('pending','accepted','dismissed')`. No other states —
  `superseded` and `expired` are transitions, and this aggregate has exactly one.
- Check: `proposal_type IN ('reply_draft')`.
- Check: `(status = 'pending') = (decided_at IS NULL)` and `decided_at >=
  created_at`. **No `updated_at`**: nothing edits a suggestion, because an edited
  draft is not what the model suggested, and the record exists to say what it
  suggested when somebody agreed with it.
- `chat_id` is **NOT NULL**; `conversation_id` is nullable and **has no writer
  today** — generation reads a chat's recent messages rather than a segmented
  conversation, so it is always NULL. Stated here rather than discovered later.
- `description` is what a person reads; `payload` is JSON for a machine that
  does not exist yet. Nothing reads `payload`, and nothing validates it beyond
  its being a JSON object.
- The single mutation is conditional — `UPDATE ... WHERE status = 'pending'` —
  so a second decision changes no row even if two arrive at once.

### `behavior_recommendations`

- Index: `(conversation_id, created_at DESC)`
- FK: `conversation_id → conversations(id) ON DELETE CASCADE`

### `analyses`

Replaces the v1.0 `ai_analysis` table, generalised to both messages and conversations.

- **Unique: `(subject_kind, subject_id, analysis_type, analysis_version)`**
- Index: `(subject_kind, subject_id)`
- Check: `subject_kind IN ('message','conversation')`
- Check: `analysis_type IN ('emotion','intent','topic','question_detection','stage','composite')`
- `input_fingerprint` is a content hash; a mismatch invalidates the cached entry (ADR-029 §5).
- Polymorphic `subject_id` cannot carry a foreign key. Referential integrity is maintained by the application and enforced by a nightly integrity job (§18); this trade-off is accepted deliberately to avoid two near-identical tables.

## 4.5 Vector Storage

### `embedding_models`

Registry of embedding models ever used, so vectors from different models are never compared (ADR-018 §5).

- Unique: `(provider, model_name)`
- Columns: `dimension`, `normalization` (`l2` | `none`), `is_active`

### `embeddings`

- **Unique: `(owner_kind, owner_id, embedding_model_id)`**
- Index: `(account_id, embedding_model_id)` — loads the active matrix
- Check: `owner_kind IN ('memory','summary','message','conversation')`
- FK: `embedding_model_id → embedding_models(id) ON DELETE RESTRICT`
- `vector` is a `BLOB` of `dimension` little-endian float32 values. `content_fingerprint` detects staleness when the source text changes.
- Strategy and retrieval algorithm are specified in `VECTOR_SEARCH.md`.

## 4.6 Operations

### `sync_cursors`

How far each chat's history backfill has got. Created by migration `0008`, and
the only table whose *write timing* is the feature: it is written in the same
transaction as the messages it accounts for, which is the whole of what makes an
interrupted backfill resumable (ADR-050).

Columns: `chat_id`, `account_id`, `oldest_synced_message_id`,
`newest_synced_message_id`, `backfill_complete`, `backfill_horizon`,
`last_sync_at`, `updated_at`.

- **Primary key `chat_id`**, not a surrogate beside a unique index. Exactly one cursor per chat, so the invariant is the key — the reasoning ADR-038 applied to `user_profiles` and `0007` applied to `telegram_sessions`. This supersedes the surrogate `id` in the diagram above (ADR-054).
- **FK: `(account_id, chat_id) → chats(account_id, id) ON DELETE CASCADE`** — composite, so a cursor for one account's chat cannot be attached to another's (ADR-043). It reuses the `uq_chats_account_id_id` index migration `0005` created for exactly this, so no new index on `chats` was needed.
- `oldest_synced_message_id` is where the next fetch continues from. **A Telegram message identifier, not a timestamp**: Telegram pages history by identifier, and identifiers are unique and totally ordered within a chat, which timestamps are not (ADR-054). NULL means nothing is stored yet, which is also what "start at the newest" means to the gateway.
- Check: `(oldest_synced_message_id IS NULL) = (newest_synced_message_id IS NULL)` — both ends of the range or neither. A floor with no ceiling describes a range whose extent nobody can state.
- Check: `oldest_synced_message_id IS NULL OR oldest_synced_message_id <= newest_synced_message_id`.
- Check: `chat_id > 0`, `account_id > 0`, and both identifiers positive when present.
- `backfill_horizon` records what `backfill_complete` *meant*. The two are read together: a run configured to reach further back reopens the cursor rather than reporting success (ADR-054).
- **No index beyond the primary key.** The only query is by chat, which the key serves. The scheduler's "which chats are pending" query arrives with the scheduler, and its index should be chosen by it (§20).
- **No `created_at`**: nothing asks when a chat was first synchronised, and `last_sync_at` is the time that means something.
- `consecutive_failures` and `last_error` from `DOMAIN_MODEL.md` §5.22 are **not** created. The first drives backoff and notifications, neither of which exists; the second was dropped by ADR-050.
- Access is through a repository **scoped at construction** (ADR-039), and that repository never commits — a cursor committed on its own would advance past messages that had not been written.

### `notifications`

- Index: `(account_id, is_dismissed, created_at DESC)`
- Check: `severity IN ('info','warning','error','action_required')`

### `ai_providers`

- Unique: `(provider_name, model_identifier)`
- Check: `data_boundary IN ('local','external')`
- Check: `provider_kind IN ('cloud_llm','local_llm','cloud_embedding','local_embedding')`
- `api_key_ref` is a `SecretStore` name only.

### `ai_calls`

One row per model invocation, including the ones that failed and the ones that
were refused. Created by migration `0010`.

- Index: `(account_id, created_at DESC)` -- the only order a cost report is read
  in. No index on the vendor: one provider is configured at a time, so an index
  on it would have one distinct value.
- Foreign keys: `account_id -> accounts` (cascade), and a **composite**
  `(account_id, chat_id) -> chats(account_id, id)` (cascade). Composite so a
  call in one account cannot name another account's chat (ADR-043); cascading
  because a record derived from a deleted chat is residue of that chat, and
  because SET NULL is not available on a composite key -- it nulls every column
  in it, including the NOT NULL `account_id` (ADR-057 §10).
- **No `UPDATE` and no `DELETE` path exists in any repository.** Append-only,
  the same discipline `messages` has (ADR-046, ADR-057 §5).
- **Contains no prompt content, under any setting** (`SECURITY.md` §9).
  `response_digest` is a truncated SHA-256, which is what deterministic replay
  compares; `response_text` is null unless `ai.store_responses` is on, which the
  production profile refuses (ADR-057 §6).
- `estimated_cost` is **TEXT, not REAL**. Money in fractions of a cent summed
  over many rows is exactly where binary floating point drifts. Check:
  `(estimated_cost IS NULL) = (cost_currency IS NULL)`.
- `input_tokens` / `output_tokens` are nullable, and NULL means *unreported* --
  which is not zero. Zero is a claim that a call was free.
- Check: `(outcome = 'success') = (finish_reason IS NOT NULL)`. A successful
  call is one the model answered, so it knows why the model stopped; a failed
  one never got that far.
- Check: `response_text IS NULL OR response_digest IS NOT NULL`.
- Check: `data_boundary IN ('local','external')`, and `outcome` restricted to
  the seven values in `DOMAIN_MODEL.md` §5.25.
- No `updated_at`. An AI call is an immutable record of an instant, and the
  absence of the column is what says so.
- Subject to log retention, not conversation retention

### `memory_proposals`

One row per candidate fact a model extracted and a person has not yet decided
about. **Nothing here is believed.** Created by migration `0011`.

- Index: `(account_id, created_at DESC)` — the review queue's order.
- **Unique: `(account_id, conversation_id, category, value)`.** Re-running
  extraction over a conversation must cost nothing and change nothing. The
  application checks for duplicates first; this index is what makes that true
  rather than usually true, because the read and the write are separate
  transactions (ADR-058 §9).
- Foreign keys: `account_id -> accounts` (cascade), and **two composite keys** —
  `(account_id, conversation_id) -> conversations` and
  `(account_id, ai_call_id) -> ai_calls`, both cascading. Composite so a
  proposal cannot cite another account's conversation or audit trail (ADR-043);
  cascading because a claim about a deleted conversation is residue of it, and
  because a proposal whose provenance had been deleted would be a fact with no
  visible origin — the state proposals exist to prevent.
- **No `UPDATE` and no `DELETE` path exists in any repository.** With no update
  path, `pending` is the only state a stored row can hold.
- `evidence` is **NOT NULL**. The verbatim text the fact was read from, which is
  the only way to check an extraction without re-running it. The application
  additionally verifies it appears in the conversation before writing the row.
- `confidence` is **REAL**, not text — unlike a cost. Nothing sums confidences;
  the only operation is a comparison against a threshold. Check:
  `confidence >= 0 AND confidence <= 1`.
- `category` and `status` are both check-constrained to their closed sets.
  `accepted` and `rejected` are named for Slice 9c; only `pending` is written
  today.
- `prompt_id` / `prompt_version` are duplicated from `ai_calls` deliberately:
  "which proposals came from the prompt we changed last week" is asked of *this*
  table, and joining through an audit table for a routine query would make the
  audit table load bearing.
- `decided_at` moves with `status`: a check refuses a decided proposal with no
  timestamp and a pending one with a timestamp. No `decided_by` — every decision
  is a person's, because there is no other way to make one, so the column would
  record a constant. No `updated_at`: a proposal has exactly one transition.

Two unique indexes on existing tables — `uq_conversations_account_id_id` and
`uq_ai_calls_account_id_id` — exist so the composite foreign keys above can
reference them. Both are created by `0011`, the migration that needs them,
rather than retrofitted into `0009` and `0010`: an applied migration is history.

### `memories`

Facts a person has approved for long-term retention. **Nothing reaches this
table without a decision.** Created by migration `0012`.

- Index: `(account_id, created_at DESC)`. No index on `contact_id` yet — "what
  do we know about this person" is retrieval's question, and the index it wants
  should be chosen by that query rather than guessed a milestone early
  (§20).
- **Unique: `proposal_id`** (partial, where not null) — one memory per accepted
  proposal, so "acceptance creates exactly one memory" is a constraint rather
  than a rule, including when two decisions race. Partial because the column is
  nullable, and several memories may have lost their proposal.
- **Unique: `(account_id, contact_id, category, key)`** (partial, where not
  deleted and the contact is known) and **`(account_id, category, key)`**
  (partial, where not deleted and the contact is unknown). Two indexes because
  SQL treats NULLs as distinct: without the second, identical facts from group
  conversations would both be stored.
- `key` is a deterministic normalisation of `value`, derived by the application
  and never supplied by a model (ADR-059 §2). It deduplicates; it does not
  detect contradictions.
- Foreign keys: `account_id -> accounts` (cascade), composite
  `(account_id, contact_id) -> contacts` (cascade — purging a contact removes
  everything about them, `PRIVACY.md` §7), and **`proposal_id`,
  `conversation_id` and `ai_call_id` all `SET NULL`**. The one place in this
  schema where SET NULL is right: `ai_calls` and `memory_proposals` cascade from
  `chats`, so without it deleting a chat would silently erase approved
  knowledge. What is lost is the trail, not the fact.
- **No `UPDATE` path exists in any repository beyond the soft delete.** A
  memory is immutable; correcting one means forgetting it and accepting a new
  proposal.
- `deleted_at` is a timestamp, not a flag: retention has to ask "deleted before
  when". Deleting frees the key, so the same fact can be accepted again.
- `importance` (REAL, NOT NULL, default `0.5`) — a person's judgement of what
  the fact is worth, set at acceptance. **Ranked above `confidence`**: a human
  view of relevance outranks a machine's view of truth (ADR-060 §2).
- `retrieval_count` (INTEGER, NOT NULL, default `0`) and `last_retrieved_at` —
  bookkeeping *about* the fact rather than part of it, which is what lets them
  change while the memory stays immutable. Incremented in SQL over the whole
  selection in one statement, so two contexts built at once cannot lose a count.
  Two checks keep them consistent: they move together, and nothing can have been
  retrieved before it existed.
- **No `updated_at`** — nothing updates a memory. No `is_pinned` or
  `valid_from`/`valid_until`: both are ranking policies nothing implements. No
  `memory_revisions` table: revisions record edits, and there are none.
- **`ix_memories_account_id_contact_id_created_at`** — chosen by the retrieval
  query rather than guessed. Retrieval asks for one account's live memories
  about one contact, newest first, so the index leads with the `WHERE` columns
  and follows with the order a candidate cap takes from. Partial on
  `deleted_at IS NULL`: a forgotten memory should occupy no space in an index
  every context walks. There is no index on `importance`, `confidence` or
  `category` — ranking happens in memory over a set already bounded to one
  contact, and no index serves a five-key lexicographic order.

### `plugins` / `plugin_data`

- `plugins`: unique `plugin_name`
- `plugin_data`: unique `(plugin_id, key)`; FK `ON DELETE CASCADE`
- Plugins never read or write core tables directly (ADR-025 §7)

### `settings`

- Unique: `(account_id, key)`
- A key declared in Configuration must not appear here (ADR-028 §1); verified at startup

### `retention_policies`

- Unique: `(account_id, scope, chat_id)`
- Check: `scope IN ('messages','attachments','analyses','summaries','memories','suggestions','logs','audit')`
- Check: `action IN ('delete','archive','anonymise')`
- Check: `scope <> 'audit' OR action = 'archive'` — audit events are never deleted by policy

### `audit_log`

- Index: `(account_id, created_at DESC)`, `(event_type, created_at DESC)`
- **Append-only.** No `UPDATE` or `DELETE` path exists in any repository. Enforced additionally by SQLite triggers that raise on update or delete.

### `schema_metadata`

Infrastructure metadata about the database file itself -- not a business table. Records which application wrote the file and when, so that backups can embed provenance (section 10) and a restore can refuse an incompatible file before overwriting anything.

- Primary key: `key`
- Columns: `key`, `value`
- Created by migration `0001`

It also gives the baseline migration something real to create. A migration sequence whose first step is a no-op cannot be tested in either direction, so the migration machinery would go unverified until the first business table -- exactly when a fault in it is most expensive.

### `alembic_version`

Managed by Alembic. Records the current schema revision. Read by backups (§17) and by the startup compatibility check (§16).

---

# 5. Removed Tables

| v1.0 Table | Disposition | Reason |
|---|---|---|
| `logs` | **Removed** | Log writes contend with the single-writer model, inflate the database and backups, and are unreadable when the database is the failing component. Replaced by rotating JSONL files plus the `audit_log` table for durable security events (ADR-027). |
| `ai_analysis` | **Renamed and generalised** to `analyses` | The original was message-scoped only, leaving conversation-level analyses homeless. |

---

# 6. Soft Deletion Policy

Three distinct operations, deliberately not conflated:

| Operation | Effect | Reversible | Applies to |
|---|---|---|---|
| **Soft delete** | Sets `deleted_at`; the row is excluded from all repository queries by default and from AI processing | Yes | `contacts`, `chats`, `messages`, `memories`, `goals` |
| **Hard delete** | Removes the row and all dependents via cascade; removes associated embeddings and downloaded files | No | Any, on explicit user request |
| **Purge** | Hard-deletes every row relating to a Contact or Chat across all tables, in one transaction, and writes an audit event | No | Contact, Chat, Account |

Rules:

1. Every repository read excludes soft-deleted rows unless explicitly requested (`include_deleted=True`).
2. A soft-deleted Contact is never sent to an AI provider, never appears in retrieval, and never produces suggestions.
3. Soft-deleted rows are hard-deleted by the retention job after the configured grace period (default 30 days).
4. **`audit_log` supports neither soft nor hard deletion.**
5. Purge is the operation that satisfies a contact's erasure request (`PRIVACY.md` §7) and always completes fully or not at all.

---

# 7. Migration Policy

1. **Alembic manages every schema change.** Manual `ALTER TABLE` against a user database is prohibited.
2. Migrations are numbered sequentially and named descriptively: `0001_initial_schema`, `0002_add_style_profiles`.
3. **Every migration provides both `upgrade()` and `downgrade()`.** A migration that cannot be reversed must document why in its docstring and is permitted only for data-destroying operations the user explicitly requested.
4. SQLite's limited `ALTER TABLE` support requires `op.batch_alter_table()` for column drops, type changes and constraint changes. Batch operations are isolated in helper functions so PostgreSQL migrations do not inherit the workaround (ADR-016 §3).
5. **Migrations run inside a transaction.** A failure rolls back completely.
6. **The application takes an automatic backup before applying any migration** and records the backup path in the audit log. A failed migration restores automatically and reports the failure.
7. Migrations are tested in CI **both directions** — `upgrade head`, then `downgrade base`, then `upgrade head` again — against a database seeded with representative data.
8. Data migrations (transforming existing rows) are separate from schema migrations and are idempotent.
9. **No migration deletes user data without explicit confirmation** surfaced through the UI or CLI.

Initial migration sequence:

| Revision | Contents | Status |
|---|---|---|
| `0001` | `schema_metadata` -- infrastructure baseline | **Applied** |
| `0002` | `accounts` — the ownership root | **Applied** |
| `0003` | `user_profiles` — the first account-scoped table | **Applied** |
| `0004` | `contacts` — the first many-per-account table | **Applied** |
| `0005` | `chats` — the communication graph's edge | **Applied** |
| `0006` | `messages` — the immutable factual record | **Applied** |
| `0007` | `telegram_sessions` — where an account stands with Telegram | **Applied** |
| — | Milestone 2.7 (chat and contact synchronisation) added **no migration** | — |
| `0008` | `sync_cursors` — how far each chat's backfill has got | **Applied** |
| next | `settings`, `audit_log` | Milestone 2 |
| `0009` | `conversations` — bounded episodes, derived from messages | **Applied** |
| `0010` | `ai_calls` — one row per model invocation | **Applied** |
| `0011` | `memory_proposals` — candidate facts awaiting a decision | **Applied** |
| `0012` | `memories`, and `decided_at` on `memory_proposals` | **Applied** |
| `0013` | `importance`, `retrieval_count`, `last_retrieved_at` and the retrieval index | **Applied** |
| `0014` | `suggestions` — what the assistant proposed, awaiting a decision | **Applied** |
| next | `attachments` | Milestone 3 |
| next | `messages_fts` and synchronisation triggers | Milestone 3 |
| next | `memories`, `memory_proposals`, `memory_revisions`, `goals` | Milestone 5 |
| `0011` | `relationship_profiles`, `style_profiles` | Milestone 6 |
| `0012` | `embedding_models`, `embeddings` | Milestone 7 |
| `0013` | `analyses`, `conversation_summaries`, `conversation_plans`, `reply_suggestions`, `behavior_recommendations` | Milestone 8 |
| next | `ai_providers` — a routing record, once there are two providers to route between (ADR-057 §2) | Milestone 8 |
| `0015` | `notifications`, `retention_policies` | Milestone 10 |
| `0016` | `plugins`, `plugin_data` | Milestone 12 |

Numbers beyond `0004` are a plan, not a commitment: each is assigned when its
migration is written, in the order the milestones actually land.

One migration adds one aggregate's table rather than a milestone's worth at once, so each is reviewable on its own and a failure has one cause to look for.

Business tables begin at `0002`: `0001` is the infrastructure baseline described above.

**Constraint naming is declared once**, in a `MetaData` naming convention. Without it SQLite invents constraint names, and an unnamed constraint cannot be dropped by a migration -- a problem that surfaces the first time a constraint needs changing, by which point every user has a database full of anonymous constraints.

---

# 8. Multi-Account Support

Multi-account is a **future feature with present-day schema readiness**:

1. `account_id` exists on every account-owned table from migration `0001`.
2. Every repository method accepts an account scope; there is no global query path.
3. Uniqueness constraints are account-scoped, so two accounts may know the same Telegram user without collision.
4. v1.0 enforces exactly one active account; the constraint is a `CHECK`, removable in a single migration.
5. Vector matrices are loaded per account, preventing cross-account retrieval leakage.
6. Backups may be account-scoped or whole-database.

The cost today is one indexed integer column per table. The cost of adding it later would be a rewrite of every query and constraint in the system.

---

# 9. Archive Strategy

Archiving keeps the working set small without discarding data, which matters once a chat holds hundreds of thousands of messages.

1. **Archive criteria.** Messages older than the archive threshold (default 365 days) in chats with `sync_enabled = true`, provided they are not referenced by a non-superseded summary or an active memory.
2. **Archive destination.** A separate SQLite file per account per year, `archive_{account_id}_{year}.db`, with the identical schema.
3. **Archive is not deletion.** Archived data remains searchable through an explicit "search archives" action, which attaches the archive file read-only.
4. **What is never archived.** Memories, goals, relationship profiles, style profiles, summaries, audit events. These are small and permanently relevant.
5. **Attachments** are archived by moving files to the archive directory and rewriting `storage_path`.
6. Archiving is a background job, resumable, transactional per batch, and writes an audit event.
7. Restoring from archive is supported and merges rows back by unique key, so it is idempotent.

Archiving is implemented in Milestone 13 and is not required for the MVP; the criteria are specified now so the schema and access patterns accommodate it.

---

# 10. Backup Strategy

1. **Method.** SQLite's online backup API (or `VACUUM INTO`), never a filesystem copy of a live database, which can capture a torn WAL state.
2. **Contents of a backup archive:**
   - the database file
   - `alembic_version` revision identifier
   - application version
   - a manifest listing included files with SHA-256 checksums
   - optionally, downloaded attachment files
   - **never** secrets, session files, or logs
3. **Triggers.** Manual; automatic on a schedule (default daily); mandatory before every migration; mandatory before any purge operation.
4. **Retention.** Configurable count and age; default 7 daily and 4 weekly, oldest pruned first.
5. **Encryption.** Optional in general, **mandatory when the destination is outside the application data directory** (ADR-022). AES-256-GCM with a key derived from a user passphrase via a memory-hard KDF.
6. **Verification.** Every backup is verified immediately after creation: checksums recomputed, `PRAGMA integrity_check` run against the copy, row counts of principal tables compared. An unverified backup is reported as failed.
7. **Restore.** Validates the manifest, checks schema-revision compatibility (§16), backs up the current database first, then replaces it atomically. Restore always writes an audit event.
8. **Session data is never restored from backup.** Telegram authentication is re-performed after a restore, by design — a restored session on a different machine is a security hazard.

---

# 11. Integrity and Constraints Summary

| Constraint kind | Count | Purpose |
|---|---|---|
| Primary keys | Every table | Identity |
| Unique constraints | 18 | Idempotency and deduplication |
| Partial unique constraints | 4 | One active account, one open conversation per chat, one active goal per contact, one current summary per conversation |
| Foreign keys | 31 | Referential integrity, enforced at all times |
| Check constraints | 24 | Schema-level enforcement of domain enums and ranges |
| Append-only triggers | 1 table | `audit_log` immutability |
| FTS synchronisation triggers | 3 | Keep `messages_fts` consistent with `messages` |

The four partial unique constraints are the schema-level expression of domain invariants 3, 4 and the summary rule in `DOMAIN_MODEL.md` §9. They are load-bearing: without them the invariants depend entirely on application discipline.

---

# 12. Indexing Strategy

Indexes exist to serve a documented access pattern (§20) and for no other reason. Each index below names the query it serves.

| Index | Serves |
|---|---|
| `messages(chat_id, sent_at DESC)` | Conversation history paging |
| `messages(conversation_id, sent_at)` | Context assembly |
| `messages(chat_id, is_outgoing, sent_at DESC)` | Response-time metrics |
| `messages(account_id, sent_at DESC)` | Recent activity across chats |
| `messages(account_id, chat_id, telegram_message_id)` UNIQUE | Ingest idempotency |
| `memories(contact_id, importance DESC)` | Memory retrieval candidates |
| `memories(contact_id, last_retrieved_at DESC)` | Recency component of ranking |
| `memory_proposals(account_id, status, created_at)` | Review queue |
| `conversations(chat_id, started_at DESC)` | Conversation list |
| `analyses(subject_kind, subject_id, analysis_type, analysis_version)` UNIQUE | Cache lookup |
| `embeddings(account_id, embedding_model_id)` | Vector matrix load |
| `chats(account_id, last_message_at DESC)` | Chat list ordering |
| `audit_log(account_id, created_at DESC)` | Audit review |

**Indexes deliberately not created:** none on `messages.text` (FTS5 handles it), none on low-cardinality boolean columns alone, none on `ai_calls` beyond time ordering. Additional indexes are added only after profiling shows a measured need (`DEVELOPMENT_WORKFLOW.md` §18).

---

# 13. Vector Storage Strategy

Summarised here; specified fully in `VECTOR_SEARCH.md`.

1. Vectors live in `embeddings` as float32 `BLOB`s alongside the rest of the data — one file to back up, one file to encrypt, one consistency domain.
2. Every vector references an `embedding_models` row, so dimension and model are always known and vectors from different models are never mixed.
3. MVP search is exact brute-force cosine similarity in NumPy over an in-process matrix cache, invalidated on write (ADR-017).
4. `sqlite-vec` and `pgvector` adapters implement the same `VectorStore` port when scale requires them.
5. Embeddings are derived data: they can always be regenerated from source text, so they are excluded from backups by default to reduce backup size, and rebuilt on restore.

---

# 14. Repository Pattern

Every table is reached through a repository (ADR-004). Repositories return domain objects, accept domain objects, and never expose rows, SQL or SQLAlchemy types.

Repositories: `AccountRepository`, `UserProfileRepository`, `SessionRepository`, `ContactRepository`, `ChatRepository`, `ConversationRepository`, `MessageRepository`, `AttachmentRepository`, `MemoryRepository`, `MemoryProposalRepository`, `GoalRepository`, `RelationshipRepository`, `StyleProfileRepository`, `SummaryRepository`, `PlanRepository`, `SuggestionRepository`, `BehaviorRepository`, `AnalysisRepository`, `EmbeddingRepository`, `SyncCursorRepository`, `NotificationRepository`, `AIProviderRepository`, `AICallRepository`, `PluginRepository`, `SettingsRepository`, `RetentionPolicyRepository`, `AuditRepository`.

Full signatures are specified in `API.md` §7–§9.

**No generic repository base** (ADR-035). Each repository declares only the
operations its aggregate supports; the shared obligations are a contract
enforced by a test suite every implementation runs, and the shared *mechanics*
(execution, pagination, mapping, error normalisation) come from an
infrastructure base class used for code reuse rather than polymorphism.

**No optimistic locking and no version columns** (ADR-036). Transactions
serialize on one connection (ADR-034), so the database-level lost update cannot
occur. The remaining race spans user think-time and is handled, where it
matters, by memory revisions -- which merge rather than reject.

## Mapping

Mapping is hand-written and explicit. Each mapper implements `to_domain` and
`to_params` and must satisfy four properties, verified by test:

1. **Round-trip fidelity** — `to_domain(to_params(entity)) == entity`. This is
   what catches a column added by a migration and forgotten in the mapper,
   which otherwise looks like a field silently reverting to its default.
2. **Purity** — no clock, no query, no mutation, so the round trip is testable.
3. **Total conversion** — every column the mapper claims is converted.
4. **Identity preservation** — the identifier survives unchanged.

Mappers contain **no version branching**. A mapper reads what the current schema
provides; when a migration adds a column it supplies a default for existing
rows, so old rows read correctly without the mapper knowing which migration
wrote them. A mapper that needs to branch on schema version is the signal that
the migration should have backfilled instead.

## Identity and loading

There is **no identity map**. Reading the same row twice produces two equal
objects, not the same object, so entities are compared by identifier and a
caller holding a stale copy holds a snapshot rather than a live view.

**Everything is eager.** There are no lazy proxies, no session-attached state
and no relationship traversal, so an accidental N+1 query is not expressible. A
use case needing related data asks for it explicitly, which puts the second
query where its cost is visible.

---

# 15. Transactions and Unit of Work

1. Multi-repository operations execute inside a `UnitOfWork` (`API.md` §6). Repositories never open their own transactions.
2. A use case is the transaction boundary. One use case, one transaction.
3. **Operations that must be atomic:**
   - Message ingest: message + conversation + sync cursor
   - Proposal approval: memory create/update + revision + proposal status
   - Suggestion generation: suggestion + behavior recommendation + plan reference
   - Purge: every row relating to the subject, across all tables
   - Migration: schema change + version bump
4. Cross-aggregate work is **eventually consistent** via domain events, deliberately outside the originating transaction (`DOMAIN_MODEL.md` §8).
5. Long-running operations (backfill, re-index, archive) are **batched** — many small transactions with progress recorded — so interruption never leaves inconsistent state.

---

# 16. Schema Version Compatibility

1. The application records the schema revision it expects.
2. At startup:
   - database revision **equals** expected → proceed
   - database revision **older** → offer migration, backing up first
   - database revision **newer** → refuse to start, with a clear message that a newer application version is required. Downgrading data is not attempted.
3. Backups embed the revision; restoring a backup from a newer revision is refused.
4. `PRAGMA integrity_check` runs on first start after any crash and after every restore.

---

# 17. Import and Export

| Format | Purpose | Contents |
|---|---|---|
| **JSON export** (default) | Portability and inspection; satisfies the export right in `PRIVACY.md` §7 | Contacts, chats, messages, memories, goals, summaries, relationship and style profiles, settings. Secrets and session data excluded. |
| **Scoped JSON export** | Per-contact or per-chat export, for honouring a contact's request | All data relating to one subject |
| **SQLite backup** | Full fidelity restore | Complete database (§10) |
| **Markdown export** | Human-readable conversation record | Rendered messages and summaries for selected chats |

Import accepts JSON exports produced by the same major version, validates against the schema, merges by unique key (so re-import is idempotent), and reports every skipped or conflicting record rather than failing silently.

---

# 18. Maintenance Jobs

| Job | Default schedule | Action |
|---|---|---|
| `integrity_check` | Weekly, and after any crash | `PRAGMA integrity_check`, foreign-key check, polymorphic reference validation for `analyses` and `embeddings` |
| `retention` | Daily | Applies retention policies; writes an audit event |
| `hard_delete_expired` | Daily | Hard-deletes soft-deleted rows past the grace period |
| `optimize` | Weekly | `PRAGMA optimize`, `ANALYZE` |
| `vacuum` | Monthly, or when free pages exceed a threshold | Reclaims space; requires exclusive access, so it runs at shutdown |
| `backup` | Daily | Creates and verifies a backup |
| `archive` | Monthly | Moves qualifying messages to archive files (§9) |
| `embedding_reindex` | On demand | Regenerates vectors after an embedding model change |
| `proposal_expiry` | Daily | Marks stale memory proposals `expired` |

All jobs are idempotent, resumable, cancellable and report progress.

---

# 19. Performance Guidelines

1. Batch inserts during backfill (transaction per 500 messages) rather than per message.
2. Paginate every message query with keyset pagination (`WHERE sent_at < ? ORDER BY sent_at DESC LIMIT ?`), never `OFFSET`, which degrades linearly.
3. Cache the vector matrix in process; invalidate on write.
4. Cache hot small tables (user profile, active goals, settings) with event-driven invalidation.
5. Never load an entire chat history into memory; the UI uses a virtualized model backed by keyset paging (ADR-014).
6. Measure before optimising; add indexes only in response to profiling (`DEVELOPMENT_WORKFLOW.md` §18).

---

# 20. Access Patterns

The queries the schema is designed to serve. Every index in §12 traces to one of these.

| # | Pattern | Frequency | Target |
|---|---|---|---|
| 1 | Latest N messages in a chat, paged backwards | Very high | < 10 ms |
| 2 | Messages within a conversation | High | < 10 ms |
| 3 | Chat list ordered by last activity | High | < 20 ms |
| 4 | Memories for a contact ordered by importance | High | < 10 ms |
| 5 | Vector similarity over a contact's memories | High | < 50 ms |
| 6 | Cached analysis lookup by subject and version | High | < 5 ms |
| 7 | Pending memory proposals | Medium | < 10 ms |
| 8 | Response-time pairs for metric computation | Medium (background) | < 500 ms per contact |
| 9 | Full-text message search | Medium | < 200 ms |
| 10 | Ingest a message idempotently | Very high during sync | < 5 ms |
| 11 | Purge all data for a contact | Rare | Transactional, any duration |
| 12 | Audit log review | Rare | < 100 ms |

Targets are provisional and become binding in `PERFORMANCE_BUDGETS` at Milestone 13, measured against a database seeded with 500,000 messages, 200 contacts and 5,000 memories.

---

# 21. Security

1. **Parameterized queries only.** String-concatenated SQL is prohibited; enforced by review and by SQLAlchemy Core's expression API.
2. **Foreign keys enforced** on every connection.
3. **Never stored in the database:** passwords, API keys, authentication codes, session tokens, TDLib encryption keys. Secrets live in the `SecretStore` (ADR-021); the database holds only their names.
4. Phone numbers are stored as salted hashes.
5. `ai_calls` stores metadata only. Never prompt content, under any setting; the response only as a digest, unless `ai.store_responses` is enabled outside production (ADR-057 §6).
6. Database files are created with owner-only permissions and verified at startup (ADR-022).
7. Full-database encryption is a Phase 2 capability with a single implementation site (ADR-022).
8. Backups written outside the application data directory are encrypted by default.

---

# 22. Testing Requirements

Every repository is tested for: CRUD round-trip, mapper fidelity (domain → row → domain equality), pagination correctness at boundaries, soft-delete exclusion, constraint violation raising the correct typed error, and account scoping (no cross-account leakage).

Schema-level tests verify: every migration up and down, every unique and partial-unique constraint actually rejecting duplicates, every foreign key cascading as documented, `audit_log` rejecting update and delete, and FTS triggers keeping the index consistent through insert, update and delete.

Load tests seed 500,000 messages and assert the §20 targets.

---

# 23. Database Rules

Every table must have:

- A primary key
- `created_at`, and `updated_at` where mutable
- `account_id` if account-owned
- Foreign keys with an explicit `ON DELETE` action
- Check constraints mirroring every domain enum
- Indexes justified by a documented access pattern
- Soft-delete support where the data is user-visible

No duplicated data unless justified in this document.

---

# 24. Database Philosophy

The database is the single source of truth for persistent application data, and it is derived from the domain model rather than the reverse.

Business rules belong in the Domain Layer. Constraints in the schema exist to make domain invariants unbreakable, not to express business logic.

Every schema change is documented here, accompanied by a reversible migration, and reflected in `DOMAIN_MODEL.md` first.
