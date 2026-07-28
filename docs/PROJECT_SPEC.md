# PROJECT_SPEC.md

# Telegram AI Conversation Assistant

Project Specification

Version: 2.0

Status: Active

Last Updated: 2026-07-28

---

# 0. Changes in Version 2.0

| Change | Reason |
|---|---|
| Milestone list replaced with a pointer to `ROADMAP.md` (§13) | v1.0 defined milestones 1–6 that conflicted with the roadmap's 0–12 |
| "unless the user enables automation" removed | No automation mode exists; the boundary is now a product constraint (ADR-023) |
| Section 4 added: thirteen previously missing requirement areas | Sync scope, offline behaviour, startup, onboarding, timezone, localization, backup, retention, import/export, recovery, multi-account, accessibility, configuration |
| Non-functional requirements paired with functional ones (§5) | v1.0 listed NFRs as five adjectives with no acceptance criteria |
| Relationship metrics respecified (§3.5) | "Trust score" and "friendship level" had no definitions and were untestable |

---

# 1. Project Overview

A modular desktop application that acts as an AI-powered conversation assistant for Telegram.

It helps the user communicate more effectively by analysing conversations, remembering what matters, generating context-aware reply suggestions, and supporting long-term relationships — while leaving every decision with the user.

The application is a **copilot, not an autopilot**. It never sends a message the user has not approved (ADR-023).

---

# 2. Project Philosophy

- Natural, thoughtful conversation over mechanical output
- Long-term relationship management
- Privacy-first, local-first design
- High-quality, modular, testable engineering
- Extensibility without core modification
- Respect for the people on the other side of the conversation, who did not install this software

---

# 3. Functional Requirements — Core

## 3.1 Telegram Integration

- Connect via the Telegram client API (ADR-001, ADR-012)
- Authenticate, including code and two-factor password steps
- Persist and restore sessions securely
- Read conversation history within the configured scope
- Receive live messages, edits and deletions
- Send messages **only** after explicit user approval
- Handle reconnection and rate limiting without data loss

## 3.2 Conversation Memory

Persistent memory per contact, covering identity, location, occupation, interests, preferences, relationships, important dates, plans, shared experiences, open questions and constraints.

**All AI-derived memories are proposals requiring user approval or an explicit auto-approval rule** (ADR-019). Memory improves as conversations continue, and every stored fact is traceable, editable and deletable.

## 3.3 Conversation Analysis

Per conversation: current topic, intent, emotion, conversation stage, open questions, follow-up opportunities and important facts. Results are cached and versioned so unchanged content is never re-analysed.

## 3.4 Goal Management

Each contact has at most one **active** goal (friendship, professional networking, language practice, maintenance, reconnection, general, custom). Goals are always user-authored; AI may suggest a change but never makes one. Goals guide planning without overriding user judgement.

## 3.5 Relationship Tracking

Deterministic, explainable metrics computed from observable data: interaction frequency, reciprocity ratio, median response times, conversation depth, topic breadth, initiation balance and engagement trend.

Every metric has a published formula and a minimum sample size (`DOMAIN_MODEL.md` §10). Below that sample size it reports `insufficient_data` rather than a number.

**No "trust score" or "friendship level" is stored.** Version 1.0 specified both without definitions, making them unfalsifiable and untestable. Qualitative labels shown in the UI are derived at presentation time from the measurable components and are always accompanied by the evidence that produced them.

## 3.6 Reply Suggestions

Each suggestion includes primary text, alternatives, reasoning, a calibrated confidence, a recommended action and the identifiers of the context used — so it remains explainable after the underlying data changes.

Confidence combines the model's self-report with verifiable signals (`AI_MODELS.md` §15). Low confidence produces a recommendation to ask a clarifying question or write manually, rather than a confident-sounding guess.

## 3.7 Timing and Pacing Recommendations

Deterministic advice on reply timing, message length and splitting, respecting relationship closeness, conversation pace, time of day and the user's quiet hours.

**Recommendations only.** The application never sends automatically and never emits synthetic typing indicators (ADR-023).

## 3.8 Conversation Summaries

Concise summaries per conversation covering main points, facts learned, open questions and follow-up opportunities. Regeneration supersedes rather than overwrites, and every summary records the prompt and model that produced it.

## 3.9 Style Adaptation

