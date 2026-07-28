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
        int id PK
        int account_id FK
        text primary_language
        text additional_languages
        text tone_preference
        text preferred_message_length
        text emoji_usage
        text available_hours
        text quiet_hours
        text auto_approve_memory_categories
        text confidence_thresholds
    }

    telegram_sessions {
        int id PK
        int account_id FK
        text state
        text session_path
        text encryption_key_ref
        timestamp connected_at
        timestamp last_activity_at
    }

    contacts {
        int id PK
        int account_id FK
        int telegram_user_id
        text username
        text display_name
        text language
        text timezone
        int is_blocked
        timestamp first_seen_at
        timestamp last_seen_at
        timestamp deleted_at
    }

    chats {
        int id PK
        int account_id FK
        int telegram_chat_id
        text chat_type
        text title
        int contact_id FK
        int sync_enabled
        text ai_processing_mode
        int retention_days
        timestamp last_message_at
        timestamp deleted_at
    }

    conversations {
        int id PK
        int account_id FK
        int chat_id FK
        timestamp started_at
        timestamp ended_at
        int message_count
        int is_open
        text initiated_by
        text dominant_language
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
        int id PK
        int account_id FK
        int chat_id FK UK
        int oldest_synced_message_id
        int newest_synced_message_id
        int backfill_complete
        timestamp last_sync_at
        text last_error
        int consecutive_failures
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
        text provider_name
        text model_identifier
        text prompt_id
        text prompt_version
        text task_kind
        int input_tokens
        int output_tokens
        real estimated_cost
        int latency_ms
        text outcome
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

- Unique: `account_id` (one profile per account)
- FK: `account_id → accounts(id) ON DELETE CASCADE`
- Check: `tone_preference IN ('casual','neutral','formal','mirror_contact')`

### `telegram_sessions`

- Unique: `account_id`
- FK: `account_id → accounts(id) ON DELETE CASCADE`
- Check: `state IN ('disconnected','connecting','awaiting_phone','awaiting_code','awaiting_password','ready','reconnecting','logged_out')`
- `encryption_key_ref` holds a `SecretStore` name only (ADR-021). A key value in this column is a security defect.

## 4.2 People and Chats

### `contacts`

- Unique: `(account_id, telegram_user_id)`
- Index: `(account_id, username)`, `(account_id, last_seen_at DESC)`
- FK: `account_id → accounts(id) ON DELETE CASCADE`
- Soft delete: `deleted_at`

### `chats`

- Unique: `(account_id, telegram_chat_id)`
- Index: `(account_id, last_message_at DESC)`, `(account_id, sync_enabled)`
- FK: `account_id → accounts(id) ON DELETE CASCADE`, `contact_id → contacts(id) ON DELETE SET NULL`
- Check: `chat_type IN ('private','group','supergroup','channel','saved')`
- Check: `ai_processing_mode IN ('disabled','local_only','cloud_allowed')`, default `'local_only'`
- Check: `contact_id IS NOT NULL OR chat_type <> 'private'`

### `conversations`

- Index: `(chat_id, started_at DESC)`, `(account_id, is_open)`
- Partial unique: at most one row per `chat_id` with `is_open = 1`
- FK: `chat_id → chats(id) ON DELETE CASCADE`
- Check: `ended_at IS NULL OR ended_at >= started_at`

### `messages`

The largest table; every index here is justified by an access pattern in §20.

- **Unique: `(account_id, chat_id, telegram_message_id)`** — the idempotency guarantee for re-synchronisation
- Index: `(chat_id, sent_at DESC)` — primary history query
- Index: `(conversation_id, sent_at)` — context assembly
- Index: `(chat_id, is_outgoing, sent_at DESC)` — response-time metrics
- Index: `(account_id, sent_at DESC)` — cross-chat recency
- FK: `chat_id → chats(id) ON DELETE CASCADE`, `conversation_id → conversations(id) ON DELETE SET NULL`, `reply_to_message_id → messages(id) ON DELETE SET NULL`
- Check: `sender_kind IN ('operator','contact','system')`
- Check: `message_type IN ('text','photo','voice','video','document','sticker','location','poll','service','other')`

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

- Unique: `chat_id`
- FK: `chat_id → chats(id) ON DELETE CASCADE`

### `notifications`

- Index: `(account_id, is_dismissed, created_at DESC)`
- Check: `severity IN ('info','warning','error','action_required')`

### `ai_providers`

- Unique: `(provider_name, model_identifier)`
- Check: `data_boundary IN ('local','external')`
- Check: `provider_kind IN ('cloud_llm','local_llm','cloud_embedding','local_embedding')`
- `api_key_ref` is a `SecretStore` name only.

### `ai_calls`

- Index: `(account_id, created_at DESC)`, `(provider_name, created_at DESC)`
- **Contains no prompt or response content** (`SECURITY.md` §9)
- Subject to log retention, not conversation retention

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
| `0003` | `user_profiles`, `telegram_sessions`, `settings`, `audit_log` | Milestone 1 |
| `0004` | `contacts`, `chats`, `conversations`, `messages`, `attachments`, `sync_cursors` | Milestone 1 |
| `0005` | `messages_fts` and synchronisation triggers | Milestone 1 |
| `0006` | `memories`, `memory_proposals`, `memory_revisions`, `goals` | Milestone 1 |
| `0007` | `relationship_profiles`, `style_profiles` | Milestone 1 |
| `0008` | `embedding_models`, `embeddings` | Milestone 1 |
| `0009` | `analyses`, `conversation_summaries`, `conversation_plans`, `reply_suggestions`, `behavior_recommendations` | Milestone 1 |
| `0010` | `ai_providers`, `ai_calls` | Milestone 1 |
| `0011` | `notifications`, `retention_policies` | Milestone 1 |
| `0012` | `plugins`, `plugin_data` | Milestone 1 |

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
5. `ai_calls` stores metadata only, never prompt or response content.
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
