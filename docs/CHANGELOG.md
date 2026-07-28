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

### Milestone 1.3 -- Contact Aggregate

The first aggregate describing somebody other than the operator. Its key appears
in six tables that do not exist yet -- memories, goals, chats, relationship and
style profiles, suggestions -- which made this the last cheap moment to choose
that key deliberately.

**Domain**

- `Contact` -- frozen, self-validating, with `archived`, `deleted`, `restored`, `renamed` and `with_username`. Each returns `self` when the change is a no-op.
- **Identity is a locally generated `ContactId`**, not the Telegram identifier (ADR-041). The same person can be known to two accounts, so `telegram_user_id` is not unique in the table; a natural key would have to be the pair, and every child table would carry both columns in its foreign key.
- **Lifecycle: `active ⇄ archived`, either state to `deleted`, and one `restored` transition back** (ADR-042). Two nullable timestamps, at most one ever set, rather than booleans -- retention has to ask "deleted before when", and a boolean cannot answer that.
- `validate_username` -- structural Telegram handle validation with normalisation: a leading `@` is stripped, 5-32 characters, letter first, no trailing underscore.

**Persistence**

- Migration `0004`: `contacts`, the first table with many rows per account, and so the first whose indexes serve queries rather than only constraints.
- **The unique index on `(account_id, telegram_user_id)` covers soft-deleted rows.** A deleted contact still holds that person's history; a second row for them would split it. Re-adding is therefore refused, and the caller is told to restore instead -- which is why `get_by_telegram_id` takes `include_deleted` and creation passes it.
- Index `(account_id, created_at, id)`, matching the listing query and its keyset tiebreaker. `account_id` leads because every query this table serves is scoped.
- Seven check constraints, including `archived_at IS NULL OR deleted_at IS NULL`, each with a test proving it rejects a bad row.

**Application**

- `CreateContact`, `GetContact`, `ListContacts`, `ChangeContactStatus`. The last is one use case parameterised by `ContactTransition` rather than three near-identical classes, because archive, restore and delete are the same transaction with a different entity method.
- `resolve_account` extracted to `use_cases/account_scope.py`: every account-scoped use case begins by resolving the active account, and the rule was already duplicated between the profile use cases.

**CLI**

- `contact add`, `show`, `list`, `archive`, `restore`, `delete`, each accepting `--account`.

**Tests**

- Contact runs the shared Milestone 1.0 contract suite for the first time as an account-scoped collection -- including its **soft-deletion branch, which no aggregate had ever executed**. An untested contract clause is a clause that is probably wrong.
- A second suite covers what only matters when rows belong to somebody: ownership, foreign-key integrity, cascade deletion, scope isolation, uniqueness within and across accounts, and the archive lifecycle.
- `tests/fakes/pagination.py` extracts the keyset paging the fakes need. It is the code most likely to be subtly wrong in a way the contract suite would then pass for the wrong reason, so it is written once and verified by every aggregate's contract run. The account fake now uses it, and its contract suite still passes.

## Fixed

- **Archiving a deleted contact reported "not found".** `ChangeContactStatus` excluded deleted contacts from its lookup for every transition except restore, so the entity's own rule -- that a deleted contact must be restored before it can be archived -- was never reached, and the caller got a message that was untrue. The lookup now includes deleted contacts for every transition and lets the entity judge legality. Found by a test, not by review.

## Changed

- `DOMAIN_MODEL.md` §5.4 corrected: identity, the three-state lifecycle, the reduced attribute set, and each deferred field with its reason.
- `DATABASE.md`: `contacts` documented as implemented; the migration plan renumbered into milestone order, with a note that numbers beyond `0004` are a plan rather than a commitment.
- `API.md`: the implemented `ContactRepository`, and why `get_by_username`, `search`, `soft_delete` and `purge` are absent.

## Architecture Decisions

- **ADR-041 -- Contact Identity Is a Local Surrogate Key** (Proposed). Weighs the natural, composite, surrogate and dual arrangements, and records why the unique index deliberately covers soft-deleted rows.
- **ADR-042 -- Contact Lifecycle** (Proposed). Three states with immediate value; `discovered` and `dormant` deferred as unrepresentable today; `is_blocked` deferred until something processes contacts.

## Scope note

Nineteen source and test files were created or modified, within the twenty-file limit.

## Fixed

### Maintenance -- CLI logging configuration (ADR-040, now Accepted)

