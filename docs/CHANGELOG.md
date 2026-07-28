# CHANGELOG.md

All notable changes to this project will be documented in this file.

This project follows the principles of **Keep a Changelog** and **Semantic Versioning (SemVer)**.

Version numbers follow:

```
MAJOR.MINOR.PATCH
```

---

# Change Categories

Use the following categories when applicable:

- Added
- Changed
- Deprecated
- Removed
- Fixed
- Security
- Performance
- Documentation
- AI
- Database
- Breaking Changes

Not every release requires every category.

---

# [Unreleased]

## Added

### Milestone 0 — Foundation & Tooling

**Project structure**

- Clean Architecture package tree under `src/tgassist/` with `domain`, `application`, `infrastructure` and `presentation` layers. Every package states its responsibility in a docstring, verified by test.
- Supporting trees outside the source package: `config/`, `prompts/`, `migrations/`, `resources/`, `scripts/`, `plugins/`, `tests/`.

**Build and tooling**

- `pyproject.toml` with `uv`, hatchling build backend, Python 3.12 floor, and a development environment pinned to 3.13.
- Ruff for linting *and* formatting (Black is not used — Ruff's formatter replaces it, per Step 2's condition).
- mypy in strict mode over source *and* tests, with the pydantic plugin.
- pytest with marker taxonomy (`integration`, `e2e`, `eval`, `slow`), coverage configuration, and `filterwarnings = ["error"]`.
- pre-commit hooks: Ruff, file hygiene, private-key detection, gitleaks, mypy, import-linter.
- `poethepoet` task runner: `poe lint | format | typecheck | arch | test | cov | check`.
- GitHub Actions CI across Windows and Linux on Python 3.12 and 3.13, plus a security job running `pip-audit` and `gitleaks`.
- `.editorconfig`, and a `.gitignore` covering databases, sessions, backups, logs, exports and models.

**Architectural enforcement**

- `.importlinter` contracts encoding the corrected dependency rule (ADR-011): layered architecture, domain independence, domain free of third-party imports, infrastructure isolation, and presentation never importing infrastructure directly.
- `tests/architecture/` asserting the same rules by AST inspection, plus package-structure and circular-import checks. Verified to fail on a deliberately introduced violation.

**Configuration system**

- Typed, validated, immutable configuration (`pydantic-settings`) with complete built-in defaults — the application starts with no configuration files present.
- Six-layer resolution: defaults → `default.yaml` → profile → `local.yaml` → environment → explicit overrides.
- Environment profiles: `development`, `testing`, `production`.
- Unknown keys are a startup error, not a silent ignore.
- Per-key origin tracking, surfaced by `config show`.
- `logging.diagnostic_mode` is rejected by validation in the production profile, because it writes third-party conversation content to disk.

**Logging**

- structlog routed through the standard library, so records from third-party packages pass through the same processor chain.
- Console and size-rotating file sinks; JSON or human-readable rendering; per-logger level overrides; age-based retention cleanup.
- Central redaction processor applying a domain-owned sensitivity policy.

**Composition root**

- Hand-written `Container` supplying configuration, logging, directory creation and a permission report. No DI framework: the object graph is fixed and single-process.

**Command line adapter**

- `tgassist version`, `config show`, `config validate`, `config path`, `doctor`.
- `doctor` reports unimplemented subsystems explicitly rather than returning green while silently omitting them.

**Tests**

- 123 tests: 26 architectural, 97 unit. 92% statement coverage.

## Fixed

Three defects found by the Milestone 0 test suite before any of this shipped:

- **Configuration origin tracking attributed whole sections to one file.** A nested mapping absent from the merge target was assigned wholesale, so a later layer overriding one key left the rest credited to the wrong source.
- **Secrets embedded in a larger string were not redacted.** The value-shape patterns were anchored, so a bare key was caught but a formatted message from a third-party library carried the credential through to the log file. Patterns are now unanchored and the matched span is substituted, keeping the message readable.
- **Keys that name a secret were masked as though they held one.** `require_secret_store` displayed as `********`. Configuration stores secret *names*, not values (ADR-021), so masking them hid information the user needs while protecting nothing.

## Changed

- Exception classes carry the PEP 8 `Error` suffix (`ruff` rule `N818`). `ERROR_HANDLING.md` §3 and the `API.md` error table were updated to match.
- `CONFIGURATION.md`: documented environment profiles and the six-layer resolution order; removed the separate `logging.yaml`, whose keys the main configuration already owns; added an implementation-status note recording that sections arrive with their subsystems.
- `ERROR_HANDLING.md`: added an implementation-status note distinguishing the specified taxonomy from the branches that exist in code.

## Documentation

Architecture stabilization. All project documentation reviewed for internal consistency and corrected.