Observed communication characteristics for both the contact and the user — typical length, formality, emoji rate, question rate, languages, active hours — used to shape suggestions. Below the minimum sample size, style is not applied.

---

# 4. Functional Requirements — Previously Unspecified

These thirteen areas were missing from version 1.0 and are specified here.

## 4.1 Synchronisation Scope

Unbounded synchronisation is both a performance risk and a privacy failure. Scope is therefore explicit and bounded.

| Requirement | Specification |
|---|---|
| Chat selection | The user selects which chats synchronise. Default: none until chosen. Modes: `selected_chats` (default), `all_private`, `manual` |
| History depth | Configurable horizon, default 365 days; unlimited is possible but never the default |
| Per-chat cap | Default 50,000 messages |
| Resumability | Backfill uses per-chat cursors; interruption never loses or duplicates data |
| Idempotency | Re-synchronisation is safe by unique constraint |
| Rate limiting | Throttled with configurable inter-request delay; `FLOOD_WAIT` honoured |
| Live updates | Optional; when disabled, the application works entirely on stored data |
| Media | Metadata always; file bytes only when enabled, with per-file and total caps |
| Exclusion | Any chat can be excluded from sync entirely, and this is honoured immediately |

## 4.2 Offline and Degraded Behaviour

The application is useful without a network connection and without any AI provider. This is a requirement, not a graceful accident.

**Available with no network:** browse all conversations · full-text search · view, create, edit and delete memories · manage goals · view relationship and style metrics · timing recommendations · export · backup · settings.

**Available with no AI provider:** everything above, plus keyword-and-recency memory retrieval. Analysis, summaries, memory proposals and reply suggestions are unavailable.

**Requirements:**

1. Features unavailable due to a missing dependency are **visibly disabled with an explanation**, never silently broken.
2. The application starts and is fully navigable with no Telegram connection.
3. Queued outbound actions are never sent automatically on reconnection — they are re-presented for approval.
4. Reconnection is automatic with backoff and visible status.

## 4.3 Startup Workflow

```
Load configuration → validate → fail fast on error
   ↓
Initialise logging with redaction
   ↓
Verify secret store availability   → refuse to start if required and unavailable
   ↓
Verify data directory and file permissions
   ↓
Check schema version → migrate (after backup) / refuse if newer
   ↓
Run integrity check if the last shutdown was unclean
   ↓
Build composition root
   ↓
Validate prompt registry            → fatal on mismatch
   ↓
Load plugins (bounded, isolated)
   ↓
Start scheduler
   ↓
Start presentation layer
   ↓
Connect to Telegram (non-blocking; the UI is usable before it completes)
```

**Requirements:** startup must not block on the network · every failure produces an actionable message · a partially failed startup still yields a usable application wherever possible · target cold start under 3 seconds to a usable interface.

## 4.4 Onboarding

First-run experience, covering the largest adoption hurdles.

1. **Welcome and honest scope** — what the application does, that it never sends without approval, and that it stores conversation data locally.
2. **Privacy disclosure** — local-first defaults, what enabling a cloud provider would mean, and the Phase 1 database-encryption limitation (`PRIVACY.md` §10).
3. **Telegram credentials** — guided acquisition of `api_id`/`api_hash` from my.telegram.org, with the secret stored in the credential store.
4. **Authentication** — phone, code, and two-factor password where enabled.
5. **Sync scope selection** — choose chats and history depth. Nothing synchronises before this step.
6. **AI provider setup (skippable)** — choose local, cloud or none, with data-boundary implications explained. Skipping yields a fully working non-AI application.
7. **Embedding model (skippable)** — download size and source disclosed before any download.
8. **Preferences** — tone, quiet hours, memory auto-approval categories.
9. **First sync** — with progress, cancellable.

**Requirements:** every step is skippable or reversible · no step requires a payment or account with this project (there is none) · onboarding can be re-run · the account-restriction risk of any third-party client is disclosed.

## 4.5 Timezone Handling

1. **All stored instants are UTC.** No naive datetimes anywhere in the system.
2. Conversion to local time happens **only** in the presentation layer.
3. The account timezone comes from the OS and is user-overridable.
4. Each contact may have a timezone, used for timing recommendations; when unknown, the account timezone is used and the recommendation says so.
5. Quiet hours and available hours are in the **user's** local time.
6. Daylight-saving transitions are handled by using IANA identifiers, never fixed offsets.
7. Displayed timestamps show relative time for recent items and absolute local time otherwise.

