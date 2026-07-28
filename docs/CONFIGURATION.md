# CONFIGURATION.md

# Telegram AI Conversation Assistant

Configuration System

Version: 1.0

Status: Active

Last Updated: 2026-07-28

Governing decisions: ADR-028 (configuration vs settings ownership), ADR-021 (secrets)

---

# 1. Purpose

This document specifies the configuration system: what belongs in configuration, what belongs in the database, what belongs in the secret store, how values are resolved, and the full key reference.

---

# 2. Three Stores, One Rule

Every configurable value lives in exactly one of three places. The rule is *who owns it and when it changes*.

| Store | Contains | Scope | Mutable at runtime | Backed up | Example |
|---|---|---|---|---|---|
| **Configuration** (files + env) | Paths, endpoints, limits, feature flags, log levels, model names | Machine / deployment | No — restart or explicit reload | No (version-controlled instead) | `database.path`, `ai.providers.*.endpoint` |
| **Settings** (database) | Theme, language, tone, active provider, retention periods, auto-approve categories, per-chat AI mode | User | Yes | Yes, with user data | `ui.theme`, `memory.auto_approve_categories` |
| **Secret store** (OS credential store) | API keys, encryption keys, `api_hash` | Machine, protected | Yes | **Never** | `ANTHROPIC_API_KEY` |

**A key exists in exactly one store.** Startup validation compares the configuration schema against the settings schema and fails if any key appears in both — the drift this prevents is otherwise discovered only when the two disagree in production.

---

# 3. Resolution Order

Later sources override earlier ones:

```
1. Built-in defaults              (in code, always complete and valid)
2. config/default.yaml            (committed; the documented baseline)
3. config/profiles/<profile>.yaml (environment profile)
4. config/local.yaml              (gitignored; user overrides)
5. Environment variables          (TGASSIST_ prefixed)
6. Command-line flags             (highest precedence)
```

## Environment profiles

A profile supplies a layer of defaults between the shipped baseline and the user's local overrides, so that developer ergonomics and production safety do not have to be reconciled in one file.

| Profile | Purpose | Notable defaults |
|---|---|---|
| `development` (default) | A developer at a terminal | `DEBUG` level, console format, credential store not required |
| `testing` | The automated suite | `WARNING` level, no file output, no permission enforcement |
| `production` | Shipped configuration | `INFO` level, JSON format, full enforcement; `logging.diagnostic_mode` is **rejected by validation** |

The profile is selected by `TGASSIST_PROFILE` or `--profile`. A profile named *inside* a configuration file is ignored: honouring it would mean selecting a profile after that profile's own file had already been applied.

Rules:

1. **Built-in defaults are complete.** The application starts with no configuration files present.
2. `config/default.yaml` is committed and serves as documentation of every key.
3. `config/local.yaml` is gitignored and never contains secrets.
4. Environment variables map by upper-casing and replacing `.` with `__`: `database.path` → `TGASSIST_DATABASE__PATH`.
5. **Unknown keys are a startup error**, not a silent ignore. A typo in a config file should stop the application, not be discovered as a mysteriously ineffective setting.
6. Configuration is **immutable after startup**. Changes require a restart or `tgassist config reload`, which re-validates and swaps atomically or keeps the previous configuration.
7. `tgassist config show` prints the resolved configuration with the origin of each value and **every secret masked**.

---

# 4. Validation

Configuration is parsed into typed `pydantic-settings` models (`API.md` §12). Validation runs at startup and:

- rejects unknown keys
- enforces types, ranges and enum membership
- verifies that paths are writable and creates directories where appropriate
- verifies that referenced prompt files and schemas exist (ADR-026 §7)
- verifies that every referenced secret name resolves, for enabled providers only
- verifies no key overlaps with the settings schema
- verifies the secret store is available

Any failure is a `ConfigurationError` and is **fatal**. A misconfigured application that starts and behaves subtly wrongly is worse than one that refuses to start with a clear message.