**The CLI never configured logging, so every log record was printed to standard
output, unfiltered and unredacted.** `_open` called `Container.create` with
`configure_logging_on_start=False` to keep command output clean. Suppressing the
call did not silence logging: structlog falls back to its default `PrintLogger`,
which writes to standard output at every level. The comment described the
opposite of what the code did.

Two consequences, one cosmetic and one not:

- Records appeared in the middle of command output, so no command's output was
  deterministic.
- The whole `logging` configuration section was ignored on this path --
  including `mask_secret_values`. Records emitted by a CLI command had never
  passed through redaction. Nothing secret was being logged yet, which is why
  this is a fix rather than an incident, but Milestone 2 introduces records that
  carry Telegram identifiers and message metadata.

**The fix is one line**: `_open` no longer suppresses the call. The CLI now uses
the same logging configuration as every other entry point, with no CLI-specific
default and no second initialisation path -- `configure_logging` remains the
only place logging is set up, and it already resets handlers, so repeated
invocation in one process cannot accumulate them.

The console handler writes to **standard error**, so configured output never
reaches standard output. This is why the proposal's suggestion of a CLI-specific
"console off by default" was dropped: it would have bought nothing, while giving
`console_enabled` a second answer and ignoring the user's setting -- the same
class of defect the ADR exists to remove.

**Configuration**

- `config/default.yaml` now ships `component_levels` for `asyncio`, `alembic`
  and `sqlalchemy.engine` at `WARNING`. Routing every record through one chain
  is deliberate, since redaction must cover more than our own call sites, but at
  `DEBUG` it also meant the event loop announcing its selector. The
  application's own records are untouched, and a developer who wants the detail
  raises the level.

**Tests**

- Twelve tests in `TestCliLoggingStartup`: structlog is configured by a command;
  records do not reach standard output; standard output is byte-identical across
  runs; records do reach standard error; the configured level is honoured; debug
  appears only when configured; `console_enabled: false` silences it; redaction
  is installed on this path; the file sink receives command records; repeated
  invocation duplicates neither handlers nor records; third-party libraries stay
  quiet.
- `restore_logging` now also calls `structlog.reset_defaults()`. structlog's
  configuration is process-wide, so a test invoking a command would otherwise
  leave every later test running against whatever that command installed. This
  immediately exposed an order-dependent test that had been asserting on
  configuration leaked from an earlier one; it now configures logging itself.
- The Milestone 1.2 test that compared only the profile fields a command printed
  compares whole output again, the workaround for this defect being gone.

## Added

### Milestone 1.2 -- UserProfile Vertical Slice

The first account-owned aggregate. Everything after this one is account-scoped
too, so the point of the slice was to settle how ownership, cascade and scoping
work while the aggregate is small enough to get them right.

**Domain**

- `UserProfile` -- frozen, self-validating, with `with_language`, `with_tone`, `with_message_length`, `with_emoji_usage` and `with_quiet_hours`. Each returns `self` when the value is unchanged, so a redundant edit does not move `updated_at`.
- **Identity is the account.** `account_id` is the primary key; there is no surrogate key, because an account has exactly one profile and a second name for one row is how a query eventually reads by one and writes by the other (ADR-038).
- `TimeRange` -- quiet hours as minutes past midnight, so a period crossing midnight compares correctly. A pair of naive times makes `22:00-08:00` look empty. Equal bounds are refused as ambiguous between an empty range and the whole day.
- `TonePreference`, `MessageLength`, `EmojiUsage` as `StrEnum`s, stored as their values rather than ordinals.
- `validate_language` -- structural BCP-47 validation with normalisation (`EN-gb` becomes `en-GB`). Structural rather than registry-based: an unregistered tag simply matches nothing, whereas a malformed one breaks parsing everywhere it is used.

**Persistence**

- Migration `0003`: `user_profiles`, the first table with a foreign key. `account_id` is simultaneously primary key and foreign key with `ON DELETE CASCADE`, so one profile per account and deletion of orphans are both structural. No extra index on `account_id`: the primary key already is one.
- Seven check constraints restating the entity's invariants, each with a test proving it rejects a bad row rather than merely that the table exists.
- Every column `NOT NULL` with a server default -- no nullable column standing in for "not decided yet".
- `UserProfileMapper` with round-trip and column-coverage tests.
- A test asserts `PRAGMA foreign_keys` is actually on, because SQLite ignores foreign keys without it and the cascade would otherwise be decorative.

