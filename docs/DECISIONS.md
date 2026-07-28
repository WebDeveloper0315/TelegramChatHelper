# DECISIONS.md

# Telegram AI Conversation Assistant

Architecture Decision Records (ADR)

Version: 2.0

Status: Active

Last Updated: 2026-07-28

---

# Purpose

This document records important technical and architectural decisions made throughout the project.

Every major decision should answer:

- What decision was made?
- Why was it made?
- What alternatives were considered?
- What are the trade-offs?
- What are the long-term consequences?

This document serves as the project's engineering memory.

---

# Decision Status

Each decision must have one of the following statuses:

- Proposed
- Accepted
- Deprecated
- Superseded
- Rejected

**ADR-001 through ADR-010 are Accepted.**
**ADR-011 through ADR-030 are Proposed and require explicit approval before implementation begins.**

---

# Index

| ADR | Title | Status |
|-----|-------|--------|
| 001 | Desktop Application Instead of Telegram Bot | Accepted |
| 002 | Python as Primary Language | Accepted |
| 003 | Clean Architecture | Accepted |
| 004 | Repository Pattern | Accepted |
| 005 | Multiple AI Providers | Accepted |
| 006 | Separate AI Services | Accepted |
| 007 | SQLite for MVP | Accepted |
| 008 | Prompt Files Outside Source Code | Accepted |
| 009 | Plugin-Oriented Design | Accepted |
| 010 | User-Controlled AI Assistance | Accepted |
| 011 | Clean Architecture Dependency Direction Correction | Proposed |
| 012 | Telegram Client Library and Gateway Adapter Strategy | Proposed |
| 013 | Concurrency Model | Proposed |
| 014 | Desktop UI Framework | Proposed |
| 015 | Database Access Layer and Migration Tooling | Proposed |
| 016 | SQLite to PostgreSQL Migration Strategy | Proposed |
| 017 | Vector Search Implementation | Proposed |
| 018 | Embedding Model Strategy | Proposed |
| 019 | AI Memory Approval Workflow | Proposed |
| 020 | Structured Output and Provider Capability Negotiation | Proposed |
| 021 | Secret Management Strategy | Proposed |
| 022 | Local Data Encryption Strategy | Proposed |
| 023 | Automation Boundary | Proposed |
| 024 | Local-First Design and AI Data Boundaries | Proposed |
| 025 | Plugin Architecture and Trust Model | Proposed |
| 026 | Prompt Externalization, Registry and Versioning | Proposed |
| 027 | Logging Destination | Proposed |
| 028 | Configuration and Settings Ownership | Proposed |
| 029 | Composite AI Execution Behind Separate Ports | Proposed |
| 030 | Developer CLI as a First-Class Adapter | Proposed |

---

# ADR-001

## Title

Desktop Application Instead of Telegram Bot

Status

Accepted

Date

2026-07-28

---

### Context

The project needs to interact with Telegram conversations while maintaining long-term conversation context and providing AI-assisted reply suggestions.

---

### Decision

Develop the application as a desktop client using Telegram's client API (TDLib) instead of the Telegram Bot API.

---

### Alternatives Considered

1. Telegram Bot API
2. TDLib Desktop Client
3. MTProto implementation

---

### Reasoning

The Bot API has significant limitations for personal conversations.

TDLib provides better support for client-side features and future extensibility.

---

### Consequences

Pros

- Greater flexibility
- Better access to conversation history
- Supports future expansion

Cons

- More complex implementation
- Requires local authentication

---

### Related Decisions

Refined by ADR-012 (adapter strategy), ADR-023 (automation boundary).

---

# ADR-002

## Title

Python as Primary Language

Status

Accepted

Date

2026-07-28

---

### Decision

Use Python for the application. Minimum supported version is Python 3.12.

---

### Alternatives

Rust, Go, C#, C++, Node.js

---

### Reasoning

Python offers:

- Excellent AI ecosystem
- Mature Telegram libraries
- Rapid development
- Strong community support

---

### Consequences

Pros

- Fast development
- Excellent AI integration
- Large ecosystem

Cons

- Lower runtime performance than compiled languages
- Desktop packaging requires additional tooling (see ADR-014)

---

# ADR-003

## Title

Clean Architecture

Status

Accepted

---

### Decision

Use Clean Architecture.

---

### Alternatives

Layered Architecture, MVC, Monolithic Structure, Microservices

---

### Reasoning

Clean Architecture keeps business logic independent of infrastructure.

---

### Consequences

Pros

- Easy testing
- Replaceable infrastructure
- Maintainable

Cons

- More initial complexity

---

### Related Decisions

The dependency direction originally documented alongside this decision was incorrect and is corrected by ADR-011.

---

# ADR-004

## Title

Repository Pattern

Status

Accepted

---

### Decision

All database access must go through repositories.

---

### Alternatives

Direct SQL, ORM-only approach, Active Record

---

### Reasoning

Repositories isolate storage implementation from business logic.

---

### Consequences

Pros

- Testability
- Database independence
- Cleaner code

Cons

- Additional abstraction

---

### Related Decisions

Extended by ADR-015 (access layer) and ADR-016 (PostgreSQL path).

---

# ADR-005

## Title

Multiple AI Providers

Status

Accepted

---

### Decision

Support multiple AI providers through interfaces.

---

### Alternatives

Single provider, provider-specific implementation

---

### Reasoning

Avoid vendor lock-in. Support local and cloud models.

---

### Consequences

Pros

- Flexibility
- Future-proofing
- Resilience

Cons

- More abstraction
- Provider capability differences must be modelled explicitly (see ADR-020)

---

# ADR-006

## Title

Separate AI Services

Status

Accepted

---

### Decision

Use specialized AI services instead of one monolithic AI component.

Examples: Conversation Analyzer, Memory Extractor, Planner, Reply Generator, Emotion Analyzer.

---

### Reasoning

Single-responsibility components are easier to test and replace.

---

### Consequences

Pros

- Maintainability
- Scalability
- Independent improvements

Cons

- More orchestration required
- Naive implementation causes one LLM call per service per message (addressed by ADR-029)

---

# ADR-007

## Title

SQLite for MVP

Status

Accepted

---

### Decision

Use SQLite during early development.

---

### Alternatives

PostgreSQL, MySQL, MongoDB

---

### Reasoning