---

# 5. File Layout

```
config/
├── default.yaml              # committed; complete documented baseline
├── local.yaml                # gitignored; user overrides
├── local.yaml.example        # committed; annotated template
└── profiles/
    ├── development.yaml
    ├── testing.yaml
    └── production.yaml
```

Logging configuration lives under the `logging` section of the main files
rather than in a separate `logging.yaml`. A second file would duplicate keys
that the model already owns, and duplication between configuration sources is
exactly what the one-key-one-store rule exists to prevent.

## Implementation status

The configuration *system* is complete as of Milestone 0: layering, profiles,
validation, unknown-key rejection, origin tracking, immutability and masking.

**Sections are added by the milestone that implements their subsystem.** Unknown
keys are rejected, so adding `database:` or `ai:` before Milestone 1 or 3 stops
startup rather than being ignored. Implemented today: `app`, `logging`,
`security`. The remaining sections in §6 are the specification those milestones
implement against.

---

# 6. Configuration Reference

Complete key reference. Types, defaults and descriptions.

## 6.1 `app`

| Key | Type | Default | Description |
|---|---|---|---|
| `app.data_dir` | path | OS app-data dir | Root for database, sessions, attachments, logs |
| `app.locale` | string | `system` | Interface language; `system` follows the OS |
| `app.first_run_completed` | bool | `false` | Set after onboarding; skips the wizard |

## 6.2 `database`

| Key | Type | Default | Description |
|---|---|---|---|
| `database.path` | path | `{data_dir}/tgassist.db` | SQLite file |
| `database.busy_timeout_ms` | int | `5000` | Lock wait before error |
| `database.journal_mode` | enum | `WAL` | `WAL` \| `DELETE` |
| `database.synchronous` | enum | `NORMAL` | `FULL` \| `NORMAL` \| `OFF` |
| `database.auto_migrate` | bool | `true` | Apply pending migrations at startup after backup |
| `database.encryption_enabled` | bool | `false` | Phase 2 only (ADR-022) |
| `database.archive_dir` | path | `{data_dir}/archives` | Archive files |

## 6.3 `telegram`

| Key | Type | Default | Description |
|---|---|---|---|
| `telegram.adapter` | enum | `tdlib` | `tdlib` \| `telethon` (ADR-012) |
| `telegram.api_id` | int | — | From my.telegram.org; required |
| `telegram.api_hash_ref` | string | `TELEGRAM_API_HASH` | **Secret store name**, not the value |
| `telegram.session_dir` | path | `{data_dir}/sessions` | Encrypted session store |
| `telegram.tdlib_library_path` | path | auto-detect | Path to `tdjson`; TDLib adapter only |
| `telegram.device_model` | string | `Desktop` | Reported to Telegram |
| `telegram.reconnect_max_attempts` | int | `10` | Before giving up |
| `telegram.reconnect_base_delay_s` | float | `2.0` | Exponential base |
| `telegram.flood_wait_ceiling_s` | int | `300` | Above this, raise rather than wait |
| `telegram.request_timeout_s` | int | `30` | Per API call |

## 6.4 `sync`

Bounded synchronisation scope — the data-minimisation control (`PRIVACY.md` §6).

| Key | Type | Default | Description |
|---|---|---|---|
| `sync.mode` | enum | `selected_chats` | `selected_chats` \| `all_private` \| `manual` |
| `sync.history_depth_days` | int | `365` | Backfill horizon; `0` = unlimited |
| `sync.max_messages_per_chat` | int | `50000` | Backfill cap per chat |
| `sync.batch_size` | int | `500` | Messages per transaction |
| `sync.backfill_delay_ms` | int | `200` | Inter-request delay; account safety |
| `sync.live_updates_enabled` | bool | `true` | Receive updates while running |
| `sync.mirror_remote_deletions` | bool | `true` | Blank text when deleted remotely |
| `sync.download_media` | bool | `false` | Download attachment bytes |
| `sync.media_max_file_mb` | int | `10` | Per-file cap |
| `sync.media_total_budget_gb` | float | `5.0` | Total attachment storage cap |