**Application**

- `GetUserProfile` creates a default profile on first access, so adding an account does not require deciding preferences before the application is usable.
- `UpdateUserProfile` with `ProfileChanges`, where `None` means "leave alone" -- a partial update without inventing a null-means-unset convention in the entity.

**CLI**

- `profile show` and `profile set --language --tone --length --emoji --quiet-hours --account`.

**Tests**

- A contract suite running both implementations against ownership, foreign-key integrity, cascade deletion and scope isolation. The in-memory fake deliberately shares one store across scoped instances, so isolation is genuinely tested rather than passing because the fake had nothing to leak.
- One test inspects the signatures and asserts no repository method accepts an account, so a future method reintroducing the parameter fails a test rather than a review.

## Changed

- `ScopedRepositoryFactory` added to `domain/ports/repository.py`: account-owned repositories are scoped at construction and **no method takes an account identifier** (ADR-039). The conventional `get(account_id)` form places correctness at every call site forever, and its failure mode is silently returning another account's data.
- `DOMAIN_MODEL.md` §5.2 corrected: identity, the reduced attribute set, and each deferred field with its reason.
- `DATABASE.md`: `user_profiles` documented as implemented; the migration plan renumbered to one aggregate per migration.
- `API.md`: new §7.2 on account-scoped construction, and the `UserProfileRepository` interface with the reasoning for having no `delete` and no `list`.

## Fixed

- Nothing in this milestone's code. One pre-existing defect was found here and fixed in the maintenance entry above -- see ADR-040.

## Architecture Decisions

- **ADR-038 -- UserProfile Identity Is the Account** (Proposed). Drops the surrogate key, and defers `display_name`, `timezone`, `available_hours`, `auto_approve_memory_categories` and `confidence_thresholds` -- the first two because Account already owns them, the rest because the vocabulary each draws on does not exist yet and a column that accepts anything is worse than an absent one.
- **ADR-039 -- Account Scope Is a Constructor Parameter** (Proposed). Establishes scoped repositories for every account-owned aggregate to come.
- **ADR-040 -- The CLI Does Not Configure Logging** (**Accepted**; found here, fixed in the maintenance entry above). Found by comparing the output of two identical `profile show` runs. `_open` suppressed `configure_logging` to keep command output clean; the effect was the opposite, because unconfigured structlog defaults to a `PrintLogger` on standard output with no level filtering. The whole `logging` configuration section -- including secret redaction -- was therefore ignored for every CLI command. Left unapplied in this milestone as it changed behaviour outside its scope.

## Scope note

Eighteen source and test files were created or modified, within the twenty-file limit.

### Milestone 1.1 -- Account Aggregate

The first real business aggregate, exercising the whole architecture end to end:
domain, repository, mapper, migration, unit of work, dependency injection, CLI,
tests and documentation -- with no Telegram or AI involved.

**Domain**

- `Account` -- frozen, self-validating, with `activated`, `deactivated` and `renamed` returning new instances. Each returns `self` when the change is a no-op, so a redundant call does not move `updated_at` and make nothing look like something.
- `AccountId` and `TelegramUserId` as `NewType` aliases: statically non-interchangeable under `mypy --strict`, with no wrapping noise at call sites.
- `validate_timezone` -- IANA identifiers only, resolved through `zoneinfo`. A fixed offset is refused because it cannot express daylight saving, and reply-timing advice is computed against local hours.
- `DomainValidationError`, inheriting both `DomainError` and `ValueError`, so idiomatic `except ValueError` still works while the error carries a code and a user-facing message.
- `ConflictError` for a conflict detected before writing, so the message names the conflict rather than a column.
- `AccountCreated` and `AccountActivated` events.

**Persistence**

- Migration `0002`: the `accounts` table with four check constraints and two unique indexes.
- A **partial unique index** on `is_active` makes the single-active invariant structural: a second activation fails at the database rather than depending on every caller remembering to deactivate first. Declared for both SQLite and PostgreSQL.
- `AccountMapper` with a round-trip test and a column-coverage test that fails the moment a migration adds a column the mapper does not write.
- `SqlAccountRepository` -- six operations, each traceable to a caller that exists.

**Application**

- `CreateAccount`, `GetAccount`, `ListAccounts`, `SetActiveAccount`. Separate classes rather than one service, because a class states its dependencies in its constructor and a shared service would demand everything for everyone.
- The first account created becomes active automatically, so a fresh installation is not left in a state where nothing works for no visible reason.