Simple deployment, no server dependency, reliable, fast enough for MVP.

---

### Future Plan

Support PostgreSQL through repository abstraction. The concrete strategy is defined in ADR-016.

---

# ADR-008

## Title

Prompt Files Outside Source Code

Status

Accepted

---

### Decision

Store prompts as Markdown files.

---

### Alternatives

Hardcoded strings, database, JSON

---

### Reasoning

Prompts evolve frequently. Keeping them outside the source code improves maintainability.

---

### Consequences

Pros

- Easy editing
- Version control
- Better collaboration

Cons

- Additional file management
- Requires a registry and schema binding to remain safe (see ADR-026)

---

# ADR-009

## Title

Plugin-Oriented Design

Status

Accepted

---

### Decision

Future features should be implemented as plugins whenever practical.

---

### Reasoning

Keeps the core application small. Supports future extensions.

---

### Consequences

Pros

- Scalable
- Customizable
- Maintainable

Cons

- Requires stable plugin APIs
- Requires an explicit trust model (see ADR-025)

---

# ADR-010

## Title

User-Controlled AI Assistance

Status

Accepted

---

### Decision

The assistant recommends actions rather than automatically taking over conversations by default.

---

### Reasoning

Keeping users in control improves transparency and makes it easier for them to review or edit AI-generated suggestions before sending.

---

### Consequences

Pros

- Users retain final decision-making
- Easier to build trust in the assistant
- Simpler to understand and debug AI behavior

Cons

- Requires an extra user step before sending messages

---

### Related Decisions

Hardened into a product constraint by ADR-023. Extended to memory writes by ADR-019.

---

# ADR-011

## Title

Clean Architecture Dependency Direction Correction

Status

Proposed

Date

2026-07-28

---

### Context

`ARCHITECTURE.md` v1.0 §9 documented the dependency rule as:

```
Allowed:   UI → Application → Domain → Infrastructure
Forbidden: Infrastructure → Domain
```

This is inverted relative to Clean Architecture and contradicts both ADR-003 and `ARCHITECTURE.md` §2, which states that business logic must never depend on infrastructure. Implemented literally, it would place database, Telegram and AI provider imports inside the Domain Layer, destroying testability and the platform independence the project depends on.

---

### Decision

Adopt the corrected dependency rule:

```
Allowed
  presentation  → application → domain
  infrastructure → domain                  (implements domain-defined ports)
  composition root → all layers            (object construction only)

Forbidden
  domain        → application | infrastructure | presentation | third-party libraries
  application   → infrastructure concrete classes
  presentation  → infrastructure
  infrastructure → application | presentation
```

All interfaces (ports) live in `domain/ports/`. Infrastructure supplies adapters. The application layer depends only on ports. Only `application/container.py` (the composition root) is permitted to import concrete infrastructure classes, and only to construct them.

The rule is enforced mechanically in CI using `import-linter` contracts, not by convention.

---

### Alternatives Considered

1. Keep the documented (inverted) rule — rejected; it is not Clean Architecture and forfeits every benefit claimed in ADR-003.
2. Use a separate top-level `ports/` package outside `domain/` — rejected; ports are part of the domain's contract with the outside world and belong with it.
3. Rely on code review rather than tooling — rejected; a single accidental import is invisible in review and permanently couples the layers.

---

### Reasoning

Dependency inversion is the mechanism that makes the domain testable without Telegram, a database, or an AI provider. It is the load-bearing property of the entire architecture. Automated enforcement is inexpensive and turns an aspiration into a verifiable invariant.

---

### Consequences

Pros

- Domain becomes genuinely dependency-free and unit-testable
- New messaging platforms require only a new adapter
- Violations fail the build instead of accumulating silently

Cons

- Requires explicit mapping between persistence rows and domain objects
- Slightly more code than letting infrastructure types leak inward

---

### Related Decisions

ADR-003, ADR-004, ADR-015.

---

# ADR-012

## Title

Telegram Client Library and Gateway Adapter Strategy

Status

Proposed

Date

2026-07-28

---

### Context

ADR-001 selected TDLib. TDLib requires a compiled native library (`tdjson`) for each target platform, which affects onboarding, CI, and desktop packaging. A pure-Python MTProto client (Telethon) removes that constraint but reimplements the protocol and local state management outside Telegram's control.

The `TelegramGateway` port isolates this choice from the rest of the application, so the decision is about sequencing and risk, not architecture.

---

### Decision

1. Define `TelegramGateway` as a domain port with no library-specific types in its signature.
2. Implement **TDLib as the primary adapter**, honouring ADR-001.
3. Resolve native binary acquisition, verification and bundling during **Milestone 0**, not at packaging time.
4. Keep a **Telethon adapter as an explicitly supported fallback** if TDLib binary distribution proves impractical on a target platform, and implement it opportunistically as the second adapter to validate the port.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| TDLib primary (chosen) | Official library; robust local cache, update ordering and reconnection; highest behavioural fidelity | Native binary per platform; more complex packaging; thin third-party Python wrappers |
| Telethon primary | `pip install`, no native dependency, trivial packaging, excellent async history API | Third-party protocol reimplementation; local state and update-gap handling are the application's problem |
| Telethon first, TDLib before 1.0 | Fastest path to a working ingestion pipeline; proves the port with two adapters | Contradicts ADR-001; rework later |

---

### Reasoning

ADR-001 is an accepted decision and TDLib is the higher-fidelity long-term choice. The material risk is packaging, and that risk is manageable *if it is confronted in Milestone 0 rather than Milestone 14*. Requiring binary resolution early converts a late-project blocker into an early, bounded task.

Building the second adapter remains valuable because two implementations are the only real proof that a port is a genuine abstraction.

---

### Consequences

Pros

- Consistent with ADR-001
- Packaging risk is retired early
- Gateway remains swappable

Cons

- Milestone 0 carries native-toolchain work
- Contributors need the binary present to run integration tests

---

### Risks

If prebuilt binaries are used, they must be checksum-verified and their provenance documented; an unverified `tdjson` binary has full access to the user's Telegram session.

---

### Related Decisions

ADR-001, ADR-013, ADR-021.

---

# ADR-013

## Title

Concurrency Model

Status

Proposed

Date

2026-07-28

---

### Context