## 6.5 `ai`

| Key | Type | Default | Description |
|---|---|---|---|
| `ai.enabled` | bool | `true` | Master switch; `false` = fully deterministic operation |
| `ai.default_data_boundary` | enum | `local` | Default for new chats (ADR-024) |
| `ai.request_timeout_s` | int | `60` | Cloud generation |
| `ai.local_request_timeout_s` | int | `180` | Local generation |
| `ai.max_wallclock_s` | int | `60` | Total including retries |
| `ai.composite_analysis_enabled` | bool | `true` | Batch analysis calls (ADR-029) |
| `ai.planner_enabled` | bool | `true` | Feature flag for the optional planner |
| `ai.daily_cost_limit` | float | `5.00` | Cloud spend cap per day |
| `ai.monthly_cost_limit` | float | `50.00` | Cloud spend cap per month |
| `ai.cost_currency` | string | `USD` | Display currency |

### `ai.providers.<name>`

| Key | Type | Description |
|---|---|---|
| `.kind` | enum | `cloud_llm` \| `local_llm` \| `cloud_embedding` \| `local_embedding` |
| `.enabled` | bool | Whether to load |
| `.model` | string | Model identifier |
| `.endpoint` | url | Base URL; localhost permitted for local servers |
| `.api_key_ref` | string | **Secret store name** |
| `.data_boundary` | enum | `local` \| `external` |
| `.priority` | int | Fallback order; lower runs first |
| `.max_output_tokens` | int | Per request |
| `.temperature` | float | Sampling temperature |

### `ai.tasks.<task>`

Per-task model assignment (`AI_MODELS.md` §5). Tasks: `analysis`, `reply`, `planning`, `summary`, `embedding`.

| Key | Type | Description |
|---|---|---|
| `.provider` | string | Provider name |
| `.max_output_tokens` | int | Override |
| `.temperature` | float | Override |

### `ai.context`

| Key | Type | Default | Description |
|---|---|---|---|
| `ai.context.recent_message_count` | int | `20` | Messages included |
| `ai.context.max_message_chars` | int | `2000` | Per-message truncation (injection surface, `SECURITY.md` §12) |
| `ai.context.memory_limit` | int | `15` | Maximum memories included |
| `ai.context.token_safety_margin` | float | `0.15` | Applied when counting is estimated |
| `ai.context.output_reserve_ratio` | float | `0.10` | Window reserved for output |

### `ai.retrieval`

| Key | Type | Default | Description |
|---|---|---|---|
| `ai.retrieval.candidate_k` | int | `50` | Vector candidates before ranking |
| `ai.retrieval.min_similarity` | float | `0.25` | Floor for candidates |
| `ai.retrieval.weights.similarity` | float | `0.45` | Ranking weight |
| `ai.retrieval.weights.recency` | float | `0.20` | Ranking weight |
| `ai.retrieval.weights.importance` | float | `0.20` | Ranking weight |
| `ai.retrieval.weights.usage` | float | `0.05` | Ranking weight |
| `ai.retrieval.weights.provenance` | float | `0.10` | Ranking weight |
| `ai.retrieval.recency_half_life_days` | int | `180` | Decay constant |

### `ai.memory`

| Key | Type | Default | Description |
|---|---|---|---|
| `ai.memory.extraction_enabled` | bool | `true` | Produce proposals |
| `ai.memory.proposal_expiry_days` | int | `90` | Before marking expired |
| `ai.memory.require_quote` | bool | `true` | Discard proposals without a supporting quotation |
| `ai.memory.max_proposals_per_conversation` | int | `10` | Bound on extraction output |

## 6.6 `conversation`