### Corrected

- **`ARCHITECTURE.md` §9 — dependency direction.** Version 1.0 documented the Clean Architecture dependency rule inverted (`Domain → Infrastructure` allowed, `Infrastructure → Domain` forbidden), which contradicted both ADR-003 and `ARCHITECTURE.md` §2. Implemented literally it would have placed database, Telegram and AI provider imports inside the domain layer. Corrected per ADR-011 and now enforced in CI by `import-linter` contracts.
- **`ARCHITECTURE.md` §2 — high-level diagram.** Version 1.0 showed replies flowing automatically from the Reply Generator through to the Telegram network, contradicting ADR-010. The gateway is now shown as an inbound adapter with a separate, explicitly user-initiated outbound path.
- **`ARCHITECTURE.md` §10 — folder structure.** `tests/` and `docs/` were nested inside `src/`. Corrected.
- **Milestone numbering.** `PROJECT_SPEC.md` §10 defined milestones 1–6 that conflicted with `ROADMAP.md`'s 0–12 (for example, SPEC M5 "Desktop UI" versus ROADMAP M5 "Relationship Intelligence"). `ROADMAP.md` is now the single authority; the conflicting list was removed.
- **Milestone ordering.** `ROADMAP.md` v1.0 placed Conversation Processing (M3) and Memory Engine (M4) before AI Services (M7), although both require a language model. AI abstraction moved to M3; persistence moved before Telegram connectivity.
- **`CHANGELOG.md` 0.1.0 entry.** The previous entry claimed shipped work — initial SQLite schema, repository abstraction, provider abstraction, conversation planner, reply generator interfaces — none of which existed. Corrected to a documentation-only entry below.
- **`PROJECT_SPEC.md` automation clause.** The phrase "unless the user enables automation" implied an automation mode with no defined boundary. Removed; the boundary is now a product constraint (ADR-023).
- **Relationship metrics.** `trust_score` and `friendship_level` were specified with no definitions, ranges or update rules, making them unfalsifiable and untestable. Replaced with deterministic metrics that each have a published formula and a minimum sample size.

### Added

- `DOMAIN_MODEL.md` — 29 entities, value objects, invariants, aggregates, domain services, event catalogue and a metric glossary. Now the authority from which the schema is derived.
- `PRIVACY.md` — privacy principles, data categories, lifecycle, user and contact rights, known limitations.
- `ERROR_HANDLING.md` — exception taxonomy, retry and timeout policy, degradation strategy, anti-patterns.
- `CONFIGURATION.md` — three-store ownership rule, resolution order, complete key reference.
- `PLUGIN_SYSTEM.md` — plugin capabilities, lifecycle, fault isolation and an explicit trust model.
- `VECTOR_SEARCH.md` — embedding pipeline, hybrid retrieval scoring, store implementations, re-indexing.
- `MASTER_ARCHITECTURE.md` — nine Mermaid diagrams covering dependencies, flows, lifecycles and sequences.
- `README.md` — project overview, status, structure and documentation index.
- Twenty new Architecture Decision Records (ADR-011 through ADR-030), all **Proposed**.

### Changed

- `PROJECT_SPEC.md` — thirteen previously unspecified requirement areas added: synchronisation scope, offline behaviour, startup workflow, onboarding, timezone handling, localization, backup policy, data retention, import/export, recovery strategy, multi-account readiness, accessibility, configuration management. Non-functional requirements now paired with measurable targets.
- `API.md` — added `UnitOfWork`, `SecretStore`, `PromptRepository`, `VectorStore`, `MigrationRunner`, `Scheduler`, `Cache`, `Clock`, `IdGenerator`, `FileStore`, `NotificationPort`, `MessageSearchPort`, `AuthorizationHandler`, `StructuredOutputValidator`, `PluginContext`. Separated `VectorStore` from `EmbeddingProvider`. Defined event bus delivery semantics. Added capability negotiation to `LLMProvider`. All I/O methods are now async.
- `DATABASE.md` — added `accounts`, `user_profiles`, `telegram_sessions`, `conversations`, `attachments`, `memory_proposals`, `memory_revisions`, `style_profiles`, `conversation_plans`, `reply_suggestions`, `behavior_recommendations`, `embedding_models`, `sync_cursors`, `notifications`, `ai_providers`, `ai_calls`, `retention_policies`, `audit_log`. Added 18 unique constraints, 4 partial unique constraints, 31 foreign keys and 24 check constraints. Added migration, archive, backup, soft-deletion and multi-account policies, and a complete Mermaid ER diagram.
- `AI_MODELS.md` — added capability matrix, latency and cost expectations, prompt injection mitigation, memory poisoning prevention, confidence calibration, structured output validation, retry and fallback policy, embedding lifecycle, context window management and semantic retrieval strategy. Documented the memory proposal workflow.
- `SECURITY.md` — added an explicit threat model with out-of-scope threats, phased encryption strategy, session encryption, secret management, plugin trust model, audit logging, prompt injection defenses, AI provider data boundaries, compliance considerations and backup encryption.
- `ROADMAP.md` — resequenced, expanded to 15 milestones with falsifiable acceptance criteria, complexity estimates and an explicit scope-risk section.
- `DECISIONS.md` — restructured with an index; ADR-001 through ADR-010 preserved and cross-referenced to the new decisions.