No project document specified whether the application is synchronous, threaded, or asyncio-based. This determines the signature of every port in `API.md`, how the Telegram client is driven, and how the desktop UI event loop is integrated. It cannot be changed later without rewriting the whole codebase.

---

### Decision

1. The application is **asyncio-first**. All ports are declared with `async def` where they perform I/O.
2. The Telegram gateway, AI providers, embedding providers and background tasks run natively on the asyncio event loop.
3. **SQLite access is synchronous**, executed on a **single dedicated worker thread** owned by the persistence layer. Async repository methods delegate to that thread. This preserves SQLite's single-writer model, avoids lock contention, and keeps repository implementations simple and directly testable.
4. The desktop UI runs the Qt event loop bridged to asyncio via `qasync`. No I/O of any kind runs on the UI thread.
5. Pure domain services remain synchronous and side-effect free.

---

### Alternatives Considered

1. Fully synchronous with threads — simpler mental model, but a poor fit for TDLib's update stream and for a responsive UI; concurrency bugs move into manual locking.
2. `aiosqlite` — convenient, but it is a thread wrapper with less control over writer serialization, and it obscures where blocking occurs.
3. Multi-connection concurrent writes — SQLite serializes writes anyway; produces `SQLITE_BUSY` errors under load for no gain.

---

### Reasoning

Asyncio matches the event-driven nature of the domain (updates arrive, responses stream in). Isolating SQLite on one thread gives a single, well-understood serialization point and makes the "one writer" invariant structural rather than accidental.

---

### Consequences

Pros

- Uniform async port signatures
- No SQLite lock contention by construction
- Responsive UI

Cons

- Contributors must understand the executor boundary
- Blocking calls accidentally placed on the loop degrade the whole app; enforced by lint rules and review

---

### Related Decisions

ADR-014, ADR-015.

---

# ADR-014

## Title

Desktop UI Framework

Status

Proposed

Date

2026-07-28

---

### Context

The application requires a desktop interface capable of rendering conversation histories that may contain hundreds of thousands of messages, alongside memory and goal editors.

---

### Decision

Use **PySide6** (the official Qt for Python binding, LGPL), with `qasync` bridging the Qt event loop to asyncio and `pytest-qt` for UI testing. Message lists use `QAbstractListModel` with virtualized views.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| PySide6 (chosen) | LGPL permits closed distribution; mature; model/view scales to very large lists; native look | Large dependency; Qt concepts to learn |
| PyQt6 | Equivalent capability | GPL or commercial licence — constrains distribution |
| Flet / Textual | Fast to build | Weak for dense virtualized data views |
| Local web UI (FastAPI + browser) | Aligns with the future web dashboard goal; best styling | Loses native feel; adds a local HTTP attack surface; harder packaging |
| Tkinter | Zero dependencies | Inadequate for this UI |

---

### Reasoning

Licensing is decisive: PySide6's LGPL keeps distribution options open, while PyQt6 would force a licensing commitment before the project has one. Virtualized model/view rendering is a hard requirement given the data volumes and only the Qt options provide it convincingly.

---

### Consequences

Pros

- Distribution flexibility preserved
- Scales to large conversation histories
- Testable via `pytest-qt`

Cons

- Adds a large dependency and increases installer size
- LGPL compliance requires dynamic linking and attribution in distributed builds

---

### Related Decisions

ADR-013, ADR-030.

---

# ADR-015

## Title

Database Access Layer and Migration Tooling

Status

Proposed

Date

2026-07-28

---

### Context

`DATABASE.md` requires repositories, versioned reversible migrations, parameterized queries and a future PostgreSQL path, but did not specify how SQL is executed or how migrations are managed.

---

### Decision

1. Use **SQLAlchemy Core** (expression language), **not** the ORM.
2. Repositories are hand-written and return **domain objects**, never rows or ORM entities.
3. Explicit mapper functions translate between rows and domain objects in `infrastructure/persistence/mappers.py`.
4. Use **Alembic** for migrations, with SQLite batch mode for `ALTER TABLE` limitations.
5. Every connection is configured with `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`, `PRAGMA synchronous=NORMAL`.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| SQLAlchemy Core (chosen) | Safe parameter binding, dialect portability, mature migrations via Alembic, no identity map leaking into the domain | Some boilerplate in mappers |
| SQLAlchemy ORM | Fastest to write | ORM entities become de facto domain models; session lifetime leaks into use cases; undermines ADR-003/004 |
| Raw `sqlite3` + numbered SQL files | Zero dependency, total control | Forfeits the PostgreSQL path in ADR-007; portability written by hand |
| Peewee / Tortoise | Lighter than SQLAlchemy | Smaller ecosystems, weaker migration story |

---

### Reasoning

The ORM's convenience is precisely the mechanism by which persistence concerns invade the domain. Core provides the genuinely valuable parts (binding, dialects, migrations) without that failure mode, and explicit mappers are boring, readable and trivially unit-tested.

---

### Consequences

Pros

- Domain stays persistence-ignorant
- PostgreSQL migration path preserved
- Mapping logic is explicit and testable

Cons

- More code than an ORM
- Developers must write SQL expressions rather than relying on lazy loading

---

### Related Decisions

ADR-004, ADR-007, ADR-013, ADR-016.

---

# ADR-016

## Title

SQLite to PostgreSQL Migration Strategy

Status

Proposed

Date

2026-07-28

---

### Context

ADR-007 chose SQLite for the MVP and promised a PostgreSQL path "through repository abstraction" without defining what makes that path viable.

---

### Decision

PostgreSQL support is a **post-1.0 capability**, kept viable by five rules observed from the first commit:

1. **No SQLite-only SQL in repositories.** Portable constructs only; any exception is isolated in a dialect-specific module.
2. **Portable types.** Timestamps stored as UTC ISO-8601 text in SQLite map to `TIMESTAMPTZ`; booleans as integers map to `BOOLEAN`; identifiers are 64-bit integers.
3. **Alembic migrations are written dialect-aware**, with SQLite batch operations isolated behind helpers.
4. **Full-text search is behind a port** (`MessageSearchPort`), implemented with FTS5 on SQLite and `tsvector` on PostgreSQL.
5. **Vector search is behind `VectorStore`** (ADR-017), implemented with NumPy/sqlite-vec on SQLite and `pgvector` on PostgreSQL.