| Key | Type | Default | Description |
|---|---|---|---|
| `conversation.gap_minutes` | int | `360` | Inactivity that starts a new conversation |
| `conversation.max_messages` | int | `200` | Forces segmentation |
| `conversation.summarize_on_close` | bool | `true` | Background summarisation |

## 6.7 `behavior`

Deterministic timing engine bounds (`AI_MODELS.md` §3).

| Key | Type | Default | Description |
|---|---|---|---|
| `behavior.min_delay_seconds` | int | `0` | Lower bound on advice |
| `behavior.max_delay_seconds` | int | `86400` | Upper bound |
| `behavior.respect_quiet_hours` | bool | `true` | Never advise sending during quiet hours |
| `behavior.rule_version` | string | `v1` | Recorded on recommendations |

## 6.8 `embeddings`

| Key | Type | Default | Description |
|---|---|---|---|
| `embeddings.enabled` | bool | `true` | Semantic retrieval |
| `embeddings.provider` | string | `fastembed` | Provider name |
| `embeddings.model` | string | multilingual default | Model identifier |
| `embeddings.model_cache_dir` | path | `{data_dir}/models` | Downloaded weights |
| `embeddings.batch_size` | int | `32` | Texts per call |
| `embeddings.store` | enum | `numpy` | `numpy` \| `sqlite_vec` (ADR-017) |
| `embeddings.max_matrix_mb` | int | `512` | Cache ceiling before per-contact loading |
| `embeddings.auto_download` | bool | `false` | Requires explicit consent (ADR-018 §3) |

## 6.9 `search`

| Key | Type | Default | Description |
|---|---|---|---|
| `search.fts_enabled` | bool | `true` | FTS5 index |
| `search.max_results` | int | `100` | Per query |

## 6.10 `logging`

| Key | Type | Default | Description |
|---|---|---|---|
| `logging.level` | enum | `INFO` | Global level |
| `logging.dir` | path | `{data_dir}/logs` | Log destination |
| `logging.retention_days` | int | `14` | Rotation age |
| `logging.max_file_mb` | int | `50` | Rotation size |
| `logging.format` | enum | `json` | `json` \| `console` |
| `logging.diagnostic_mode` | bool | `false` | **Logs message content** — requires opt-in (`SECURITY.md` §9) |
| `logging.diagnostic_duration_minutes` | int | `60` | Auto-disable timer |
| `logging.backup_count` | int | `5` | Rotated files kept |
| `logging.console_enabled` | bool | `true` | Human-readable output to stderr |
| `logging.file_enabled` | bool | `true` | Rotating file output |
| `logging.component_levels` | map | see below | Per-logger overrides |

`component_levels` ships with `asyncio`, `alembic` and `sqlalchemy.engine` set
to `WARNING`. Every record from every source passes through one processor chain,
which is what makes redaction complete (`SECURITY.md` §9) -- but at `DEBUG` that
also means the event loop announcing its own selector. These entries quieten the
libraries without touching the application's own records; raise any of them when
you need the detail.

Every entry point applies this configuration, the CLI included (ADR-040). The
console sink writes to standard error, so command output on standard output is
unaffected by the level you choose.

## 6.11 `security`

| Key | Type | Default | Description |
|---|---|---|---|
| `security.secret_backend` | enum | `keyring` | `keyring` \| `encrypted_file` \| `env_only` |
| `security.enforce_file_permissions` | bool | `true` | Verify owner-only ACLs at startup |
| `security.require_secret_store` | bool | `true` | Refuse to start without it (`SECURITY.md` §7) |

## 6.12 `backup`

| Key | Type | Default | Description |
|---|---|---|---|
| `backup.enabled` | bool | `true` | Scheduled backups |
| `backup.dir` | path | `{data_dir}/backups` | Destination |
| `backup.schedule_cron` | string | `0 3 * * *` | Daily at 03:00 local |
| `backup.keep_daily` | int | `7` | Retention |
| `backup.keep_weekly` | int | `4` | Retention |
| `backup.encrypt` | bool | `false` | Forced to `true` outside `data_dir` (ADR-022) |
| `backup.include_attachments` | bool | `false` | Size trade-off |
| `backup.include_embeddings` | bool | `false` | Derived data (`VECTOR_SEARCH.md` §8) |
| `backup.verify_after_create` | bool | `true` | Cannot be disabled in release builds |