**CLI**

- `account create`, `account show`, `account list`, `account activate`.

**Tests**

- 579 passing. Both repository implementations run the Milestone 1.0 contract suite plus account-specific obligations, and a further suite asserts they agree with each other.
- Migration tests assert the check constraints and the partial index actually reject bad rows, rather than merely that the table exists.

**Dependencies**

- Added `tzdata`, required for IANA timezone resolution on Windows, which has no system timezone database.

## Fixed

- **Domain validation escaped as an unhandled traceback.** Invariant failures raised bare `ValueError`, which is not in the taxonomy the CLI catches, so an invalid timezone showed a stack trace instead of a message. Found by exercising the CLI rather than by a test, which is the argument for the CLI existing.

## Changed

- `DOMAIN_MODEL.md` §5.1: Account's lifecycle corrected, invariants stated in full, deferred fields named with reasons, and the identifier implementation note added.
- `DATABASE.md`: the `accounts` table documented as implemented; business migrations renumbered from `0002` to `0003` onward.
- `API.md` §9: the implemented `AccountRepository` interface, and why it has no `delete`.

## Architecture Decisions

- **ADR-037 -- Account Lifecycle Separated from Session Lifecycle** (Proposed). The documented lifecycle named six states while providing one boolean, and three of the six duplicated Session's. Two entities would have owned "is this account authenticated" and would eventually have disagreed. Account now owns only whether the user selected it.

## Scope note

Sixteen source files were modified or created, within the twenty-file limit.

### Milestone 1.0 -- Repository Contracts

**Domain**

- `SortDirection`, `SortOrder`, `PageRequest`, `TimeWindow` -- query intent expressed as domain value objects. `SortOrder.field` is a domain field name, never a column, so a schema rename cannot reach the application layer.
- `RepositoryFactory` -- a callable taking a unit of work. Use cases declare the repositories they need as constructor parameters, which keeps their real dependencies visible in their signature.
- `domain/ports/repository.py` -- the repository contract, stated as obligations rather than as a base class, with the reasoning for that choice.

**Infrastructure**

- `KeysetPaginator` -- ordering, cursor position and lookahead in one place. **Requires a unique tiebreaker column**, because ordering by a non-unique column alone silently skips rows.
- `Cursor` extracted to its own module, with lossless datetime encoding.
- `EntityMapper` -- the mapping framework, with the four properties a mapper must satisfy stated as a contract and verified by test.
- `Repository.fetch_page` rewritten to take a `PageRequest` and a paginator, so the tiebreaker requirement is the framework's responsibility rather than each repository's to remember.

**Testing**

- `tests/support/repository_contract.py` -- a reusable contract suite every future repository inherits. Covers identity, round-trip equality, snapshot semantics, pagination completeness and stability, malformed cursors, sort inversion, transaction participation, and optional soft-delete and count capabilities.
- `tests/support/sample_aggregate.py` -- a toy aggregate that exists only to exercise the framework, with a deliberately non-unique sort column so the tiebreaker is tested where it actually matters. Test scaffolding: its table is created by a fixture and never reaches a user's database.
- Two independent implementations -- SQLAlchemy and in-memory -- run the identical suite, plus a further suite asserting they agree with each other.
- 443 tests passing.

**Composition root**

- `Container.repository(factory, uow)` for wiring. No business registrations.

## Fixed

- **Cursors silently skipped rows.** Encoding stringified a `datetime` through a generic `str()` fallback; decoding produced text, and binding text against a `DateTime` column compared the wrong things -- returning wrong rows without any error. Datetimes now encode as ISO-8601 and decode back to the column's own Python type. Found by the contract suite on its first run against both implementations, which is precisely the failure that suite exists to catch.

## Changed

- `API.md` §7: recorded that there is no generic repository interface and why; documented `SortOrder`, `PageRequest`, the mandatory pagination tiebreaker, and repository construction via factories.
- `DATABASE.md` §14: added the mapping contract, identity and loading policy, and the absence of optimistic locking.

## Architecture Decisions

- **ADR-035 -- No Generic Repository Base** (Proposed). A generic CRUD interface is wrong for four of the five aggregates examined and would break the guarantee that an audit trail cannot be rewritten. `ReadRepository`/`WriteRepository`, `Specification` and a factory registry are omitted for stated reasons rather than by oversight.
- **ADR-036 -- No Optimistic Locking** (Proposed). The database-level lost update is structurally impossible under serialized transactions. The remaining think-time race is narrow and better served by memory revisions, which merge rather than reject. Records the three conditions that would require revisiting.