A migration utility (`tgassist db migrate-engine`) will export from SQLite and import to PostgreSQL in dependency order inside one transaction, verifying row counts and referential integrity before committing.

---

### Alternatives Considered

1. Design for PostgreSQL from day one — rejected; imposes a server dependency on a local-first desktop application for a benefit no MVP user needs.
2. Ignore portability and rewrite later — rejected; the two hardest parts (search and vectors) are exactly the parts that become impossible to retrofit if their implementations are scattered.

---

### Reasoning

The realistic driver for PostgreSQL is multi-device sync or a web dashboard, both of which are post-1.0. Constraining today's implementation to portable constructs costs almost nothing; discovering FTS5 syntax spread across twenty repository methods later costs a rewrite.

---

### Consequences

Pros

- Future path preserved at minimal present cost
- Search and vector strategies isolated from the start

Cons

- Cannot use the most convenient SQLite-specific features
- Dialect-aware migrations require slightly more care

---

### Related Decisions

ADR-007, ADR-015, ADR-017.

---

# ADR-017

## Title

Vector Search Implementation

Status

Proposed

Date

2026-07-28

---

### Context

Semantic memory retrieval requires vector similarity search. `DATABASE.md` left storage undecided ("vectors may be stored externally"). Realistic scale is hundreds of memories per contact and low tens of thousands overall.

---

### Decision

1. Define a `VectorStore` port (`upsert`, `delete`, `search`, `rebuild`, `stats`).
2. **MVP implementation: exact brute-force cosine similarity using NumPy**, over vectors stored as `BLOB` in the `embeddings` table, with an in-process cache of the active matrix.
3. **Upgrade path: a `sqlite-vec` adapter** implementing the same port, adopted when measured retrieval latency exceeds the budget in `PERFORMANCE_BUDGETS`.
4. **PostgreSQL path: `pgvector`**, same port.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| NumPy brute force (chosen for MVP) | Exact results, zero extra dependencies, sub-millisecond at target scale, trivially correct | Linear scaling; unsuitable beyond ~10⁵ vectors |
| sqlite-vec | Keeps one storage engine, ANN indexing | Younger project; adds an extension dependency |
| Chroma | Batteries included | Own persistence layer, duplicate storage, heavier deps |
| FAISS | Very fast | Awkward persistence semantics, heavy wheels |
| Qdrant / LanceDB | Production-grade | Server or large embedded footprint; disproportionate to a desktop app |

---

### Reasoning

Adopting a vector database at this scale is over-engineering. An exact NumPy search is *more* correct than an approximate index, needs no new dependency, and is fast enough by a wide margin. The port makes the upgrade a one-file change once measurement justifies it.

---

### Consequences

Pros

- No premature dependency
- Exact recall
- Clean upgrade path

Cons

- Memory resident matrix grows with corpus size (bounded by retention policy)
- Requires the cache to be invalidated on write

---

### Related Decisions

ADR-016, ADR-018.

---

# ADR-018

## Title

Embedding Model Strategy

Status

Proposed

Date

2026-07-28

---

### Context

Semantic retrieval requires an embedding model. Local models preserve privacy but add substantial install weight; cloud embeddings are small and cheap but transmit memory text to a third party — in direct tension with ADR-024.

---

### Decision

1. Default to **local embeddings via `fastembed`** (ONNX Runtime, no PyTorch), with a multilingual model appropriate to the user's languages.
2. Offer **cloud embedding providers as an explicit opt-in**, subject to the same data-boundary consent as any other cloud AI use.
3. **Never bundle model weights in the installer.** Models are downloaded on first use, with size and provider disclosed before download begins.
4. Record `embedding_provider`, `embedding_model`, `embedding_dimension` and `embedding_version` with every stored vector.
5. Changing the model triggers a **re-index job**, not silent mixing. Vectors from different models are never compared.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| `fastembed` (chosen) | ~100 MB runtime, no torch, good quality, fully local | Smaller model selection than the torch ecosystem |
| `sentence-transformers` | Widest model choice, best quality ceiling | Pulls PyTorch (multi-GB download) onto every user's machine |
| Cloud embedding APIs | Tiny local footprint, strong quality | Transmits memory content off-device; conflicts with local-first default |
| Hash/TF-IDF baseline | No model at all | Insufficient for semantic retrieval across languages |

---

### Reasoning

A multi-gigabyte PyTorch install is a serious adoption barrier for a desktop application, and `CLAUDE.md` explicitly requires large dependencies to be surfaced rather than assumed. `fastembed` delivers most of the quality at a fraction of the weight while keeping the privacy default intact.

---

### Consequences

Pros

- Private by default
- Modest install size
- Model changes are safe because they are versioned

Cons

- First use requires a network download
- Re-index required on model change

---

### Related Decisions

ADR-017, ADR-024.

---

# ADR-019

## Title

AI Memory Approval Workflow

Status

Proposed

Date

2026-07-28

---

### Context

Long-term memory is the product's core value and its largest correctness risk. A hallucinated or injected "fact" written automatically is retrieved indefinitely and silently degrades every future suggestion. Errors compound and are hard to detect after the fact.

---

### Decision

AI-extracted memories are **proposals**, never direct writes.

1. Extraction produces `MemoryProposal` records with status `pending`, carrying: extracted value, category, confidence, source message reference, extracted-at timestamp, and the model/prompt version that produced them.
2. Proposals become `Memory` records only via an explicit transition: **user approval**, or **auto-approval** where all of the following hold — the category is in the user's configured auto-approve list, model confidence is at or above the configured threshold, and the proposal does not contradict an existing memory.
3. Contradictions never overwrite. A proposal conflicting with an existing memory is always surfaced for user resolution, and resolution creates a `MemoryRevision` recording supersession.
4. Rejected proposals are retained (status `rejected`) so the same fact is not re-proposed repeatedly.
5. Every `Memory` retains provenance and is individually viewable, editable and deletable by the user.
6. **User-entered memories always outrank AI-derived memories** in retrieval and in conflict resolution.

---

### Alternatives Considered

1. Write memories automatically — rejected; unbounded silent corruption of the system's most valuable state.
2. Require approval for every memory with no auto-approval — rejected; approval fatigue makes the feature unusable and users will disable it wholesale.
3. Confidence threshold only, no proposal record — rejected; loses provenance and the ability to review or undo.

---

### Reasoning