## 6.13 `scheduler`

| Key | Type | Default | Description |
|---|---|---|---|
| `scheduler.enabled` | bool | `true` | Background jobs |
| `scheduler.max_concurrent_jobs` | int | `2` | Parallelism |
| `scheduler.job_failure_threshold` | int | `5` | Consecutive failures before disabling |
| `scheduler.jobs.<name>.enabled` | bool | `true` | Per-job switch |
| `scheduler.jobs.<name>.interval_seconds` | int | per job | Per-job interval |

## 6.14 `plugins`

| Key | Type | Default | Description |
|---|---|---|---|
| `plugins.enabled` | bool | `true` | Plugin host |
| `plugins.dir` | path | `{app_root}/plugins` | Local plugin directory |
| `plugins.allow_entry_points` | bool | `true` | Discover pip-installed plugins |
| `plugins.autoload` | list | `[]` | Load at startup |
| `plugins.failure_threshold` | int | `3` | Failures before disabling |

## 6.15 `ui`

| Key | Type | Default | Description |
|---|---|---|---|
| `ui.enabled` | bool | `true` | `false` runs headless/CLI-only |
| `ui.message_page_size` | int | `50` | Virtualized paging |
| `ui.notifications_enabled` | bool | `true` | Desktop notifications |
| `ui.show_message_preview_in_notifications` | bool | `false` | Privacy default (`PRIVACY.md` §12) |

---

# 7. Settings Reference (database)

User preferences, mutable at runtime, backed up with user data.

| Key | Type | Default | Notes |
|---|---|---|---|
| `ui.theme` | enum | `system` | `light` \| `dark` \| `system` |
| `ui.language` | string | `system` | Interface language |
| `profile.tone_preference` | enum | `neutral` | Including `mirror_contact` |
| `profile.preferred_message_length` | enum | `medium` | |
| `profile.emoji_usage` | enum | `sparing` | |
| `profile.quiet_hours` | range | `22:00–08:00` | Local time |
| `profile.available_hours` | range | `08:00–22:00` | Local time |
| `ai.active_provider` | string | — | Current default provider |
| `ai.confidence_threshold_low` | float | `0.4` | Below → `write_manually` |
| `ai.confidence_threshold_medium` | float | `0.6` | |
| `ai.confidence_threshold_high` | float | `0.8` | Auto-approval floor |
| `memory.auto_approve_categories` | list | `[]` | Empty = review everything |
| `memory.review_reminder_enabled` | bool | `true` | Pending-proposal notifications |
| `retention.<scope>.days` | int | per class | See `PRIVACY.md` §6 |
| `chat.<id>.ai_processing_mode` | enum | `local_only` | Stored on `chats` |
| `chat.<id>.retention_days` | int | inherit | Stored on `chats` |
| `privacy.telemetry_consent` | bool | `false` | Off by default, always |

---

# 8. Secret Reference

Held in the OS credential store; referenced by name only.

| Name | Required when | Used for |
|---|---|---|
| `TELEGRAM_API_HASH` | Always | Telegram client authentication |
| `TELEGRAM_DB_ENCRYPTION_KEY` | Always | Session store encryption (auto-generated on first run) |
| `ANTHROPIC_API_KEY` | Anthropic enabled | Provider authentication |
| `OPENAI_API_KEY` | OpenAI enabled | Provider authentication |
| `GOOGLE_API_KEY` | Google enabled | Provider authentication |
| `BACKUP_PASSPHRASE` | Encrypted backups | Backup key derivation |
| `DATABASE_ENCRYPTION_KEY` | Phase 2 encryption | Database encryption |