### Milestone 0.2 -- Persistence Foundation

**Ports (domain layer)**

- `Database` -- connection lifecycle and health, with `HealthReport` and `PragmaState`.
- `UnitOfWork` -- transaction boundary, savepoints, and event release gated on commit.
- `UnitOfWorkFactory` -- use cases open their own transaction rather than receiving one.
- `MigrationRunner` -- schema status, upgrade, downgrade, and a pre-upgrade hook.
- `Page` -- keyset pagination. Every collection query uses a cursor, never an offset, because `OFFSET 50000` makes the database walk and discard fifty thousand rows to return twenty.

**Implementations (infrastructure)**

- `DatabaseExecutor` -- the single dedicated worker thread all database work runs on (ADR-013).
- `SqliteDatabase` -- SQLAlchemy Core engine, pragma application and read-back verification, health checks reporting integrity, foreign keys, page counts and schema revision.
- `SqlAlchemyUnitOfWork` -- transactions, savepoints, error normalisation, and events withheld until commit.
- `Repository` base -- transaction-aware execution, keyset pagination, and constraint-violation messages phrased in the domain rather than the schema. No business repositories: those arrive with Milestone 1.
- `Cursor` -- opaque pagination tokens; a malformed one is treated as absent, because a stale bookmark should start from the beginning rather than raise.
- Mapping utilities for datetimes, booleans and JSON, with naive datetimes refused at the storage boundary and stable JSON key ordering so content fingerprints do not shift.
- `AlembicMigrationRunner` -- drives Alembic against the application's own connection, so migrations run on the database thread with the application's pragmas in force.

**Migration framework**

- `alembic.ini`, `migrations/env.py` and a script template. Batch mode is enabled, because SQLite cannot alter a column in place.
- Migration `0001` creates `schema_metadata`. A baseline that creates nothing cannot be tested in either direction, so the machinery would go unverified until the first business table.
- Constraint naming conventions declared once. Without them SQLite invents names, and an unnamed constraint cannot be dropped by a later migration.

**Composition root**

- `Database`, `UnitOfWorkFactory` and `MigrationRunner` registered and overridable by constructor injection.
- `start_database()` opens the database, checks the schema position and migrates when configured, refusing outright if the database was written by a newer version.
- Async lifecycle (`aclose`, `async with`) so the worker thread is released deterministically.

**Configuration**

- `database` section: path, journal mode, synchronous mode, busy timeout, auto-migrate, archive directory.

**Command line**

- `db status`, `db migrate`, `db downgrade`, `db check`. `doctor` now reports the schema position, and diagnoses rather than repairs -- silently migrating would make a read-only command modify user data.

**Tests**

- 391 passing, 93% coverage. New shared contract suite for the unit of work, run against both the SQLAlchemy implementation and an in-memory fake.
- Covers pragma verification, migration up/down/up round trips, rollback, savepoint isolation, event release timing, concurrent reads and writes, cursor pagination walking every row exactly once, and startup refusing a newer database.

**Dependencies**

- Added `sqlalchemy` and `alembic`.

## Fixed

Three defects found by the Milestone 0.2 tests before any of this shipped:

- **A pool deadlock.** `QueuePool(pool_size=1)` held its one connection for the process lifetime, so any second checkout blocked forever rather than failing. Replaced with `StaticPool`, which expresses the actual design -- one connection, reused -- instead of a pool that happens to hold one.
- **SQLAlchemy autobegin blocked transactions.** Reading pragmas or running a health check on the long-lived connection silently opened a transaction, so the next explicit `begin()` was refused. Every read outside a unit of work now releases its implicit transaction.
- **Concurrent units of work collided.** One connection holds one transaction, so a second concurrent use case failed with an error unrelated to what the caller did. Transactions now serialize on a bounded lock: the second waits, and a genuine overlap surfaces as a named error rather than a hang.

## Changed

- `DATABASE.md`: added the connection and transaction model, pragma read-back, `schema_metadata`, constraint naming conventions, and renumbered business migrations to begin at `0002`.
- `API.md`: expanded the `UnitOfWork` contract with savepoints, event-release timing and transaction serialization; replaced the `MigrationRunner` sketch with the implemented interface; added the `Database` port.