### Database

- Removed the `logs` table. Application logs move to rotating JSONL files; durable security events move to a new append-only `audit_log` table (ADR-027). The original design caused write contention with the single-writer model and made logs unreadable when the database was the failing component.
- Renamed and generalised `ai_analysis` to `analyses`, which was message-scoped only and left conversation-level analyses homeless.

### AI

- Established that AI-derived memories are **proposals** requiring user approval or an explicit auto-approval rule (ADR-019).
- Established capability negotiation and mandatory schema validation with a single repair attempt (ADR-020).
- Established composite execution behind separate ports to bound per-message cost (ADR-029).
- Specified that relationship metrics, confidence calibration and timing recommendations use **no language model**.

### Security

- Established the automation boundary as a structural constraint: no auto-send, no synthetic typing indicators, enforced through the dependency graph rather than configuration (ADR-023).
- Established local-first defaults with per-chat AI data boundaries (ADR-024).
- Established OS credential store as the primary secret backend, with no plaintext fallback (ADR-021).
- Documented prompt injection defenses and their honest limitations.

---

# [0.1.0] - 2026-07-28

Initial documentation release. **No source code exists at this version.**

## Added

### Documentation

- CLAUDE.md
- CLAUDE_WORKFLOW.md
- DEVELOPMENT_WORKFLOW.md
- PROJECT_SPEC.md
- ARCHITECTURE.md
- AI_MODELS.md
- PROMPTS.md
- DATABASE.md
- API.md
- ROADMAP.md
- DECISIONS.md
- SECURITY.md
- TESTING.md
- CONTRIBUTING.md
- CHANGELOG.md

### Project

- Repository initialized.
- Clean Architecture adopted as the target architecture (ADR-003).
- Domain-driven folder structure designed.

### Decisions

- ADR-001 through ADR-010 accepted.

---

# Release Template

```
# [Version] - YYYY-MM-DD

## Added

## Changed

## Deprecated

## Removed

## Fixed

## Security

## Performance

## Documentation

## AI

## Database

## Breaking Changes
```

---

# Versioning Policy

## MAJOR

- Breaking API changes
- Major architectural redesign
- Incompatible database schema
- Significant plugin API changes

## MINOR

- New features
- New AI capabilities
- New plugins
- New database tables
- New UI functionality

## PATCH

- Bug fixes
- Documentation improvements
- Small performance improvements
- Prompt refinements that do not significantly change external behavior
- Internal refactoring without breaking compatibility

---

# AI Change Policy

Whenever AI behavior changes, document: prompt updates, model changes, embedding model changes, memory retrieval changes, context-building strategy, confidence estimation adjustments, evaluation benchmark results.

Prompt and model changes must record the version identifiers, because every persisted AI artifact references them and cache invalidation depends on them.

---

# Database Change Policy

Document new tables, schema changes, index additions, migration updates and data model redesigns.

Every schema change requires a reversible migration and a corresponding update to `DOMAIN_MODEL.md` first.

---

# Documentation Policy

Whenever project documentation changes, record new documents, major revisions, removed documentation and architectural updates.

---

# Security Policy

Security entries should include vulnerability fixes, dependency updates addressing security issues, authentication improvements, encryption enhancements and privacy-related changes.

---

# Breaking Changes Policy

Always describe what changed, why it changed, how developers should migrate, and whether compatibility layers exist.

---

# Release Checklist

- All planned roadmap tasks completed
- Tests passing, including architectural, security and privacy tests
- Documentation synchronized with implementation
- Database migrations verified up and down
- Security review completed
- AI evaluation completed
- Version number updated
- CHANGELOG.md updated

---

# Maintenance Guidelines

- Add entries as work is completed rather than waiting until release day.
- Keep entries concise but informative.
- Avoid duplicating commit messages.
- Group related changes together.
- Preserve historical entries; never rewrite published release notes.
- **Never record work that has not been done.** A changelog that overstates state is worse than none.

---

# Philosophy

The changelog is the historical record of the project. It should answer: what changed, why, whether it affects users or developers, whether it requires migration, and whether it improves security, performance or AI quality.