Secrets may also be supplied as environment variables of the same name, which take precedence (ADR-021 §3).

---

# 9. Environment Variables

| Purpose | Pattern | Example |
|---|---|---|
| Configuration override | `TGASSIST_<PATH>` with `__` for `.` | `TGASSIST_DATABASE__PATH=/data/t.db` |
| Secret | Bare secret name | `ANTHROPIC_API_KEY=...` |
| Config directory | `TGASSIST_CONFIG_DIR` | `TGASSIST_CONFIG_DIR=/etc/tgassist` |
| Data directory | `TGASSIST_DATA_DIR` | `TGASSIST_DATA_DIR=/data` |

Environment variables are intended for CI, automation and advanced users. Interactive users configure through the UI and the credential store.

---

# 10. Example `local.yaml`

```yaml
# config/local.yaml — gitignored. Never put secret VALUES here.

telegram:
  api_id: 1234567
  api_hash_ref: TELEGRAM_API_HASH   # a NAME in the secret store

sync:
  mode: selected_chats
  history_depth_days: 180
  download_media: false

ai:
  enabled: true
  daily_cost_limit: 2.00
  providers:
    anthropic:
      kind: cloud_llm
      enabled: true
      model: <chosen at Milestone 3>
      api_key_ref: ANTHROPIC_API_KEY
      data_boundary: external
      priority: 10
    ollama:
      kind: local_llm
      enabled: true
      model: <chosen at Milestone 3>
      endpoint: http://localhost:11434
      data_boundary: local
      priority: 20
  tasks:
    reply:     { provider: anthropic }
    analysis:  { provider: ollama }
    summary:   { provider: ollama }

embeddings:
  provider: fastembed
  auto_download: true

logging:
  level: INFO
```

---

# 11. CLI

| Command | Purpose |
|---|---|
| `tgassist config show` | Resolved configuration, origins shown, secrets masked |
| `tgassist config validate` | Validate without starting |
| `tgassist config path` | Print resolved file locations, present and absent |
| `tgassist config reload` | Re-read and swap atomically *(pending: needs a running process to reload)* |
| `tgassist secrets list` | Secret **names** only |
| `tgassist secrets set <name>` | Store a secret (prompts; never accepts the value as an argument, which would leak into shell history) |
| `tgassist secrets delete <name>` | Remove a secret |
| `tgassist doctor` | Verify directories, permissions, secret store, database, providers, prompt registry |

---

# 12. Migration of Configuration

1. Configuration carries a `schema_version`.
2. Renamed keys are accepted under the old name for one minor version with a deprecation warning, then removed.
3. Removed keys produce a clear error naming the replacement.
4. Default changes are recorded in `CHANGELOG.md`; a default change that alters privacy or safety behaviour requires an ADR.
5. Settings migrations run as database migrations (`DATABASE.md` §7).

---

# 13. Testing Requirements

| Test | Assertion |
|---|---|
| Defaults complete | Application starts with no config files |
| Precedence | Each source overrides the previous correctly |
| Unknown key | Startup fails with a clear message |
| Type validation | Wrong types and out-of-range values are rejected |
| Store exclusivity | No key appears in both configuration and settings schemas |
| Secret masking | `config show` never emits a secret value |
| Env mapping | `TGASSIST_A__B` maps to `a.b` |
| Immutability | Configuration cannot be mutated after startup |
| Reload atomicity | A failed reload keeps the previous configuration |
| Prompt registry | A missing prompt file or schema fails startup |

---

# 14. Principles

1. One key, one store.
2. Complete built-in defaults — configuration is optional, not required.
3. Fail fast and loudly; a typo stops startup rather than changing behaviour silently.
4. Typed and validated, never free-form dictionaries.
5. Configuration is deployment-scoped and reviewable; settings are user-scoped and portable.
6. Secret names in configuration; secret values only in the credential store.
7. Every key documented here, with its type, default and meaning.