## 4.6 Localization

1. Interface strings are externalised from code; no user-visible string is hard-coded.
2. Interface language follows the OS by default and is user-overridable.
3. **Conversations are multilingual by nature** — a stated use case is language practice. The application never assumes a single language per contact, and stores an observed language mix in the style profile.
4. Prompts are language-aware: the reply prompt is instructed to respond in the conversation's dominant language unless the user specifies otherwise.
5. The embedding model is multilingual by default (ADR-018).
6. Dates, times and numbers are formatted per the interface locale.
7. Right-to-left layout is supported by the UI framework and is not obstructed by custom layout code.
8. English is the reference locale; missing translations fall back to English rather than showing keys.

## 4.7 Backup Policy

1. Automatic scheduled backups, default daily, configurable.
2. Mandatory automatic backup before **every** migration and before every purge.
3. Backups contain the database, schema revision, application version and a checksummed manifest.
4. Backups **never** contain secrets, session data or logs.
5. Backups written outside the application data directory are **always encrypted**.
6. Every backup is verified immediately after creation; an unverified backup is reported as failed.
7. Retention: default 7 daily and 4 weekly, configurable, oldest pruned first.
8. Restore validates compatibility, backs up the current state first, replaces atomically, and requires Telegram re-authentication.
9. Backup and restore are audit events.

## 4.8 Data Retention

1. Retention is configurable per data class and per chat (`PRIVACY.md` §6).
2. Defaults: messages and memories indefinite; analyses 180 days; suggestions and AI-call metadata 90 days; logs 14 days.
3. **Audit events are never deleted by retention policy.** Their retention may only be lengthened.
4. Retention runs daily, is idempotent, and reports what it affected.
5. Actions available: delete, archive or anonymise.
6. The user is shown what a policy change would affect **before** it is applied.
7. Retention never deletes user-pinned memories without explicit confirmation.

## 4.9 Import and Export

**Export formats:** complete JSON · scoped JSON (single contact or chat) · Markdown (human-readable conversations and summaries) · SQLite backup (full fidelity).

**Requirements:**

1. Export is available offline and requires no AI provider.
2. Exports exclude secrets and session data.
3. The **scoped export** is a first-class single action, so the user can answer a contact who asks what is stored about them (`PRIVACY.md` §7).
4. The JSON schema is documented and stable within a major version.
5. Import accepts exports from the same major version, validates against the schema, merges idempotently by unique key, and reports every skipped or conflicting record rather than failing silently.
6. Import never overwrites user-authored memories without confirmation.
7. Export and import are audit events.

## 4.10 Recovery Strategy

| Failure | Recovery |
|---|---|
| Unclean shutdown | `PRAGMA integrity_check` on next start; WAL recovery is automatic |
| Corrupted database | Detected at startup; offer restore from the most recent verified backup |
| Failed migration | Automatic rollback and restore from the pre-migration backup |
| Corrupted vector index | Rebuild from source text — embeddings are derived data |
| Lost or expired session | Prompt for re-authentication; no data loss |
| Sync gap or divergence | Cursor reset and re-sync; idempotent by unique constraint |
| Provider outage | Degrade to non-AI features (§4.2) |
| Secret store unavailable | Refuse to start rather than proceeding without session encryption |
| Plugin failure | Disable the plugin, continue |
| Disk full | Detect before write; pause sync and jobs; notify with the required action |

**Requirements:** every recovery path is tested · recovery never requires manual database editing · the user is always told what happened and what was done.

## 4.11 Multi-Account Support (Future)

Multi-account is post-1.0, but the **schema is ready from the first migration**, because retrofitting an ownership root is a breaking change.

1. `account_id` exists on every account-owned table from migration `0001`.
2. Every repository method is account-scoped; there is no unscoped query path.
3. Uniqueness constraints are account-scoped, so two accounts may know the same Telegram user.
4. Vector matrices load per account; cross-account retrieval is impossible by construction.
5. v1.0 enforces exactly one active account via a check constraint, removable in one migration.
6. Data never crosses accounts: no shared memories, no shared contacts, no shared suggestions.

## 4.12 Accessibility