## Architecture Decisions

- **ADR-034 -- Single Connection and Serialized Transactions** (Proposed). Records what ADR-013's threading decision implies for connections and transactions, and states explicitly that reads serialize behind writes. The remedy -- a reader pool exploiting WAL -- is deliberately deferred, with the measurements required before adopting it written down rather than left to judgement.

### Milestone 0.1 — Core Domain Ports

**Ports (domain layer)**

- `Clock` — UTC-only, timezone-aware wall time, plus a separate monotonic source for durations. Nothing else in the application calls `datetime.now()`.
- `IdGenerator` — time-ordered integer, UUID and correlation identifiers.
- `EventBus` — synchronous publish and subscribe with isolated handler failures.
- `SecretStore` — the only component permitted to hold credential material.
- `DomainEvent` base class. The concrete event catalogue still arrives with the milestones that raise those events.
- `SecretValue` value object: masked in `repr`, `str` and `format`, refuses to pickle, constant-time equality, explicit `reveal()`.

**Implementations (infrastructure)**

- `SystemClock`.
- `UuidV7IdGenerator` — RFC 9562 UUID version 7 with a monotonic counter, strictly increasing even under a frozen or backwards-moving clock, thread-safe, driven by the injected clock.
- `InProcessEventBus` — registration-order delivery, subclass delivery after exact-type handlers, per-handler failure isolation, automatic disabling after repeated failures, bounded publish depth.
- `KeyringSecretStore` (operating system credential store), `EnvironmentSecretStore` (read-only override) and `ChainedSecretStore`, composing the resolution order ADR-021 specifies. Blocking credential-store calls run in a worker thread.

**Composition root**

- All four ports constructed in `Container` and overridable through its constructor, so tests inject doubles rather than patching module globals. The identifier generator is driven by the container's clock, so fixing the clock fixes the identifiers.
- `Container.verify_secret_store()` fails closed when `security.require_secret_store` is set.
- `tgassist doctor` now reports credential-backend availability instead of listing it as unimplemented.

**Error taxonomy**

- Added `SecurityError` (with `SecretStoreUnavailableError`, `ReadOnlySecretStoreError`) and `EventDispatchError` — the branches that now have live consumers.

**Tests**

- New shared contract suite (`tests/contract/`) parametrized over every implementation of each port, production and fake alike. 288 tests, 93% statement coverage.
- Fakes: `FixedClock`, `AdvanceableClock`, `SequentialIdGenerator`, `RecordingEventBus`, `InMemorySecretStore`, `UnavailableSecretStore`. `RecordingEventBus` is written as an independent implementation rather than a subclass, so the contract suite genuinely tests it.
- Edge cases covered: naive-datetime rejection, clock correction versus monotonic time, counter exhaustion, backwards clock, thread safety, event cycles, failure-count reset, pickling refusal, chained-store precedence and deletion.

**Dependencies**

- Added `keyring` (mandated by ADR-021) and `pytest-asyncio` (development only).

## Fixed

- **The event bus swallowed its own refusal.** `EventDispatchError` raised by a nested `publish` was caught by the handler-isolation boundary, so an event cycle stopped silently instead of surfacing. The bus now re-raises its own control error while still isolating handler failures — a handler failing is contained, a cycle is a defect that must reach the publisher.
- **A log field named `event` collided with structlog's first positional parameter**, raising `TypeError` from inside the failure-isolation path — the one place an exception is least welcome. The field is now `event_type`.

## Changed

- `API.md` §5.1–§5.3 and §5.6: recorded where each implementation lives, added `new_uuid()` and the time-ordering guarantee, replaced fire-and-forget delivery with synchronous delivery, and changed `SecretStore.get()` to return `SecretValue`.
- `ARCHITECTURE.md` §8: event delivery is synchronous.
- `ERROR_HANDLING.md` §3: added `EventDispatchError` and refreshed the implementation-status note.

## Architecture Decisions

Three Proposed ADRs arising from implementation:

- **ADR-031 — Synchronous Event Delivery.** Supersedes the fire-and-forget semantics in `API.md` §5.3. Raised rather than changed silently, because it alters a documented contract.
- **ADR-032 — Secrets as a Domain Value Object.** A bare `str` cannot satisfy the documented rule that a secret must not appear in a `repr`.
- **ADR-033 — Identifier Generation Strategy.** UUID version 7, chosen for database index locality.

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