This is ADR-010 ("the user remains in control") applied to persistent state rather than only to outgoing messages. The proposal queue also produces a labelled dataset of accepted and rejected extractions, which is the natural evaluation signal for improving the extraction prompt.

---

### Consequences

Pros

- Memory corruption becomes visible and reversible
- Provenance for every stored fact
- Generates evaluation data as a by-product

Cons

- Requires a review interface (CLI in M5, UI in M10)
- Two tables instead of one

---

### Related Decisions

ADR-010, ADR-024, and the prompt-injection defences in `SECURITY.md` §12.

---

# ADR-020

## Title

Structured Output and Provider Capability Negotiation

Status

Proposed

Date

2026-07-28

---

### Context

`PROMPTS.md` requires every AI response to be structured JSON. Support for constrained output differs sharply between providers: some offer native JSON-schema modes, some only tool-calling, and some local models offer neither reliably. The original `LLMProvider` interface had no way to express this.

---

### Decision

1. `LLMProvider` exposes a `capabilities()` method returning a set of `Capability` values: `JSON_SCHEMA`, `TOOL_CALLING`, `STREAMING`, `SYSTEM_PROMPT`, `TOKEN_COUNTING`, `VISION`.
2. A `StructuredOutputStrategy` selects the strongest available mechanism: native schema → tool-calling coercion → prompt-instructed JSON with extraction.
3. **All output is validated against a JSON Schema regardless of mechanism.** Provider claims are never trusted.
4. On validation failure: exactly **one repair attempt**, sending the validation errors back to the model. A second failure raises a typed `SchemaViolationError`.
5. `count_tokens()` returns `Optional[int]`. Business logic must never require an exact count; context budgeting uses conservative estimation when counting is unavailable.
6. A provider conformance test suite runs the same schemas against every adapter.

---

### Alternatives Considered

1. Require native JSON-schema support — rejected; eliminates local models and contradicts ADR-005.
2. Free-text parsing with regex extraction — rejected; unreliable and untestable.
3. Unlimited repair retries — rejected; unbounded cost and latency on a systematically failing prompt.

---

### Reasoning

Uniform validation at the boundary is what makes provider substitution safe. Capability negotiation makes provider differences explicit data rather than hidden failure modes discovered in production.

---

### Consequences

Pros

- Any provider can be used, with graceful degradation
- Malformed output never reaches business logic
- Repair cost is bounded

Cons

- Two round trips in the failure case
- Schemas must be maintained alongside prompts

---

### Related Decisions

ADR-005, ADR-026, ADR-029.

---

# ADR-021

## Title

Secret Management Strategy

Status

Proposed

Date

2026-07-28

---

### Context

`SECURITY.md` v1.0 prescribed environment variables for API keys. That is appropriate for servers and poor for a desktop application, where users have no natural place to set them persistently and would end up pasting keys into files. Secrets in scope: AI provider API keys, the Telegram `api_id`/`api_hash`, the TDLib database encryption key, and any backup encryption key.

---

### Decision

1. Define a `SecretStore` port: `get`, `set`, `delete`, `list_names`.
2. **Primary implementation: the OS credential store** via the `keyring` package — DPAPI-backed Windows Credential Manager, macOS Keychain, Secret Service on Linux.
3. **Override chain (highest priority first): process environment variable → OS keyring → not configured.** Environment variables remain supported for CI, automation and advanced users.
4. If no OS backend is available (some headless Linux environments), fall back to an **encrypted file store** whose key is derived from a user passphrase via a memory-hard KDF. Never a plaintext file.
5. Secret **names** may appear in configuration and logs; secret **values** never do. A logging redaction processor enforces this centrally.
6. The application never writes a secret value to the database, to a log, to a crash report, or to a backup that is not encrypted.

---

### Alternatives Considered

1. Environment variables only — poor desktop UX; encourages plaintext `.env` files that get committed or backed up.
2. Encrypted config file only — portable, but requires a passphrase prompt at every start and reimplements what the OS already does well.
3. Plaintext config file — rejected outright.

---

### Reasoning

The OS credential store is the only option that is both secure at rest and invisible in normal use. The environment override preserves scriptability without weakening the default.

---

### Consequences

Pros

- Secrets protected by OS-level facilities
- No plaintext secrets on disk by default
- Works for both interactive and automated use

Cons

- `keyring` backend behaviour varies across Linux desktops
- Users must re-enter secrets if the OS profile changes

---

### Related Decisions

ADR-012, ADR-022, ADR-024.

---

# ADR-022

## Title

Local Data Encryption Strategy

Status

Proposed

Date

2026-07-28

---

### Context

`SECURITY.md` v1.0 listed "encrypted local database" as a future enhancement while `DATABASE.md` v1.0 said sensitive data "should be encrypted where appropriate" — an unresolved contradiction. Full-database encryption (SQLCipher or SQLite3MultipleCiphers) is a build-time dependency that is expensive to retrofit but also imposes real cost: custom SQLite builds, packaging complexity, key management at every start, and loss of standard tooling.

---

### Decision

A **phased strategy**, decided now and implemented in stages:

**Phase 1 — MVP (Milestones 0–10)**

- **Secrets** are always encrypted, via the OS credential store (ADR-021).
- **Telegram session data is always encrypted** using TDLib's `database_encryption_key`, with that key held in the OS credential store. This is non-negotiable: the session is the highest-value asset in the system.
- The **application database is not encrypted at rest**, but is protected by restrictive filesystem ACLs (owner-only) applied on creation and verified at startup by `tgassist doctor`.
- **Backups support optional encryption** (age/AES-GCM with a user passphrase), and encryption is the default for any backup written outside the application data directory.
- All database access goes through repositories and a single `DatabaseEngine` factory, so the encryption decision has exactly one implementation site.

**Phase 2 — v1.0**

- Optional full-database encryption via SQLite3MultipleCiphers, enabled per profile, with the key in the OS credential store and a documented, tested migration in both directions.

---

### Alternatives Considered

1. Encrypt everything from day one — significant packaging and debugging cost for a threat model (local attacker with filesystem access) that already implies broad compromise; delays the MVP.
2. Never encrypt the database — leaves a defensible gap for users on shared or portable machines.
3. Encrypt selected columns — worst of both: complexity of encryption, none of the completeness, and it breaks search and indexing.

---

### Reasoning