1. **Full keyboard navigation.** Every action reachable without a mouse, with a visible focus indicator.
2. **Screen reader support** — accessible names, roles and descriptions on all controls; message lists announce sender and time.
3. **Contrast** meeting WCAG 2.1 AA (4.5:1 for text) in both light and dark themes.
4. **Scalable text** honouring OS scaling up to 200% without loss of function or clipping.
5. **No colour-only information** — status, confidence and severity always carry an icon or text label as well.
6. **Motion** respects the OS reduced-motion preference.
7. **The CLI is a first-class accessible interface** (ADR-030), fully usable with a screen reader for users who find it preferable.
8. **Confidence and uncertainty are conveyed in words**, not only as a numeric score or a colour.

Accessibility is verified during Milestone 10 with keyboard-only and screen-reader passes, not deferred to a post-release audit.

## 4.13 Configuration Management

Specified fully in `CONFIGURATION.md`.

1. Three stores with one rule: configuration (files/env), settings (database), secrets (OS credential store). A key lives in exactly one.
2. Built-in defaults are complete; the application starts with no configuration files.
3. Precedence: defaults → `default.yaml` → `local.yaml` → environment → CLI flags.
4. Configuration is typed and validated; unknown keys are a startup error.
5. Configuration is immutable at runtime; settings are mutable and emit events.
6. `config show` masks every secret.
7. Secret **names** appear in configuration; secret **values** never do.

---

# 5. Non-Functional Requirements

Each functional area has measurable non-functional requirements. Targets are provisional until Milestone 13, when they become binding.

| Area | Requirement | Target |
|---|---|---|
| **Startup** | Cold start to usable interface | < 3 s |
| **UI responsiveness** | No blocking of the UI thread | Frame budget maintained; no operation > 100 ms on the UI thread |
| **History paging** | Load the next page of messages | < 100 ms |
| **Message search** | Full-text search over 500k messages | < 200 ms |
| **Memory retrieval** | Semantic retrieval end to end | < 100 ms |
| **Suggestion latency** | Perceived time to a suggestion (cloud) | < 8 s |
| **Sync throughput** | Backfill rate within rate limits | ≥ 1,000 messages/minute |
| **Ingest** | Single live message persisted | < 50 ms |
| **Memory footprint** | Steady state with 500k messages | < 600 MB |
| **Database size** | Growth per 100k text messages | < 150 MB |
| **Reliability** | Data loss on abrupt termination | Zero committed data lost |
| **Reliability** | Sync interruption | Always resumable, never duplicated |
| **Correctness** | Domain and application test coverage | > 90% |
| **Correctness** | Every domain invariant | Has a corresponding test |
| **Security** | Secrets in logs, database or backups | Zero, verified by test |
| **Security** | Data boundary violations | Zero, verified by test |
| **Privacy** | Data leaving the device without consent | Zero |
| **Maintainability** | Architectural contract violations | Zero, enforced in CI |
| **Portability** | Windows, macOS, Linux | Core supported; Windows is the primary target |
| **Accessibility** | WCAG 2.1 AA for the desktop UI | Verified in M10 |
| **Cost** | Cloud AI spend | User-configurable daily and monthly caps, enforced |

---

# 6. Technology Stack

| Concern | Choice | Decision |
|---|---|---|
| Language | Python 3.12+ | ADR-002 |
| Telegram | TDLib (primary), Telethon (fallback adapter) | ADR-001, ADR-012 |
| Concurrency | asyncio; SQLite on a dedicated thread | ADR-013 |
| Desktop UI | PySide6 (LGPL) + qasync | ADR-014 |
| CLI | Typer | ADR-030 |
| Database | SQLite → PostgreSQL path | ADR-007, ADR-016 |
| Data access | SQLAlchemy Core + repositories | ADR-015 |
| Migrations | Alembic | ADR-015 |
| Embeddings | fastembed (local default) | ADR-018 |
| Vector search | NumPy exact → sqlite-vec | ADR-017 |
| Configuration | pydantic-settings + YAML | ADR-028 |
| Secrets | OS credential store via keyring | ADR-021 |
| Logging | structlog, JSONL files | ADR-027 |
| Plugins | pluggy + entry points | ADR-025 |
| Testing | pytest, hypothesis, pytest-qt | `TESTING.md` |
| Quality | ruff, mypy, import-linter | `CONTRIBUTING.md` |
| Packaging | uv (dev), PyInstaller (distribution) | `ROADMAP.md` M14 |

---