The phasing follows the value of each asset. Session credentials permit full account takeover and are encrypted unconditionally. Conversation content is sensitive but its exposure requires local filesystem access, so OS-level protection is proportionate for the MVP — provided the architecture keeps the stronger option one implementation away, which the single engine factory guarantees.

---

### Consequences

Pros

- Highest-value asset protected immediately
- MVP is not delayed by custom SQLite builds
- Upgrade is a contained change

Cons

- Conversation data is readable by anyone with filesystem access in Phase 1; this must be stated plainly in `PRIVACY.md` and in onboarding
- Phase 2 requires a tested bidirectional migration

---

### Related Decisions

ADR-021, ADR-024.

---

# ADR-023

## Title

Automation Boundary

Status

Proposed

Date

2026-07-28

---

### Context

The Human Behavior Engine models reply timing, typing duration and message splitting. Implemented as *actions* rather than *recommendations*, these features would make automated messages indistinguishable from the user's own — deceiving the contact, and matching the behavioural signature that leads Telegram to restrict accounts. `PROJECT_SPEC.md` v1.0 also contained the conditional phrase "unless the user enables automation," implying an automation mode with no defined boundary.

---

### Decision

The following are **product constraints, not configurable settings**:

1. **The application never sends a message that the user has not explicitly approved.** There is no unattended auto-reply mode.
2. **The application never emits synthetic typing indicators** or other signals designed to imply human activity that is not occurring.
3. **The application never impersonates the user to a contact** beyond composing text the user reviews and sends.
4. The Human Behavior Engine emits `BehaviorRecommendation` objects consumed by the presentation layer. It has **no dependency on `TelegramGateway`** — the constraint is structural, not merely policy.
5. `ReplyGenerator` likewise has no send capability. Only the `SendMessage` use case can send, and it requires an approved `ReplySuggestion` or user-authored text.
6. Scheduled send (user writes now, approves, delivery occurs later at a recommended time) **is** permitted: the content and the decision are the user's; only delivery timing is deferred. It is disabled by default and shows pending sends prominently.

`PROJECT_SPEC.md` is amended to remove the "unless the user enables automation" clause.

---

### Alternatives Considered

1. Auto-reply behind an off-by-default setting — rejected; the harms (deceived contacts, account restriction, AI errors sent unreviewed) fall largely on people who never consented, and a warning dialog does not transfer that risk.
2. Auto-reply for an allowlist of contacts — rejected for the MVP; recognisable as the same risk with extra steps.
3. Fully manual with no timing features — rejected; timing *advice* is genuinely useful and carries none of the risk.

---

### Reasoning

This is where the project's stated principle ("the user remains in control") becomes falsifiable. Enforcing it through the dependency graph rather than through configuration means it cannot be eroded by a future feature request without a visible architectural change — which is exactly the review trigger it should be.

---

### Consequences

Pros

- Contacts are never deceived about who they are talking to
- Materially reduces account-restriction risk
- AI errors cannot reach a contact unreviewed

Cons

- Rules out a feature some users will request
- Requires the UI to make approval fast enough to feel unobtrusive

---

### Related Decisions

ADR-010, ADR-019, ADR-024.

---

# ADR-024

## Title

Local-First Design and AI Data Boundaries

Status

Proposed

Date

2026-07-28

---

### Context

Every conversation contains another person's personal data. The application stores it, profiles it (emotion, trust, relationship stage) and may transmit it to a third-party AI provider — all without the contact's knowledge. No v1.0 document acknowledged this.

---

### Decision

1. **Local-first is the default.** All data is stored on the user's device. No component transmits conversation content anywhere unless the user has enabled a cloud provider.
2. **Cloud AI is opt-in and granular.** The user enables cloud providers explicitly, and may exclude specific chats or contacts from cloud processing entirely (`chats.ai_processing_mode`: `local_only`, `cloud_allowed`, `disabled`).
3. **Minimum necessary context.** Requests carry only the retrieved memories, summary and recent messages required by the task — never entire histories. Context assembly is budgeted and logged (metadata only).
4. **Transparency.** Before any first transmission to a given provider the user sees which provider will receive data and what categories are included. The active provider is visible in the UI whenever a suggestion is generated.
5. **No telemetry.** The application performs no analytics, crash reporting or usage tracking that leaves the device without separate, explicit, off-by-default consent.
6. **Data minimisation at ingest.** Synchronisation scope is bounded (selected chats, bounded history depth) rather than mirroring the entire account by default.
7. **Contact rights.** The user can export or delete all data relating to a single contact in one action, so they can honour a request from that person.

---

### Alternatives Considered

1. Cloud-first for quality — better default output, but incompatible with the privacy commitments in `PROJECT_SPEC.md` and unreasonable for third parties who never consented.
2. Cloud allowed globally with a single toggle — simpler, but no user has a uniform sensitivity across all their conversations.
3. Local models only — strongest privacy, but excludes users without capable hardware and contradicts ADR-005.

---

### Reasoning

The user is the data controller for their contacts' data. The architecture should make the privacy-respecting choice the easy one and the cheap one, and make any wider transmission a conscious act. Per-chat granularity matters because sensitivity varies per relationship, not per user.

---

### Consequences

Pros

- Defensible privacy position for third-party data
- Users can match provider choice to conversation sensitivity
- Reduced cost and latency by default

Cons

- Local-only mode yields lower suggestion quality
- Per-chat provider routing adds configuration surface

---

### Related Decisions

ADR-005, ADR-018, ADR-021, ADR-022, ADR-023.

---

# ADR-025

## Title

Plugin Architecture and Trust Model

Status

Proposed

Date

2026-07-28

---

### Context

ADR-009 committed to plugin-oriented design. Designing the API before any extension exists reliably produces the wrong API. Separately, in-process Python plugins cannot be sandboxed: a plugin has full access to the process, the database file and the session.

---

### Decision

1. Use **`pluggy`** for hook specifications and **`importlib.metadata` entry points** for discovery, plus a local `plugins/` directory for development.
2. **Derive the API rather than design it.** Before the framework is generalised (M12), two capabilities are built as if they were plugins — an additional AI provider and a UI panel — and the hook specification is extracted from what they actually required.
3. **Plugins are trusted code.** This is stated plainly to users. There is no security boundary between a plugin and the application; installing a plugin is equivalent to installing an application.
4. A **declarative permission manifest** (`requires: [memory:read, telegram:send, network]`) is required from every plugin. In v1.0 it is *advisory*: shown to the user at install time and logged, but not enforced by a sandbox. It is not presented as a security control.
5. **Failure isolation is mandatory.** All hook invocations are wrapped; an exception disables the offending plugin for the session, is logged with the plugin name, and never propagates into core execution.
6. The plugin API carries a **semantic version**; plugins declare a compatible range and incompatible plugins are refused with a clear message.
7. Plugins access data **only through the published API**, never by opening the database file.

---

### Alternatives Considered

1. Hand-rolled hook system — fine initially, but converges on pluggy's design with fewer edge cases handled.
2. Subprocess or WASM isolation with IPC — a genuine security boundary, but a large increase in complexity and latency, and disproportionate for a single-user desktop application where the user already chose to install the plugin.
3. No plugin system — simplest, but abandons ADR-009 and the extensibility goal.

---

### Reasoning

Honesty about the trust model is more protective than an enforcement mechanism that does not actually enforce. Deriving the API from real consumers is the difference between a plugin system that survives contact with its second plugin and one that needs a breaking change immediately.

---

### Consequences

Pros

- Battle-tested hook semantics with exception isolation
- Pip-installable plugins
- API grounded in real usage

Cons

- Malicious plugins are not contained; user judgement is the control
- Deferring to M12 delays third-party contribution

---

### Related Decisions

ADR-009, ADR-024.

---

# ADR-026

## Title

Prompt Externalization, Registry and Versioning

Status

Proposed

Date

2026-07-28

---

### Context

ADR-008 established prompts as external Markdown files. That leaves open how prompts are located, how their required inputs are validated, how output schemas are bound, and how versions are tracked — all necessary for the prompt testing and regression requirements in `TESTING.md`.

---

### Decision

1. A **prompt registry** (`prompts/_registry.yaml`) is the single source of truth, mapping a stable prompt ID to: file path, semantic version, JSON Schema path, required input variables, and a description.
2. Prompt files carry **YAML front matter** with id, version, purpose, inputs, output schema, and last-modified date.
3. The `PromptRepository` port loads and renders prompts. **Rendering fails loudly if a declared input is missing** — never silently substituting an empty string.
4. **Every prompt is bound to a JSON Schema** in `prompts/schemas/`, enforced by ADR-020's validation path.
5. Prompt version, model identifier and provider are recorded on **every** persisted AI artifact (`message_analyses`, `conversation_summaries`, `memory_proposals`, `reply_suggestions`, `conversation_plans`), so any output can be traced to the exact prompt that produced it and invalidated when that prompt changes.
6. Prompts are rendered with a **restricted template engine with autoescaping disabled and no arbitrary code execution**; untrusted conversation content is inserted only through delimited slots, never through template logic.
7. Startup validation confirms every registry entry resolves to an existing file and schema; a mismatch is a fatal configuration error.

---

### Alternatives Considered

1. Convention-based file discovery — simple, but breaks silently on rename and provides no version or schema binding.
2. Prompts in the database — enables runtime editing, forfeits version control and code review, which are the main benefits of ADR-008.
3. Prompts as Python string constants — rejected by ADR-008.

---

### Reasoning

A prompt is a versioned interface between the application and a model. Treating it with the same rigour as a schema — registry, version, validation, provenance — is what makes AI regression testing possible at all.

---

### Consequences

Pros

- Every AI artifact is traceable to its prompt version
- Cache invalidation on prompt change becomes mechanical
- Missing inputs fail at render time, not in model output

Cons

- Registry must be kept in sync with the filesystem (enforced at startup)
- Schema authoring effort per prompt

---

### Related Decisions

ADR-008, ADR-020, ADR-029.

---

# ADR-027

## Title

Logging Destination

Status

Proposed

Date

2026-07-28

---

### Context

`DATABASE.md` v1.0 defined a `logs` table. Writing application logs into the same SQLite file that carries the application's transactions causes lock contention with the single-writer model (ADR-013), inflates database size and backup time, and makes logs impossible to read when the database itself is the thing failing.

---

### Decision

1. **Application logs are written to rotating JSONL files** under the logs directory, using `structlog`. The `logs` table is removed from the schema.
2. A **central redaction processor** removes secret values, authentication codes and — unless diagnostic logging is explicitly enabled — message content, before any record is emitted.
3. **Audit events are different from logs and remain in the database** (`audit_log` table): security- and privacy-relevant events (login, logout, provider changes, data export, data deletion, plugin install, consent changes). These are few, must be queryable alongside the data they describe, and must survive log rotation.
4. Log retention is configurable with a default of 14 days; rotation is size- and age-based.
5. Diagnostic mode (which may log message content) requires explicit opt-in, is time-limited, and displays a persistent indicator while active.

---

### Alternatives Considered

1. Logs in the database — the original design; causes contention, growth and unavailability precisely when needed.
2. Logs only to files, including audit events — loses queryability and transactional consistency with the data the events describe.
3. External log aggregation — inappropriate for a local-first desktop application (ADR-024).

---

### Reasoning

The two concerns look similar and behave differently: logs are high-volume, disposable and needed when the system is broken; audit events are low-volume, durable and needed as evidence. Separating them gives each the right storage.

---

### Consequences

Pros

- No log-induced write contention
- Logs readable when the database is unavailable
- Audit trail is queryable and durable

Cons

- Two places to look during an investigation
- Audit event schema must be maintained

---

### Related Decisions

ADR-013, ADR-022.

---

# ADR-028

## Title

Configuration and Settings Ownership

Status

Proposed

Date

2026-07-28

---

### Context

The project has both YAML configuration files and a `settings` database table, with no rule for what belongs where. Without one, the same value ends up in both and the two disagree.

---

### Decision

A single ownership rule:

| Concern | Home | Rationale |
|---|---|---|
| Paths, engine selection, provider endpoints, log levels, feature flags, performance limits | **Configuration files + environment** | Machine- and deployment-scoped; belongs in version-controllable, diffable text |
| Secret values | **`SecretStore`** (ADR-021) | Never in files or the database |
| User preferences: theme, language, tone, active provider, auto-approve categories, notification rules, retention periods, per-chat AI mode | **Database (`settings`, `user_profiles`, `chats`)** | User-scoped, changed at runtime through the UI, must survive reinstall and be included in backups |