# 7. AI Architecture

Seven AI services behind separate ports (ADR-006), three of which are **deterministic and use no model** (relationship metrics, confidence calibration, timing). Several model-using services are satisfied by one batched call (ADR-029).

Every AI output is schema-validated, versioned by prompt and model, cost-instrumented, and bounded by a token budget. No AI output becomes permanent state without a user decision.

Specified in `AI_MODELS.md`, `PROMPTS.md` and `VECTOR_SEARCH.md`.

---

# 8. Data Storage

| Data | Location |
|---|---|
| Application data, embeddings, audit log | SQLite database |
| Secrets | OS credential store |
| Telegram session | Encrypted session store |
| Logs | Rotating JSONL files |
| Attachments, exports, backups, archives, models | Filesystem, by category |
| Configuration | YAML + environment |

Specified in `DATABASE.md` and `ARCHITECTURE.md` §7.

---

# 9. Privacy and Security

Local-first by default. Cloud AI is opt-in **per chat**. No telemetry. Every derived fact is visible, editable and deletable. Per-contact export and purge exist as single actions so the user can honour a request from someone they talk to.

Specified in `PRIVACY.md` and `SECURITY.md`.

---

# 10. Future Features

Voice transcription · voice reply suggestions · image understanding · calendar integration · conversation analytics · local model expansion · multi-account · plugin marketplace · mobile companion · web dashboard · knowledge-graph memory · cross-device synchronisation.

All future features are optional, modular, and preferably plugins (ADR-009).

---

# 11. Success Criteria

The MVP is successful when it can:

1. Connect to Telegram and synchronise a bounded, user-selected scope reliably and resumably.
2. Maintain long-term memory whose every entry is traceable, reviewable and deletable.
3. Generate context-aware reply suggestions and **explain what they were based on**.
4. Adapt to different contacts through goals, style and relationship signals.
5. Recommend manual intervention when confidence is genuinely low.
6. Preserve user and third-party privacy by default.
7. Remain useful with no AI provider and no network.
8. Be extended without modifying core code.
9. Pass every architectural, security and privacy test in CI.

---

# 12. Out of Scope (v1.0)

Group conversation management · voice and video calls · multi-device synchronisation · enterprise deployment · cloud infrastructure operated by this project · automated sending of any kind · mobile applications · a plugin marketplace.

**Permanently out of scope**, not merely deferred: unattended auto-reply, synthetic typing indicators, and any feature whose purpose is to make automated activity appear human (ADR-023).

---

# 13. Milestones

**`ROADMAP.md` is the authoritative milestone plan.** Version 1.0 of this document defined a conflicting list; it has been removed rather than maintained in two places.

---

# 14. Development Workflow

Analyse → design → explain trade-offs → obtain approval → implement → test → refactor → document → update this specification if requirements change.

Specified in `DEVELOPMENT_WORKFLOW.md` and `CLAUDE_WORKFLOW.md`.

---

# 15. Document Authority

| Document | Authoritative for |
|---|---|
| `PROJECT_SPEC.md` | Requirements — what the system must do |
| `ROADMAP.md` | Sequencing — when it gets built |
| `ARCHITECTURE.md` | Structure — how layers and components relate |
| `MASTER_ARCHITECTURE.md` | Diagrams — dependencies, flows, lifecycles, sequences |
| `DOMAIN_MODEL.md` | The domain — entities, invariants, vocabulary |
| `DATABASE.md` | Schema — derived from the domain model |
| `API.md` | Interfaces — ports and contracts |
| `AI_MODELS.md` | AI pipeline, providers, retrieval, validation |
| `PROMPTS.md` | Prompt engineering |
| `VECTOR_SEARCH.md` | Embeddings and semantic retrieval |
| `SECURITY.md` | Threat model and technical controls |
| `PRIVACY.md` | Privacy commitments and data lifecycle |
| `ERROR_HANDLING.md` | Error taxonomy and policy |
| `CONFIGURATION.md` | Configuration, settings and secrets |
| `PLUGIN_SYSTEM.md` | Extension architecture |
| `DECISIONS.md` | Why things are the way they are |
| `TESTING.md` | Verification strategy |
| `CONTRIBUTING.md` | How to work on the project |
| `CHANGELOG.md` | What changed |

Where two documents conflict, the one listed as authoritative for that concern wins, and the other is a defect to be fixed.