Rules:

1. **No key exists in both places.** Enforced by a startup check against the declared schema.
2. Precedence for configuration: **defaults → `config/default.yaml` → `config/local.yaml` → environment variables → command-line flags**.
3. All configuration is parsed into **typed, validated models** (`pydantic-settings`); unknown keys are a startup error, not a silent ignore.
4. Configuration is **immutable at runtime**; changing it requires a restart or an explicit reload command. Settings are mutable at runtime and emit `SettingChanged` events.
5. `tgassist config show` prints the fully resolved configuration with every secret value masked.

---

### Alternatives Considered

1. Everything in the database — no version control, awkward to edit when the app cannot start.
2. Everything in files — user preferences become unmanageable from the UI and are lost on reinstall.
3. No formal rule — the status quo; guarantees drift.

---

### Reasoning

The distinction that matters is *who* owns a value and *when* it changes. Deployment-scoped values belong in text that can be reviewed; user-scoped values belong in the data store that is backed up and restored with the user's other data.

---

### Consequences

Pros

- No ambiguity or duplication
- Configuration is reviewable; preferences are portable
- Startup validation catches drift

Cons

- Some values need a documented migration if their ownership ever changes

---

### Related Decisions

ADR-021, ADR-022.

---

# ADR-029

## Title

Composite AI Execution Behind Separate Ports

Status

Proposed

Date

2026-07-28

---

### Context

ADR-006 mandates specialized AI services. Executed literally, a single incoming message triggers seven or more LLM round trips (analysis, emotion, memory extraction, relationship, planning, reply, uncertainty), producing unacceptable latency and per-message cost.

---

### Decision

1. **Ports stay separate.** `ConversationAnalyzer`, `EmotionAnalyzer`, `MemoryExtractor`, `RelationshipAnalyzer`, `ConversationPlanner`, `ReplyGenerator` remain distinct interfaces, preserving ADR-006's testability and replaceability.
2. **Execution may be batched.** A `CompositeAnalysisService` implements several ports at once, satisfying them with a single structured LLM call whose schema contains a section per task. This is an infrastructure detail invisible to the application layer.
3. **Not every service uses an LLM.** `RelationshipAnalyzer` and the Human Behavior Engine are implemented with deterministic formulas over observable signals; they are free, instant, testable and explainable. The LLM is used only for qualitative judgements.
4. **A trigger policy governs invocation.** Analysis does not run on every message. `SuggestionTriggerPolicy` decides based on chat enablement, whether a reply is expected, user request, and rate/cost limits.
5. **Results are cached** in `message_analyses` and `conversation_analyses`, keyed by content, `analysis_version` and prompt version; a prompt or model change invalidates the affected cache entries and nothing else.
6. **Every call is instrumented** (`ai_calls`): provider, model, prompt id and version, input and output tokens, latency, estimated cost, outcome.

---

### Alternatives Considered

1. One call per service — architecturally purest, operationally unaffordable.
2. One monolithic AI service — cheap, but abandons ADR-006 and makes each capability untestable in isolation.
3. Batching decided in the application layer — leaks an infrastructure optimisation into business logic.

---

### Reasoning

Interface granularity and execution granularity are independent choices. Keeping them separate lets the architecture stay clean while the runtime stays affordable, and lets batching be tuned or reverted without touching a use case.

---

### Consequences

Pros

- Order-of-magnitude reduction in calls per message
- ADR-006's benefits retained
- Cost and latency measurable from day one

Cons

- Composite schemas are larger and more failure-prone; mitigated by ADR-020 validation
- One failing section can fail the whole batch; the composite must support partial-result extraction

---

### Related Decisions

ADR-006, ADR-020, ADR-026.

---

# ADR-030

## Title

Developer CLI as a First-Class Adapter

Status

Proposed

Date

2026-07-28

---

### Context

The desktop UI arrives at Milestone 10. Without an earlier interface, Milestones 1–9 have no human-verifiable output, contradicting the roadmap rule that every milestone produces a working application, and leaving AI quality work (M4–M8) impossible to assess.

---

### Decision

1. A **command-line adapter** is built in Milestone 0 and maintained permanently as a supported interface, not a throwaway harness.
2. It is a **presentation-layer adapter** subject to the same dependency rules as the UI: it may not import infrastructure, and it obtains services from the composition root.
3. It grows with each milestone: `version`, `config show`, `doctor` (M0); `db migrate`, `db backup` (M1); `login`, `chats`, `sync`, `watch` (M2); `ai check` (M3); `analyze`, `summarize` (M4); `memory list|approve|reject|forget` (M5); `goal` (M7); `suggest` (M8); `export`, `delete` (M11).
4. **End-to-end tests are driven through the CLI**, giving the e2e suite a stable, scriptable driver that does not depend on Qt.

---

### Alternatives Considered

1. Build the UI earlier — high cost, and UI churn while the domain is still moving is wasted work.
2. Throwaway scripts per milestone — no test value, no consistency, silently rot.
3. Tests only, no interface — cannot evaluate AI output quality, which requires a human reading real suggestions.

---

### Reasoning

A second presentation adapter is the cheapest available proof that the application layer is genuinely UI-independent — the same argument as building a second gateway adapter. It also gives every milestone a demonstrable result and gives the e2e suite a driver that is orders of magnitude simpler than automating a GUI.

---

### Consequences

Pros

- Every milestone is demonstrable
- Validates presentation independence
- Stable e2e driver; useful for power users and support diagnostics

Cons

- Two interfaces to maintain
- CLI commands need their own tests

---

### Related Decisions

ADR-011, ADR-014.

---

# Decision Template

Use this template for future decisions.

```
# ADR-XXX

Title

Status

Date

Context

Decision

Alternatives Considered

Reasoning

Consequences

Future Considerations

Related Decisions
```

---

# Decision Rules

Create a new ADR whenever:

- A new framework is adopted.
- A database changes.
- A major dependency changes.
- The architecture changes.
- A new AI provider is introduced.
- A significant security decision is made.
- A design pattern changes.

Do not overwrite historical decisions.

If a decision changes:

- Mark the old ADR as **Superseded**.
- Create a new ADR explaining the updated decision.
- Cross-reference the related ADRs.

The history of architectural decisions should remain preserved for the lifetime of the project.
