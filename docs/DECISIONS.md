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
**ADR-011 through ADR-039, ADR-041 through ADR-046, and ADR-050 through ADR-062 are Proposed and require explicit approval.**

**ADR-040, ADR-047, ADR-048 and ADR-049 are Accepted and implemented.** ADR-011 through ADR-030 were approved for implementation at the close of the architecture stabilization session; ADR-031 through ADR-039 arose during implementation and await review.

**ADR-040 is Accepted and implemented.**

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
| 031 | Synchronous Event Delivery | Proposed |
| 032 | Secrets as a Domain Value Object | Proposed |
| 033 | Identifier Generation Strategy | Proposed |
| 034 | Single Connection and Serialized Transactions | Proposed |
| 035 | No Generic Repository Base | Proposed |
| 036 | No Optimistic Locking | Proposed |
| 037 | Account Lifecycle Separated from Session Lifecycle | Proposed |
| 038 | UserProfile Identity Is the Account | Proposed |
| 039 | Account Scope Is a Constructor Parameter, Not a Method Argument | Proposed |
| 040 | The CLI Does Not Configure Logging, So Log Records Reach Standard Output | **Accepted** |
| 041 | Contact Identity Is a Local Surrogate Key, Not the Telegram User Identifier | Proposed |
| 042 | Contact Lifecycle: Archived and Deleted as Mutually Exclusive Timestamps | Proposed |
| 043 | Cross-Table Account Ownership Is Enforced by Composite Foreign Keys | Proposed |
| 044 | The Communication Graph Is Established by Chat Alone | Proposed |
| 045 | Message Identity Is Local; the External Identifier Is Optional and Its Index Partial | Proposed |
| 046 | Messages Are Append-Only, and Nothing Deletes Them Yet | Proposed |
| 047 | TDLib Binary Acquisition, Verification and Distribution | **Accepted** |
| 048 | The TDLib Update Loop Runs on a Dedicated Thread, Bridged to asyncio | **Accepted** |
| 049 | Session Models Authorization and Connection as Separate Axes | **Accepted** |
| 050 | Synchronisation Cursors, Batch Boundaries and Batched Event Publication | Proposed |
| 051 | Authorization Is Driven by a Dispatch Loop over a Single Update Stream | Proposed |

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

# ADR-031

## Title

Synchronous Event Delivery

Status

Proposed

Date

2026-07-28

---

### Context

`API.md` version 2.0 section 5.3 specified asynchronous delivery: "`publish()` returns once handlers are scheduled, not once they complete." Implementing the bus made three consequences of that concrete.

**Tests become nondeterministic.** A test that publishes and then asserts on the result of a handler has to wait for something it cannot observe. The usual remedies — sleeping, polling, or draining an internal queue — are either flaky or leak the bus's internals into every test that touches it.

**Events are lost at shutdown.** Scheduled-but-unrun handlers are discarded when the loop stops. For `MessageIngested` that means a message persisted but never analysed, with nothing recording that the analysis was skipped.

**Latency becomes invisible.** A publisher that returns immediately cannot know that its handlers took four seconds, so the one component positioned to react — by deferring work, by reporting progress — never learns.

---

### Decision

`EventBus.publish` is `async` and **awaits every matching handler to completion** before returning. A caller that awaits `publish` can rely on the handlers having run.

The method remains `async` because handlers perform I/O (ADR-013), not because delivery is deferred. Handlers may be plain functions or coroutine functions; the bus awaits the result only when it is awaitable.

Everything else in the section 5.3 contract is unchanged: registration order, failure isolation, automatic disabling, at-most-once, non-durable, immutable events.

`API.md` section 5.3 clause 1 is amended accordingly.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| Synchronous await (chosen) | Deterministic; no lost events; publisher sees latency; trivial to test | A slow handler delays the publisher |
| Fire-and-forget scheduling (original) | Publisher never waits | Nondeterministic tests; events lost at shutdown; unbounded task growth; latency invisible |
| Queue with a background drain | Decoupled within a run | A queue that is neither persisted nor acknowledged has the failure modes of a durable queue without the benefits |
| Synchronous `def publish` | Simplest possible | Excludes async handlers, and every interesting handler performs I/O |

---

### Reasoning

The argument for fire-and-forget is that a publisher should not wait on subscribers. That argument is strong for a distributed bus, where a subscriber may be slow, remote or absent. It is weak for an in-process bus in a desktop application, where the handlers are the application's own code and their latency is the operation's real latency, merely hidden.

Determinism is the decisive factor. An event bus is infrastructure that practically every later use case will depend on, and a nondeterministic foundation makes every test built on it intermittently unreliable — the kind of defect that trains a team to re-run failing tests rather than read them.

If a genuinely long-running handler appears, the correct answer is for that handler to schedule its own background work explicitly, where the decision and its failure handling are visible.

---

### Consequences

Pros

- Deterministic tests without sleeping or polling
- No events lost at shutdown
- Handler latency attributable to the publisher
- Simpler implementation, with no task lifecycle to manage

Cons

- A slow handler delays its publisher; handlers must stay quick or defer explicitly
- Requires `pytest-asyncio` for tests, since publishing is awaited

---

### Related Decisions

ADR-013 (asyncio-first), ADR-025 (plugin fault isolation).

---

# ADR-032

## Title

Secrets as a Domain Value Object

Status

Proposed

Date

2026-07-28

---

### Context

`API.md` version 2.0 section 5.6 specified `SecretStore.get()` returning `str | None`, alongside the rule that "`__repr__` of any object holding a secret must not include it". A bare `str` cannot satisfy that rule: its `repr` is the value.

Two existing defences already cover most disclosure paths — the logging processor strips fields whose *name* looks sensitive, and strips values whose *shape* matches a known credential format. Neither catches a secret passed under an innocuous name in an unrecognised format:

```python
logger.info("provider_configured", value=api_key)
```

Nor does either cover exception tracebacks, debugger output, f-strings or pytest assertion messages, none of which pass through the logging pipeline at all.

---

### Decision

Introduce `SecretValue`, a domain value object wrapping a secret string:

1. `__repr__`, `__str__` and `__format__` return a mask, so the value stays hidden on every incidental rendering path.
2. `reveal()` returns the real value. The deliberately conspicuous name makes every disclosure point greppable and reviewable.
3. Equality uses a constant-time comparison, so comparing secrets does not leak content through timing.
4. Pickling raises. Serialising a secret is nearly always an accident — a cached object, a multiprocessing argument, a persisted session — and failing loudly beats writing the value somewhere durable.
5. `len()` and truthiness are available without revealing, so an empty credential can be rejected before a request is attempted.

`SecretStore.get()` returns `SecretValue | None`; `set()` takes a `SecretValue`. `API.md` section 5.6 is amended accordingly.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| `SecretValue` wrapper (chosen) | Covers tracebacks, debuggers and f-strings, not only logging; disclosure points are greppable | One more type at every boundary; `reveal()` at each use |
| Bare `str` with redaction only | Nothing new to learn | Cannot satisfy the documented `__repr__` rule; misses tracebacks and debuggers entirely |
| A `NewType` alias | Zero runtime cost, some static signal | No runtime behaviour at all, so `repr` still discloses |
| Encrypted in memory | Defeats a memory-reading attacker | That attacker can read the decryption key too; real cost, illusory benefit |

---

### Reasoning

This is a safety net against accident, not a boundary against an attacker. Anything able to read process memory can read the value regardless. What the wrapper prevents is the far more likely failure: a credential reaching a log file, a crash report, a screenshot or a pasted stack trace.

The cost is one `reveal()` call at each genuine use — perhaps a dozen sites across the application — and that call is exactly the marker a security review wants to be able to find.

---

### Consequences

Pros

- Secrets stay masked in tracebacks, debuggers, f-strings and assertions
- Disclosure points are explicit and searchable
- Constant-time comparison for free

Cons

- `SecretStore.get()` no longer returns a plain string
- Callers must call `reveal()`, which is the point but is still ceremony

---

### Related Decisions

ADR-021 (secret management), ADR-027 (logging and redaction).

---

# ADR-033

## Title

Identifier Generation Strategy

Status

Proposed

Date

2026-07-28

---

### Context

`API.md` specified an `IdGenerator` port without saying what the identifiers look like. The choice matters more than it appears, because identifiers become database keys and the `messages` table is expected to reach hundreds of thousands of rows.

A random key scatters inserts across the whole index. Every write dirties a different page, the working set grows with the table, and insert throughput degrades over months — which for this application means a sync that starts fast and gets slower the longer someone uses it. A time-ordered key appends to the end of the index, so inserts touch one page and stay fast at any size.

---

### Decision

Use **UUID version 7** (RFC 9562) as the generation strategy:

1. `new_uuid()` returns a canonical UUID version 7 string: a 48-bit millisecond timestamp, a 12-bit counter, and 62 random bits.
2. `new_id()` returns a 60-bit integer using the same timestamp and counter, so integer and UUID identifiers from one generator share an ordering.
3. `new_correlation_id()` returns a UUID version 7 string, so log records sort into request order.
4. The counter occupies the `rand_a` field, which RFC 9562 permits. This makes identifiers **strictly** increasing rather than merely increasing per millisecond — "sortable" is only useful if two identifiers created in the same millisecond still sort in creation order.
5. When the clock moves backwards (an NTP correction, a virtual machine resuming), the generator holds its previous timestamp until wall time catches up. Uniqueness is preserved by the counter.
6. When more than 4096 identifiers are generated in one millisecond, the generator advances its logical millisecond rather than blocking or colliding.
7. Time comes from the injected `Clock`, so fixing the clock fixes the identifiers and any test involving generated identifiers stays deterministic.
8. `uuid.uuid7` is used when the standard library provides it (Python 3.14 and later); otherwise this project implements the same layout.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| UUID version 7 (chosen) | Time-ordered, standardised, no coordination, index-friendly | Encodes creation time, so unsuitable where that is sensitive |
| UUID version 4 | Maximum unpredictability | Scatters index inserts; the exact problem being avoided |
| Database autoincrement only | Simplest; perfectly ordered | No identity before insert, so an entity cannot be assembled and validated before saving |
| ULID | Same ordering properties | Not a standard UUID; needs a dependency and a custom column type |
| Snowflake | Compact, time-ordered | Requires machine-identifier coordination, which a single-user desktop application does not have and does not need |

---

### Reasoning

UUID version 7 gives the index locality of a sequential key without requiring the database to assign it, which is what allows an entity to be constructed and validated before it is persisted. It is a published standard rather than a project invention, and it is arriving in the standard library, so the local implementation is a bridge rather than a permanent obligation.

The counter and backwards-clock handling are the parts most easily got wrong, and both are covered by tests that run the generator against a frozen clock and against a clock that jumps backwards.

---

### Consequences

Pros

- Insert performance stays flat as tables grow
- Identifiers sort into creation order, which makes logs and exports readable
- Deterministic under a fixed clock

Cons

- Identifiers **encode their creation time**. They must never be used where guessing one, or learning when it was created, would be a security problem; that is what the secret store is for.
- Sustained generation above 4096 per millisecond would drift the logical clock ahead of wall time. This is far beyond any expected rate, and drifting is preferable to colliding.
- A local implementation to remove once Python 3.14 is the floor.

---

### Related Decisions

ADR-013 (concurrency), ADR-015 (database access layer).

---

# ADR-034

## Title

Single Connection and Serialized Transactions

Status

Proposed

Date

2026-07-28

---

### Context

ADR-013 established that SQLite work runs on one dedicated worker thread, so that SQLite's single-writer rule is structural rather than aspirational and `SQLITE_BUSY` cannot occur.

Implementing the persistence layer showed that the threading decision implies a connection decision, and that the connection decision has consequences the original ADR did not state.

**One thread implies one connection.** A pool would hand different connections to the same thread at different times, and an in-memory database — used by every test — lives *inside* its connection, so a second connection is a second, empty database. The implementation therefore holds exactly one connection for the process lifetime, via `StaticPool`.

**One connection implies serialized transactions.** A SQLAlchemy connection holds at most one transaction. Two concurrent use cases each opening a unit of work would find a transaction already open, and the second would fail with an error that has nothing to do with what the caller did wrong. This was not hypothetical: it was the failure that the concurrency test produced on first run.

**A long-lived connection also changes how SQLAlchemy behaves.** SQLAlchemy 2.0 "autobegins" — any statement on a connection without an explicit transaction opens one. On a short-lived connection this is invisible, because the transaction ends with the connection. On a connection held for the process lifetime, an autobegun transaction from a pragma read or a health check persists and blocks the next explicit `begin()`.

---

### Decision

1. **Exactly one connection**, held for the process lifetime, using `StaticPool`. This expresses the design directly. A sized pool would express "a pool that happens to hold one", and its second checkout would block permanently rather than fail clearly.
2. **`check_same_thread` is disabled.** SQLite's own thread guard is redundant here and would reject the executor's worker thread. Thread affinity is instead guaranteed structurally, because every statement runs through `DatabaseExecutor`, which owns exactly one thread.
3. **Units of work serialize on an `asyncio.Lock`.** A second concurrent transaction waits rather than failing. Queuing is what the single-writer model already implies for statements; this extends it to whole transactions.
4. **Lock acquisition is bounded** (30 seconds, configurable). The pathological case — two overlapping transactions in the same task — becomes a diagnosable `TransactionFailedError` naming the cause, rather than a silent hang.
5. **Every read outside a unit of work releases its autobegun transaction.** Only reads happen outside a unit of work, so rolling back is safe by construction, and the alternative is that the next real transaction cannot start.

---

### Consequences

Pros

- `SQLITE_BUSY` cannot occur; there is no retry loop and no intermittent lock failure
- Transaction semantics are simple: one transaction at a time, in a defined order
- In-memory databases work identically to file databases, so tests exercise the real code path
- Overlapping-transaction defects surface as a named error rather than a hang

Cons

- **Reads serialize behind writes.** A long backfill transaction delays interface queries. This is the significant limitation and the reason for the measurement plan below.
- Throughput is bounded by one thread. Adequate for a single-user desktop application; it would not be for a server.
- A held connection makes SQLAlchemy's autobegin behaviour something every read path must account for.

---

### The read-concurrency question, deliberately left open

WAL mode permits one writer *and many concurrent readers*. This design forfeits that: everything goes through one connection on one thread, so a reader waits behind a writer even though SQLite would not require it.

The natural remedy is a **reader pool** — one writer connection plus several read-only connections, with the unit of work taking the writer and read-only queries taking a reader. WAL makes this safe without additional locking.

It is not being implemented now, for two reasons. It roughly doubles the persistence layer's concurrency surface, adding read-your-own-writes questions that do not exist today. And there is no evidence yet that it is needed: the cost only matters if a backfill is long enough, and interface queries frequent enough, for the delay to be perceptible.

**Evidence required before adopting it**, to be gathered at Milestone 13 against a database seeded with 500,000 messages:

1. Measured p95 latency of an interface query issued *during* a sustained backfill.
2. Comparison against the `PROJECT_SPEC.md` section 5 target of under 100 ms for a history page.
3. Confirmation that the delay is attributable to transaction serialization rather than to the query itself.

If p95 exceeds the target and serialization is the cause, the reader pool is the remedy and warrants its own ADR. If not, this design stands and the simplicity is kept.

The seam already exists: `DatabaseExecutor` and the connection accessor are the only two places that would change.

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| One connection, serialized transactions (chosen) | Simplest correct model; no lock contention; no retry logic | Reads wait behind writes |
| Connection pool, several writers | Parallel writes | SQLite serializes writes anyway; produces `SQLITE_BUSY` and a retry loop for no throughput gain |
| Writer connection plus reader pool | Uses WAL as intended; reads never wait | Doubles the concurrency surface; unjustified without measurement |
| Fail rather than queue on a busy transaction | Surfaces contention immediately | Turns ordinary concurrency into an error the caller cannot act on |

---

### Related Decisions

ADR-013 (concurrency model), ADR-015 (database access layer), ADR-016 (PostgreSQL path — where a real pool becomes both possible and necessary).

---

# ADR-035

## Title

No Generic Repository Base

Status

Proposed

Date

2026-07-28

---

### Context

A generic `Repository[T, ID]` with `create`, `update`, `delete`, `find_by_id`, `exists` and `count`, inherited by every concrete repository, is close to universal in layered applications. Milestone 1.0 asked whether this project should have one.

Examining the aggregates in `DOMAIN_MODEL.md` against that interface shows it does not fit any of them well and actively breaks one:

* **`Message`** is append-only and arrives in bulk. It is never updated except to record a remote edit, never deleted except by retention. Its meaningful write is `add_batch`, which a single-entity `create` cannot express, and batching is what makes history backfill viable at all.
* **`AuditEvent`** has no update or delete path whatsoever. An architectural test currently asserts that `AuditRepository` exposes no mutation method, because an audit trail that can be rewritten is not an audit trail. A base class providing `delete` would either break that guarantee or force the subclass to inherit a method that raises.
* **`RelationshipProfile`** is a computed singleton per contact, upserted whole. "Create" and "update" are the same operation, so two of the six base methods are one method wearing two names.
* **`Memory`** cannot be updated in the ordinary sense. Changing a value must create a `MemoryRevision` in the same transaction (invariant 10), so its write takes two arguments where `update(entity)` takes one. The repository interface enforces this today: `update()` *requires* a revision, which is what makes the invariant unbreakable rather than merely documented.

The intersection of these lifecycles is close to empty. The union is a base class most of whose methods are wrong for most of its subclasses.

---

### Decision

**No generic repository base class or interface.**

Each aggregate declares its own repository port in the domain, exposing only the operations that aggregate actually supports, named for the intent they serve.

What *is* shared is documented as a **contract** rather than expressed as a type: account scoping, domain objects only, absence returning `None`, soft-delete exclusion, typed errors, no transaction control, keyset pagination, no business logic. That contract is enforced by a shared test suite (`tests/support/repository_contract.py`) that every implementation runs, rather than by inheritance.

The **mechanics** are shared through an infrastructure base class (`Repository[T]`): transaction-aware execution, pagination, mapping and error normalisation. This is inheritance for code reuse, not for polymorphism -- no caller ever holds a `Repository` and asks it to do something generic.

Correspondingly, **`ReadRepository` / `WriteRepository`**, **`Specification`** and a **`RepositoryFactory` registry** are also omitted:

* A read/write split needs a consumer that holds a repository and must be prevented from writing. The only read-only consumer in the design is a plugin, and plugins receive `PluginDataAccess` -- a separate, narrower surface -- rather than repositories at all.
* A specification pattern exists to compose predicates at runtime, which is needed for user-built queries: report builders, admin search screens. Every query in this application is fixed and known at development time, and each one is matched to an index. A specification that must become SQL either drags SQLAlchemy into the domain or needs a translator layer larger than the queries it replaces.
* A factory registry is a service locator with a nicer name. Repositories are instead created by a `RepositoryFactory` **type alias** -- a callable taking a unit of work -- which use cases declare as constructor parameters. A use case that needs four repositories says so in its signature.

---

### Consequences

Pros

- No repository inherits a method that is wrong for its aggregate
- `AuditRepository` remains structurally incapable of deletion
- `MemoryRepository.update` can require a revision, making an invariant unbreakable
- Query surfaces stay small, so every query can be matched to an index and measured
- A use case's dependencies are visible in its signature

Cons

- Each repository declares its own port, which is more interface code than one shared base
- The shared obligations live in prose and a test suite rather than in a type, so a new repository could omit one. The contract suite is the mitigation and is why it exists.
- No compile-time guarantee that a component holding a repository will not write to it

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| No generic base (chosen) | Each repository fits its aggregate; invariants enforceable per aggregate | Shared rules enforced by tests rather than types |
| Generic CRUD base | Familiar; less interface code | Wrong for four of the five aggregates examined; breaks the audit guarantee |
| Generic base plus opt-out mixins | Reuse where it fits | The opt-outs outnumber the opt-ins, which is the abstraction telling you it does not fit |
| Read/write split | Compile-time read-only guarantee | No consumer needs it; plugins use a different surface entirely |

---

### Related Decisions

ADR-004 (repository pattern), ADR-015 (SQLAlchemy Core, no ORM), ADR-034 (connection and transaction model).

---

# ADR-036

## Title

No Optimistic Locking

Status

Proposed

Date

2026-07-28

---

### Context

Optimistic locking -- a version column compared on write, rejecting the update if it changed -- protects against lost updates when two writers read the same row, both modify it, and the second overwrites the first.

Milestone 1.0 asked whether the repository framework should provide it.

---

### Decision

**No optimistic locking, and no version column.**

The database-level race it protects against **cannot occur** in this design. Every write goes through one connection on one thread, and transactions serialize on a lock (ADR-034). Two transactions cannot interleave: the second begins only after the first has committed or rolled back, so it necessarily reads the first transaction's result. The classic lost update is structurally impossible.

A second, genuinely different race does exist: a user opens a memory for editing, thinks for two minutes, and saves -- while a background job has updated that memory in between. Transaction serialization does not help here, because the read and the write are in different transactions separated by human time.

That race is real but narrow, and optimistic locking is the wrong answer to it:

1. It affects one aggregate family in practice -- user-editable records, principally `Memory` and `Goal`.
2. For `Memory` specifically, a better mechanism already exists. Value changes create a `MemoryRevision` and conflicts are resolved by supersession (ADR-019), so a concurrent change is *merged and recorded* rather than rejected with an error the user can do nothing useful about.
3. A version conflict surfaced to a user who has just spent two minutes editing is a poor outcome. "Your change was rejected, try again" loses their work.

Adding the machinery now would also violate the project's own rule that only branches with a live consumer are implemented: there is no aggregate to version yet.

---

### Revisit criteria

This decision is reconsidered if any of the following becomes true:

1. **Multi-device synchronisation** is implemented. Two devices writing the same record is a genuine distributed lost update, and neither transaction serialization nor revisions solve it.
2. **PostgreSQL with a real connection pool** is adopted (ADR-016), removing the single-connection serialization this decision rests on.
3. A user-facing editing surface appears for an aggregate that has **no revision history**, where a silent overwrite would lose work invisibly.

The seam is small: a `version` column, a `WHERE version = :expected` clause in the update, and a typed `ConcurrentModificationError`. Retrofitting it per aggregate is a contained change, which is part of why deferring is safe.

---

### Consequences

Pros

- No version column, no conflict-handling code, no error path with no good user response
- The mechanism that would be needed is already present where it matters, in a form that merges rather than rejects

Cons

- The think-time race is unhandled for aggregates without revision history. Currently there are none; when the first appears, this ADR is the record of why it was not already covered.
- If the concurrency model changes, this decision must be revisited rather than assumed

---

### Related Decisions

ADR-013 (concurrency), ADR-019 (memory approval and revisions), ADR-034 (serialized transactions), ADR-016 (PostgreSQL path).

---

# ADR-037

## Title

Account Lifecycle Separated from Session Lifecycle

Status

Proposed

Date

2026-07-28

---

### Context

`DOMAIN_MODEL.md` version 1.0 gave the Account aggregate this lifecycle:

```
created → authenticating → active → suspended → logged_out → deleted
```

while giving it a single boolean, `is_active`, to represent it. A boolean cannot express six states, so implementing the aggregate forced the question of what the missing five were for.

Comparing them against `Session` (section 5.3) showed the answer: they were already modelled there.

| Account state (v1.0) | Also a Session state? |
|---|---|
| `authenticating` | Yes — `awaiting_phone`, `awaiting_code`, `awaiting_password` |
| `logged_out` | Yes — `logged_out` |
| `active` | Partly — Session's `ready` |
| `created`, `suspended`, `deleted` | No |

Three of the six duplicate Session's states, and Session models them properly as a state machine with defined transitions. Two entities would have owned the answer to "is this account authenticated", and two owners of one fact eventually disagree — usually at the moment a connection drops and only one of them is updated.

---

### Decision

**Account owns only the lifecycle it genuinely has**, which is whether the user has selected it:

```
created → active ⇄ inactive → deleted
```

represented by `is_active`, with `created` and `deleted` being the presence or absence of the row.

**Session owns authentication state**, unchanged from `DOMAIN_MODEL.md` section 5.3.

`suspended` is dropped rather than reassigned. It described a state no code produced and no user action reached, and its meaning was never defined — a suspended account is either inactive (which `is_active` covers) or logged out (which Session covers).

The single-active invariant is enforced by a **partial unique index** on `is_active`, so a second activation fails at the database rather than depending on every future caller remembering to deactivate first.

`DOMAIN_MODEL.md` section 5.1 is corrected accordingly.

---

### Consequences

Pros

- One owner for authentication state, so the two cannot disagree
- `is_active` is sufficient for the lifecycle Account actually has, so no field is speculative
- The invariant is structural rather than conventional
- Milestone 2 inherits an unambiguous division: Session handles the auth state machine, Account records which one the user selected

Cons

- Multi-account switching cannot express "this account is temporarily unavailable because its session dropped". That is a *derived* state — inactive Account plus disconnected Session — and computing it at the point of display is better than storing a third copy that can go stale.
- `DOMAIN_MODEL.md` changes, so anyone who read the original lifecycle must re-read it

---

### Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| Account owns only selection (chosen) | One owner per fact; no speculative fields | Availability must be derived at display time |
| Full status enum on Account | Matches the documented lifecycle literally | Duplicates Session; two owners of one fact |
| Status enum on Account, remove Session's | One state machine | Conflates "which account" with "is it connected"; multi-account would need per-account auth state anyway |
| Leave undecided until Milestone 2 | No decision now | The Account table is written in this milestone, and adding or removing a status column later is a migration plus a rewrite of everything that reads it |

---

### Related Decisions

ADR-012 (Telegram adapter), ADR-035 (no generic repository base). Milestone 2 implements Session against this division.

---

# ADR-038

## Title

UserProfile Identity Is the Account

Status

Proposed

Date

2026-07-28

---

### Context

`DOMAIN_MODEL.md` version 1.0 listed UserProfile with a `user_profile_id` surrogate key alongside `account_id`, and with fields including `display_name`, `timezone`, `available_hours`, `auto_approve_memory_categories` and `confidence_thresholds`.

Implementing it raised two questions the document did not answer.

**First: what does the surrogate key buy?** An account has exactly one profile, and a profile cannot exist without an account. A separate identifier would therefore always be in one-to-one correspondence with `account_id`, and would introduce a second way to name the same row. Two names for one row is how a query eventually reads by one and writes by the other.

**Second: several fields already have an owner, or have no owner yet.**

| Field (v1.0) | Problem |
|---|---|
| `display_name` | Account already has one, and it is Telegram's own name for the user |
| `timezone` | Account already has one, validated against the IANA database |
| `available_hours` | Can contradict `quiet_hours`; no rule says which wins |
| `auto_approve_memory_categories` | The category vocabulary belongs to the Memory aggregate (Milestone 5) |
| `confidence_thresholds` | The confidence scale is defined by the suggestion pipeline (Milestone 8) |

---

### Decision

**The account identifier is the profile identity.** `account_id` is simultaneously the primary key and a foreign key to `accounts(id)` with `ON DELETE CASCADE`. There is no surrogate key.

`UserProfile` carries only the preferences that shape generated replies:

```
account_id, primary_language, tone_preference,
preferred_message_length, emoji_usage, quiet_hours,
created_at, updated_at
```

`display_name` and `timezone` remain on Account; the profile does not restate them.

`available_hours`, `auto_approve_memory_categories` and `confidence_thresholds` are **deferred**, not rejected. Each is added when the aggregate that defines its meaning exists, so that its values can be validated rather than merely stored.

`quiet_hours` is a `TimeRange` in minutes past midnight rather than two times, because a quiet period normally wraps midnight and a pair of naive time values makes 22:00-08:00 look empty.

---

### Alternatives Considered

**Keep the surrogate key.** Uniform with aggregates that genuinely need one, and would allow more than one profile per account later. But "later" is speculative, and until then the key is a second name for a row that already has one. Reintroducing it is a migration; removing it after code depends on it is a larger one.

**Fold the preferences into Account.** Removes a table and a join. Rejected because Account is identity - who the user is on Telegram - and the profile is preference - how they want replies written. They change for different reasons and at different rates, and merging them would put a settings screen worth of columns in the row every other aggregate references.

**Implement every v1.0 field now.** Rejected: three of them cannot be validated, because the vocabulary each draws on does not exist yet. A column that accepts anything is worse than an absent column, because data accumulates in it before the rules are known.

---

### Consequences

Pros

- One identity per profile, so no query can read by one name and write by another
- The primary key is already the index needed for every lookup, so no additional index exists
- Cascade deletion is expressed by the schema, not by application code that must remember to run
- Every column has a validated domain, checked in the entity and restated as a CHECK constraint

Cons

- If a future requirement genuinely needs several profiles per account - for example per-contact overrides - a surrogate key must be introduced by migration
- Callers wanting the user display name must read Account, not the profile

The second is intended: it keeps one owner for that fact.

---

### Future Considerations

Per-contact preference overrides are the likeliest reason to revisit this. They would most naturally live on the Contact relationship rather than as additional UserProfile rows, which would leave this decision intact.

---

### Related Decisions

ADR-037 (Account lifecycle), ADR-039 (account scoping), ADR-004 (repository pattern). `DOMAIN_MODEL.md` section 5.2 is corrected accordingly.

---

# ADR-039

## Title

Account Scope Is a Constructor Parameter, Not a Method Argument

Status

Proposed

Date

2026-07-28

---

### Context

Nearly every aggregate after Account belongs to one account: profiles, contacts, conversations, messages, memories. Each therefore needs its queries filtered by account, and a filter that is applied by convention is applied incorrectly eventually.

The conventional interface takes the account per call:

```python
async def get(self, account_id: AccountId) -> UserProfile | None: ...
async def update(self, account_id: AccountId, profile: UserProfile) -> None: ...
```

Every call site must then supply the right value, every time, for the life of the project. One omission or one stale variable returns - or overwrites - another account data. Nothing in the type system objects, and a test only catches it if it happens to exercise two accounts at once.

---

### Decision

**A repository over account-owned data is scoped when it is constructed, and no method accepts an account identifier.**

```python
class UserProfileRepository(Protocol):
    @property
    def account_id(self) -> AccountId: ...
    async def get(self) -> UserProfile | None: ...
    async def add(self, profile: UserProfile) -> None: ...
    async def update(self, profile: UserProfile) -> None: ...
```

Construction goes through a factory type added to `domain/ports/repository.py`:

```python
ScopedRepositoryFactory = Callable[[UnitOfWork, AccountId], R_co]
```

A use case declares the factory as a dependency and supplies the account once, inside its transaction. There is no value left for a caller to get wrong, because there is no parameter to pass.

Writes are checked as well as reads. The scope makes a cross-account *read* impossible, but a caller could still hand the repository an entity built for another account, which would overwrite the wrong row. Every write therefore verifies that the entity `account_id` matches the scope, and raises `DomainValidationError` otherwise.

---

### Alternatives Considered

**Account identifier per method.** Familiar, and one object serves every account. Rejected: correctness rests on every call site, forever, and the failure mode is silent cross-account data exposure - the single worst outcome for an application holding private conversations.

**A row-level security predicate applied by the unit of work.** Attractive because it is enforced centrally and cannot be bypassed. Rejected for now: SQLite has no row-level security, so it would have to be implemented by rewriting statements, which is considerably more machinery than a constructor parameter and fails open if the rewriting misses a query shape. It becomes worth revisiting if PostgreSQL is adopted (ADR-007).

**Passing the account to the unit of work.** Would scope every repository at once. Rejected: not all data is account-owned - schema metadata and the account table itself are not - so the unit of work would carry a value most of its users must ignore.

---

### Consequences

Pros

- Cross-account access is structurally impossible for reads, and rejected explicitly for writes
- The guarantee is testable directly: the contract suite asserts, by inspecting the signatures, that no method accepts an account
- Two scoped repositories over the same storage are the normal test setup, so isolation is exercised rather than assumed
- The pattern extends unchanged to Contact, Conversation, Message and Memory

Cons

- A repository instance serves one account, so code operating across accounts constructs one per account
- One more type (`ScopedRepositoryFactory`) alongside `RepositoryFactory`

The first is a fair price: work spanning accounts is rare, and making it explicit is the point.

---

### Future Considerations

If PostgreSQL is adopted, row-level security can be added *underneath* this decision as defence in depth. The interface would not change.

---

### Related Decisions

ADR-004 (repository pattern), ADR-035 (no generic repository base), ADR-038 (UserProfile identity), ADR-007 (SQLite for MVP).

---

# ADR-040

## Title

The CLI Does Not Configure Logging, So Log Records Reach Standard Output

Status

Accepted

Date

2026-07-28 (proposed), 2026-07-28 (accepted and implemented)

---

### Context

Found while testing the `profile` commands, not by reading the code: comparing the output of two identical `profile show` invocations showed a stray line in the first.

```
2026-07-28 06:12:03 [debug    ] transaction_committed  duration_ms=12.74 events=0
account          : 7312346003582976
language         : en
```

`Container.create` is called from `_open` in `presentation/cli/app.py` with `configure_logging_on_start=False`, carrying this comment:

> Logging is deliberately not configured here: these commands report on the application rather than run it, and reconfiguring global logging would interleave log records with their output.

The intent is right; the effect is the opposite of the intent. `configure_logging` is what installs the structlog configuration. Without it, structlog falls back to its **default** configuration - a `PrintLogger` writing to standard output with no level filtering. Every record any layer emits is printed, at every level, mixed into the command own output.

Confirmed directly:

```
structlog configured: False   root handlers: []

$ tgassist account create 100 P
[warning  ] migration_without_backup  detail=No backup provider is registered...
[info     ] migration_applied         from_revision=None to_revision=0003
[debug    ] transaction_committed     duration_ms=12.71 events=1
Created account 7312346355339264 (P).
```

Two consequences follow. Output is polluted, which matters for any command a user might pipe or parse. And the entire `logging` configuration section - `console_enabled`, `file_enabled`, `level`, `component_levels`, the rotating file handler, the secret-redaction processors - **is ignored for every CLI command**. The redaction point is the serious one: `mask_secret_values` is installed by `configure_logging`, so records printed on this path have never passed through it.

The defect predates this milestone; it was invisible until a command emitted a record. The Milestone 1.1 CLI tests assert with substrings and so did not notice.

---

### Decision

**`_open` configures logging** using the loaded configuration - that is, it calls `Container.create` without suppressing it. The original goal (clean command output) is reached by configuration rather than by omission, and the configuration a user writes is honoured.

**The CLI applies no logging behaviour of its own.** It uses the same configuration as every other entry point, with no CLI-specific default and no second initialisation path. `configure_logging` remains the single place logging is set up.

This is a correction to the proposal, which suggested the CLI should default console logging **off**. That was written before checking where the console handler writes: it is constructed with `stream=sys.stderr`, so configured output never touches standard output in the first place. A CLI-specific default would therefore have bought nothing except a second answer to "what is the log level", and a user who set `console_enabled: true` would have found it ignored - the same class of defect this ADR exists to remove.

What remained was noise from *third-party* libraries. Routing every record through one processor chain is deliberate (redaction must cover more than our own call sites), but at `DEBUG` it also means the event loop announcing its selector and Alembic narrating its migration context. `config/default.yaml` therefore ships `component_levels` for `asyncio`, `alembic` and `sqlalchemy.engine` at `WARNING`. This is configuration, not code: a developer who wants the detail raises it, and the application's own records are untouched.

Concretely, as implemented:

1. `configure_logging_on_start=False` removed from `_open`. The parameter remains on `Container.create` for tests that must not mutate process-wide logging state.
2. Third-party component levels added to the shipped configuration.
3. `tests/conftest.py`'s `restore_logging` fixture also calls `structlog.reset_defaults()`, because structlog's configuration is process-wide too and a test invoking a command would otherwise leave every later test running against whatever that command installed.

Behaviour after the change: standard output carries only what the command printed; log records go to the sinks the configuration names; `level`, `console_enabled`, `file_enabled`, `format` and `component_levels` all take effect; and records on this path pass through `mask_secret_values` like every other.

---

### Alternatives Considered

**Leave it and filter in tests only.** Rejected: it leaves secret redaction disabled on the CLI path and leaves user configuration silently ignored. A test workaround does not fix either.

**Call `structlog.configure` with a null logger in `_open`.** Suppresses the output with a smaller change. Rejected: it also discards file logging, so a user who asked for a log file would not get one, and the configuration would still be ignored - the same defect with quieter symptoms.

**Send all records to standard error instead.** Fixes the pollution of standard output. Rejected as insufficient on its own: redaction and level filtering are still absent. It is a reasonable addition *after* the configuration is applied.

---

### Consequences

Pros

- Configuration is honoured on every path, including redaction
- Standard output contains only what the command printed, and is byte-identical across repeated runs
- One initialisation path, so there is no second answer to what the log level is
- Diagnostics remain available on standard error, where diagnostics belong

Cons

- The development profile logs at `DEBUG`, so a developer running a command sees several records on standard error. That is the configuration doing what it says; `TGASSIST_LOGGING__LEVEL` or the profile file changes it.
- Every command now creates the log directory and opens the file sink, where previously the CLI wrote no log file at all. That is the intended behaviour, but it is a behaviour change for anyone who relied on the CLI being silent on disk.

---

### Future Considerations

A `--verbose/-v` flag mapping to a console log level would make the level adjustable without an environment variable. It needs a Typer callback, which changes the command surface, so it is left for whenever the CLI grows global options for another reason.

---

### Related Decisions

ADR-018 (logging and observability), ADR-024 (secret handling), ADR-011 (dependency direction). Implemented in `presentation/cli/app.py` and `config/default.yaml`; no change to `infrastructure/logging`.

---

# ADR-041

## Title

Contact Identity Is a Local Surrogate Key, Not the Telegram User Identifier

Status

Proposed

Date

2026-07-28

---

### Context

Contact is the first aggregate describing somebody other than the operator, and
the anchor every later aggregate references: memories are about a contact, goals
are pursued with a contact, a private chat belongs to one. Its key therefore
appears in six tables that do not exist yet, which makes this the last cheap
moment to choose it.

Telegram already assigns every user a stable numeric identifier. Four candidates
follow from that.

| Candidate | Key |
|---|---|
| Natural | `telegram_user_id` alone |
| Composite | `(account_id, telegram_user_id)` |
| Surrogate | locally generated `contact_id` |
| Dual | surrogate key, natural unique index |

`DOMAIN_MODEL.md` section 5.4 lists both an `id` and the invariant that
`(account_id, telegram_user_id)` is unique, which is the dual arrangement -- but
it gives no reasoning, and the reasoning is what determines whether it survives
contact with the Chat and Message tables.

---

### Decision

**A locally generated `ContactId` is the primary key, and
`(account_id, telegram_user_id)` is a unique index.**

Three reasons, in descending order of how much they matter.

**The Telegram identifier is not unique in this table.** The same person can be
known to two accounts, and the two Contacts are genuinely different rows --
what is remembered about somebody, and what the operator is trying to achieve
with them, differs per account. `telegram_user_id` alone is therefore not a
candidate key at all, and the natural key is the pair. A composite key would
then propagate into every child table's foreign key: `messages` would carry
`(account_id, contact_id)` rather than `contact_id`, and every join would compare
two columns. That is a cost paid on every table for the life of the project.

**A foreign system would own our identifiers.** Telegram's identifier space is
theirs. It has already changed once in the platform's history -- user ids
outgrew 32 bits -- and a key we do not control is a key we cannot migrate on our
own schedule. A surrogate key insulates every child table from that.

**Not every contact need come from Telegram.** Imported or manually created
contacts have no Telegram identifier. Nothing requires that today, which is why
this is the weakest of the three, but it costs nothing to keep open and would be
expensive to reopen.

The unique index is what preserves the documented invariant, and it is
**deliberately not partial**: it covers soft-deleted rows. A deleted contact
still holds that person's history, so allowing a second row for the same person
would split the history between two contacts, with no way to say which is
correct. Re-adding a deleted contact is therefore refused, and the caller is told
to restore instead. That consequence is visible in the interface:
`get_by_telegram_id` takes `include_deleted`, and creation passes it, so the
message names the real situation rather than reporting a constraint violation.

---

### Alternatives Considered

**`telegram_user_id` as the primary key.** Fewer columns, no generator, and
synchronisation could insert without a lookup. Rejected because it is not
unique: multi-account support is a stated requirement (`PROJECT_SPEC.md` section
4.11), and this key would make the second account impossible without a migration
touching every table that references a contact.

**The composite `(account_id, telegram_user_id)`.** Correct, and it expresses
ownership in the key itself, which is genuinely attractive -- a child row could
not reference a contact without also naming its account. Rejected on cost: every
child table carries both columns, every join compares both, and every index
grows. The scope guarantee it would provide is already provided structurally by
the scoped repository (ADR-039), so the cost buys a second copy of something we
have.

**A surrogate key with no natural unique index.** Simplest, and tempting because
synchronisation could then upsert freely. Rejected: without the index, one
Telegram user could become two contacts through a retry or a race, and the
duplicate would be discovered later as two half-populated relationship profiles.

---

### Consequences

Pros

- Child tables carry one narrow column, both in their foreign keys and in their
  indexes
- Identifiers are ours, and are not affected by Telegram changing theirs
- The documented uniqueness invariant is enforced by the database
- The same person can be known to several accounts, independently

Cons

- Synchronisation must look a contact up by Telegram identifier before writing,
  rather than inserting on the natural key. That lookup is indexed, and it is
  needed anyway to decide between insert and update.
- Two identifiers exist for one person, so a log record naming only one of them
  is harder to correlate. Records therefore carry `contact_id`, and the CLI shows
  both.

---

### Future Considerations

If contacts from other platforms arrive, `telegram_user_id` becomes one of
several external identifiers and would move to a `contact_identities` table
keyed by `(contact_id, platform, external_id)`. This decision is what makes that
an additive change rather than a rewrite.

---

### Related Decisions

ADR-039 (account scoping), ADR-038 (UserProfile identity -- the opposite
conclusion, for the opposite reason: a profile is one-to-one with its account and
is referenced by nothing, so a surrogate key there would have been a second name
for one row), ADR-042 (Contact lifecycle).

---

# ADR-042

## Title

Contact Lifecycle: Archived and Deleted as Mutually Exclusive Timestamps

Status

Proposed

Date

2026-07-28

---

### Context

`DOMAIN_MODEL.md` version 1.0 gives Contact the lifecycle
`discovered → active → dormant → archived → deleted`, and separately lists the
attributes `is_blocked`, `is_deleted` and `deleted_at`. Implementing it raised
three problems.

**Two of the five states have no representation.** Nothing in the attribute list
stores "archived", and `dormant` is explicitly described as derived rather than
stored. So the documented lifecycle names five states while the documented
attributes can express two.

**Two of the five have no distinguishing behaviour yet.** `discovered` differs
from `active` only in how the contact arrived, and nothing arrives until
synchronisation exists (Milestone 3). `dormant` is a function of `last_seen_at`
and a configured window, neither of which exists.

**`is_deleted` and `deleted_at` are two owners of one fact.** They can disagree,
and eventually will.

---

### Decision

**Three states, two nullable timestamps, one invariant.**

```
active ⇄ archived
  ↓  ↘     ↓
    deleted → (restored) → active
```

* `archived_at` -- the operator has put this contact out of the way. Excluded
  from the default listing, still returned by `get`, restorable.
* `deleted_at` -- the operator has removed this contact. Excluded from every
  listing and from `get` unless explicitly requested, restorable, and the target
  of the purge described in `PRIVACY.md` section 7.
* Both null -- active.

**At most one is ever set.** Deleting an archived contact clears `archived_at`,
so restoring returns it to active rather than silently back to the archive,
which is not what the operator asked for. The exclusion is enforced in the
entity and restated as a `CHECK` constraint.

**Timestamps rather than booleans**, because retention has to ask "deleted
before when" and a boolean cannot answer that. `is_deleted` is dropped: the
timestamp already carries the fact, and a derived `is_deleted` property reads it.

**One `restored` method for both states**, because both answer the same
question -- make this contact ordinary again -- and a caller forced to know
which state a contact is in before it can restore it has been handed the model's
problem.

`discovered` and `dormant` are **not implemented**. `discovered` is
indistinguishable from `active` until something discovers contacts;
`dormant` is derived and its inputs do not exist. A state that changes no
behaviour is a column that will be wrong.

`is_blocked` is **deferred**. It is genuinely distinct from archived -- archived
means "out of my way", blocked means "never process this person" -- but nothing
processes anybody until Milestone 8, so today the distinction has no observable
effect. It is one additive migration away and should be added with the code that
first honours it.

---

### Alternatives Considered

**A single `status` enum column.** One column, mutually exclusive by
construction, and a check constraint listing the values. Genuinely attractive,
and rejected only because it loses *when*: retention needs the deletion
timestamp, so the enum would need a companion `deleted_at` anyway, and then the
two can disagree -- the exact defect `is_deleted` had.

**Booleans plus timestamps** (`is_archived`, `archived_at`, ...). What the
document implies. Rejected as two owners of one fact.

**Hard deletion instead of soft.** Simpler, and no state to exclude from
queries. Rejected: removing a Contact must also remove every Memory, Proposal,
Goal, Relationship Profile, Style Profile and Suggestion that references it, and
none of those tables exist yet. A hard delete now would appear to work while
leaving orphans later. Milestone 11 owns the purge.

**Archive only, no deletion.** Would have been the smaller milestone. Rejected
because "remove this person" is a reasonable thing to want on day one, and
because the shared repository contract has had a soft-deletion branch since
Milestone 1.0 that no aggregate had ever exercised -- an untested contract clause
is a clause that is probably wrong.

---

### Consequences

Pros

- Three states, each with an immediate observable effect
- Mutual exclusion is structural, in the entity and in the schema
- Retention can ask its question directly
- The soft-deletion clause of the shared repository contract is finally executed,
  against both implementations

Cons

- Two nullable columns rather than one enum, so a reader must know that "both
  null" means active. The `status` property exists so no display code has to
  work that out.
- A soft-deleted contact keeps its `(account_id, telegram_user_id)`, so the same
  person cannot be re-added while a deleted row exists. That is intended -- see
  ADR-041 -- but it is a behaviour users will meet, so the message says to
  restore rather than reporting a conflict.
- Soft-deleted rows accumulate until Milestone 11 implements the purge.

---

### Future Considerations

`is_blocked`, `last_seen_at` and the derived `dormant` state all arrive with the
milestones that give them meaning. None requires changing what is decided here.

---

### Related Decisions

ADR-041 (Contact identity), ADR-037 (Account lifecycle -- the same argument, that
an entity should own only the lifecycle it genuinely has), ADR-039 (account
scoping). `DOMAIN_MODEL.md` section 5.4 is corrected accordingly.

---

# ADR-043

## Title

Cross-Table Account Ownership Is Enforced by Composite Foreign Keys

Status

Proposed

Date

2026-07-28

---

### Context

Until now every account-owned table referenced only ``accounts``. Chat is the
first to reference *another* account-owned table, and it exposed a gap that
every later table in the graph would inherit.

The obvious foreign key is ``chats.contact_id -> contacts.id``. It guarantees
that the contact exists. It does **not** guarantee that the contact belongs to
the same account as the chat, so this row satisfies every constraint:

```
chats:    id=1  account_id=1  contact_id=5
contacts: id=5  account_id=2
```

Account 1 now has a chat pointing at account 2's contact. Nothing rejects it.
Every later table -- messages, memories, goals, relationship profiles -- would
have the same hole, and the failure mode is the worst this project has: one
account's data linked to another's, silently, with no error and no obvious
symptom.

ADR-039 makes cross-account *reads* impossible by scoping repositories, and the
use case does check. But the check lives in application code, and the guarantee
this project keeps choosing is the structural one -- ``StaticPool`` rather than a
pool that happens to hold one connection, a partial unique index rather than
"remember to deactivate first".

There is a second, separate problem in the same place. ``DATABASE.md`` version
1.0 specified two things that cannot both hold:

```
FK:    contact_id -> contacts(id) ON DELETE SET NULL
Check: contact_id IS NOT NULL OR chat_type <> 'private'
```

Purging a contact nulls ``contact_id`` on their private chat, which violates the
check on the line below. ``PRIVACY.md`` section 7 also requires a contact purge
to be "transactional removal across every table", which ``SET NULL`` is not: it
leaves the chat, and later its messages, behind.

---

### Decision

**A table referencing another account-owned table uses a composite foreign key
on ``(account_id, <referenced_id>)``.**

```sql
FOREIGN KEY (account_id, contact_id)
    REFERENCES contacts (account_id, id) ON DELETE CASCADE
```

The pair must exist together, so a chat in one account cannot name a contact in
another. It requires a unique index on ``contacts (account_id, id)`` -- redundant
with the primary key on its own, and that redundancy is exactly what makes the
guarantee expressible.

**And that key cascades rather than setting null.** ``DATABASE.md`` is corrected.
Cascade is what ``PRIVACY.md`` section 7 already requires, and it is the only
option consistent with the private-chat invariant: a private chat with nobody in
it is a row the model forbids, so there is nothing sensible to leave behind.

The application check stays. It is not redundant: it produces a message naming
the problem ("that contact was not found") instead of a constraint violation
naming a column. The constraint is there so that a route which skips the check
cannot corrupt the graph.

---

### Alternatives Considered

**A simple foreign key plus the application check.** One column, no extra index,
and the scoped repository already makes the mistake hard. Rejected because
"hard" is not "impossible", and this is the failure this project's whole design
posture exists to prevent. A migration, a repair script, or one use case written
in a hurry is all it takes.

**A trigger asserting the accounts match.** Works, and needs no extra index.
Rejected: triggers are dialect-specific, invisible to a reader of the table
definition, and would have to be written again for PostgreSQL (ADR-016). A
foreign key is declarative and portable.

**Making ``account_id`` part of the contact's primary key.** Then the reference
is composite by construction. Rejected in ADR-041 for the same reason it is
rejected here: a composite *primary* key propagates into every table's keys and
joins, whereas a composite *foreign* key is local to the tables that need the
guarantee.

**Deriving the chat's account from its contact rather than storing it.** Removes
the possibility of disagreement entirely, and is genuinely elegant. Rejected
because non-private chats have no contact, so ``account_id`` would be nullable
for exactly the rows that need it most, and every scoped query would need a join
to reach the account it is scoping by.

---

### Consequences

Pros

- Cross-account linkage is impossible at the storage layer, not merely unlikely
- The rule is declarative, portable and visible in the table definition
- A contact purge removes their private chat, as ``PRIVACY.md`` already promised
- The pattern is uniform: every later table in the graph uses the same shape

Cons

- One redundant unique index per referenced table. On ``contacts`` that is one
  extra index over two integers, maintained on write.
- Referencing tables must carry ``account_id`` even where it could have been
  derived. They all carry it already, for scoping.
- The composite key propagates: ``messages`` will reference
  ``(account_id, chat_id)``. That is the intended outcome -- the guarantee
  travels with the graph -- but it is a cost paid per table.

---

### Future Considerations

Messages, memories, goals, relationship profiles and style profiles all
reference account-owned rows and should all use this shape. Each needs the
corresponding ``(account_id, id)`` index on the table it references, added in
the same migration that creates the referencing table.

---

### Related Decisions

ADR-039 (account scoping -- the same guarantee at the repository layer; this is
the storage layer's half), ADR-041 (Contact identity -- why the *primary* key is
not composite), ADR-016 (PostgreSQL portability), ADR-044 (Chat scope).
``DATABASE.md`` section 4.2 is corrected accordingly.

---

# ADR-044

## Title

The Communication Graph Is Established by Chat Alone

Status

Proposed

Date

2026-07-28

---

### Context

The goal was to make the system capable of representing communication between an
Account and its Contacts, independent of Telegram synchronisation, and to create
the minimum architecture supporting future synchronisation, memory, goal
tracking and message ingestion without implementing any of them.

Three documented entities could plausibly carry that: ``Chat``, ``Conversation``
and ``Message``.

---

### Decision

**Chat, and nothing else.**

Chat is the *edge*. Account and Contact are the graph's nodes, and until Chat
exists there is no structure joining them. Everything the goal names attaches
here or reaches a Contact through here:

| Future system | Attachment point |
|---|---|
| Telegram synchronisation | ``telegram_chat_id`` resolves it, ``sync_enabled`` gates it |
| Message ingestion | ``messages.chat_id``; messages have no other home |
| AI memory | anchored on Contact, reached through the chat that reaches them |
| Goal tracking | the same |
| Per-chat privacy | ``ai_processing_mode``, the gate every AI feature reads (ADR-024) |

**Conversation is excluded.** It is a segment of a chat bounded by message
timestamps. With no messages there is nothing to segment and its ``started_at``
would have no source, so it could only be a placeholder.

**Message is excluded.** It is content, not structure. It is also the largest
table in the system, needs full-text search, ingestion idempotency and
conversation segmentation -- none of which the goal asks for.

**SyncCursor is excluded.** Per-chat synchronisation bookmarks are sync state,
and belong with the code that advances them.

Chat carries ten fields: identity, ownership, the Telegram identifier, the kind,
the contact or the title, the two policy flags, and timestamps. Seven documented
attributes are deferred, each because nothing reads or writes it yet:
``last_message_at`` (written by ingestion), ``is_muted`` (nothing notifies),
``is_archived`` (a third way to hide something), ``retention_days`` (no global
policy to inherit until Milestone 10), ``deleted_at`` (see below).

**A Chat has no lifecycle of its own.** This is the one place where Chat
deliberately differs from Contact, which has archive and soft delete. A chat
exists because a conversation exists in Telegram; a user does not create or
remove one. What they control is whether it is synchronised and what may be done
with its content, and both are policy rather than lifecycle. Removal happens by
cascade -- from the account, or from the contact purge.

**Two constructors, not one.** ``Chat.private_with`` requires a contact and
refuses a title; ``Chat.group_titled`` requires a title and refuses a contact.
The impossible combinations are therefore unwritable rather than merely
rejected, and the invariant

```
(chat_type = 'private') = (contact_id IS NOT NULL)
(chat_type <> 'private') = (title IS NOT NULL)
```

is stated in both directions, in the entity and in the schema.

**All five chat kinds are modelled** although Milestone 1 supports only private
chats. Two reasons: ``PROJECT_SPEC.md`` section 12 says enabling group support
should be additive, and -- more immediately -- the private-chat invariants are
only meaningful if a non-private chat is representable.

---

### Alternatives Considered

**Chat and Conversation together.** Would have delivered a more complete-looking
graph. Rejected: a Conversation with no messages has no defined start, so its
behaviour would be invented now and rewritten in Milestone 3.

**Chat and Message together.** Tempting, since messages are what a communication
graph ultimately carries. Rejected on scope: ingestion idempotency, FTS,
attachment handling and segmentation are Milestone 3's work, and none of it is
needed to *represent* communication.

**A generic ``Relationship`` table instead of Chat.** Would model the
Account-Contact edge directly, without Telegram's container concept. Rejected:
it would have to be reconciled with real chats the moment synchronisation
arrives, and a private chat *is* the relationship's container in Telegram's
model. Inventing a parallel one would mean two representations of the same edge.

**Giving Chat an archive and soft delete for symmetry with Contact.** Rejected:
symmetry is not a requirement. Three overlapping ways to hide a conversation
(archive the contact, archive the chat, disable its sync) would leave users and
code guessing which one applies.

---

### Consequences

Pros

- The graph is complete enough for every named future system to attach to
- Nothing implemented is a placeholder; every field has a reader or a writer
- ``ai_processing_mode`` exists before any AI does, so the privacy gate is in
  place before there is anything to gate
- Group support is an application change, not a migration

Cons

- Until Milestone 3 a chat has no messages, so the listing sorts by
  ``created_at`` rather than by recency. Adding ``last_message_at`` and its index
  later is additive, but it changes the default order.
- A private chat whose contact has been *soft* deleted still appears in the chat
  listing. That follows from what soft deletion means -- the history remains --
  but it is a presentation question worth revisiting when a UI exists.

---

### Future Considerations

Milestone 3 adds ``Message``, ``Conversation``, ``SyncCursor`` and
``last_message_at``. Each attaches to this table without changing it, which is
the test of whether this decision was right.

---

### Related Decisions

ADR-043 (composite foreign keys), ADR-041 (Contact identity), ADR-024 (AI
processing modes), ADR-039 (account scoping). ``DOMAIN_MODEL.md`` section 5.5 is
corrected accordingly.

---

# ADR-045

## Title

Message Identity Is Local; the External Identifier Is Optional and Its Index Partial

Status

Proposed

Date

2026-07-28

---

### Context

The goal for this slice was a pipeline that accepts messages from **any** future
source -- the CLI, Telegram synchronisation, import tools, tests. The documented
invariant does not permit that:

> ``(account_id, chat_id, telegram_message_id)`` is unique -- the idempotency
> guarantee that makes re-synchronisation safe.

A unique constraint over those three columns requires every message to have a
``telegram_message_id``. Only one of the four named sources issues one. Taken
literally the invariant makes the pipeline Telegram-specific, which is the
opposite of the requirement.

The invariant is nonetheless the right idea, and the reason given for it is
correct: without it, a synchronisation retry or a backfill overlapping live
updates would store the same message twice. The question is how to keep the
guarantee for the source that has identifiers while accepting sources that do
not.

---

### Decision

**A locally generated ``MessageId`` is the primary key.
``telegram_message_id`` is nullable, and its unique index is partial.**

```sql
CREATE UNIQUE INDEX uq_messages_account_id_chat_id_telegram_message_id
    ON messages (account_id, chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
```

Three consequences follow, and all three are intended.

**A message carrying an external identifier is ingested once.** The pipeline
looks it up before writing and reports a repeat as *skipped* rather than raising
a conflict. An error would force every caller to wrap the ordinary case in a
try/except, and a backfill meeting live updates is the ordinary case.

**A message with no external identifier is stored every time it is offered.**
There is nothing to match it against. Two identical messages typed at a keyboard
are two messages, and that is correct rather than a gap.

**The index must be partial.** A non-partial unique index over a nullable column
behaves differently across engines, and under any reading it would either permit
one source-less message per chat or none. Every message the CLI ingests today
has no identifier, so a non-partial index would reject the second one.

The partiality is stated in both dialects, so PostgreSQL gets the same index
(ADR-016).

---

### Alternatives Considered

**Require ``telegram_message_id``, and have non-Telegram sources synthesise
one.** Keeps the documented invariant exactly. Rejected: a synthesised
identifier occupies Telegram's identifier space, so a later real message could
collide with a fabricated one, and nothing would detect it. It also makes every
source pretend to be Telegram, which is the coupling the goal asked to avoid.

**A generic ``(source, external_id)`` pair, unique per chat.** More honest about
multiple sources, and where this will probably end up. Rejected as speculative:
there is one source, so the ``source`` column would hold a single value on every
row, and the vocabulary of the enum would be invented rather than observed.
Moving to it later is a migration that adds one column and widens one index --
worth paying when there are two real sources to name.

**Deduplicate source-less messages by content hash.** Would make every source
idempotent. Rejected because it is wrong: a person who sends "ok" twice has sent
two messages, and a pipeline silently discarding the second would lose real
history. Idempotency is a property of an *identifier*, not of content.

**Make ``(chat_id, telegram_message_id)`` the primary key.** Rejected for the
reasons ADR-041 gives for Contact: a foreign system would own our identifiers,
child tables would carry a composite key, and messages with no identifier could
not exist at all.

---

### Consequences

Pros

- Any source can ingest, and the pipeline has no Telegram vocabulary beyond one
  optional field
- Re-synchronisation is safe for the source that needs it
- The guarantee is enforced by the database, not by the caller remembering
- ``Message.has_external_identity`` makes the distinction visible in the domain
  rather than implicit in a null check

Cons

- Idempotency is available only to sources that issue identifiers. An import
  tool run twice will duplicate its import. That is honest -- nothing else would
  be correct -- but it should be documented where import tools are written.
- Two messages can be indistinguishable in content and differ only by local
  identifier. Any future deduplication feature has to decide what to do about
  that; this decision deliberately does not.

---

### Future Considerations

When a second identifier-issuing source exists, replace the nullable column with
``(source, external_id)`` and widen the partial index. Nothing above changes.

---

### Related Decisions

ADR-041 (Contact identity -- the same argument for a local key), ADR-043
(composite ownership keys), ADR-046 (append-only messages), ADR-016 (PostgreSQL
portability). ``DOMAIN_MODEL.md`` section 5.6 is corrected accordingly.

---

# ADR-046

## Title

Messages Are Append-Only, and Nothing Deletes Them Yet

Status

Proposed

Date

2026-07-28

---

### Context

``DOMAIN_MODEL.md`` section 5.6 calls a Message "the immutable factual record
from which everything else is derived", and then lists ``edited_at``,
``is_deleted_remotely`` and ``deleted_at`` among its attributes, with the
qualification that messages are "immutable after ingest **except** for" those
fields.

Implementing it raised the question of what to build now. Every one of those
exceptions is written by code that does not exist: synchronisation detects
edits and remote deletions (Milestone 3), retention removes old messages
(Milestone 10), purge removes a contact's entirely (Milestone 11).

The goal for this slice also asked the pipeline to "support future retention
policies", which invites the reading that retention must be settled first.

---

### Decision

**Messages are append-only in this slice, and the interface says so.**
``MessageRepository`` has no ``update``, no ``delete`` and no ``soft_delete``;
the table has no ``updated_at`` and no ``deleted_at``. A test asserts the
absence on the port and on both implementations, so the guarantee cannot erode
by someone adding a convenient method.

``ingested_at`` is the creation time. There is no ``updated_at`` because there
is nothing for it to record.

**Retention is supported without being decided.** It needs three things from
this table and has all three: an age to measure (``sent_at``), an index to find
old rows by (``(account_id, chat_id, sent_at, id)``, which the history query
needs anyway), and a per-chat override (``chats.retention_days``, one additive
column). What retention does *not* need from this slice is a policy: how long to
keep messages changes a background job, not a schema.

**The soft-versus-hard deletion question is deliberately left open.** Adding
``deleted_at`` now would settle it a milestone before the code that has to
answer it, and would put a ``WHERE deleted_at IS NULL`` filter that nothing
writes into every history query on the largest table in the system. Whichever
way Milestone 10 decides, the change is additive.

**How an edit is represented is likewise left open.** A mutation and a
superseding row are both defensible, and the choice belongs with the
synchronisation code that first observes one. Providing an ``update`` method now
would choose mutation by default.

---

### Alternatives Considered

**Implement ``deleted_at`` now, for symmetry with Contact.** Rejected: Contact's
soft deletion preserves history until a purge, but a message *is* the history --
there is nothing behind it to preserve. The two aggregates are not analogous,
and symmetry is not a requirement.

**Implement hard deletion now.** Smaller than soft deletion and arguably the
right end state. Rejected because it has no caller, and because it interacts
with derived data that does not exist: a Memory citing a deleted message needs
its provenance handled, which is Milestone 5's decision.

**Provide ``update`` for the four documented exceptions.** Rejected: all four
are written by synchronisation, and a general update method is a general licence
to change a record described as immutable.

**Stop and decide retention before writing any code**, as the goal's conditional
invited. Rejected after analysis: nothing in the retention policy space changes
the message table, so stopping would have blocked the slice on a decision it
does not depend on. The decision that *did* block it was identity (ADR-045),
which is resolved there.

---

### Consequences

Pros

- "Immutable factual record" is a property of the code, not a sentence in a
  document
- No filter that nothing writes appears in the history query
- Milestones 3, 10 and 11 each make their own decision with their own code in
  front of them
- The absence of an update path is asserted by test, so it cannot be eroded
  quietly

Cons

- Milestone 3 cannot record an edit without either adding an update path or
  choosing supersession. That decision is deferred, not avoided.
- Messages accumulate with no way to remove them until Milestone 10. On a
  personal-scale archive that is acceptable for now; it would not be indefinitely.

---

### Future Considerations

Milestone 3 adds ``edited_at`` and ``is_deleted_remotely`` and decides the edit
representation. Milestone 10 adds retention and decides soft versus hard.
Milestone 11 adds purge. Each is additive to what is built here.

---

### Related Decisions

ADR-045 (message identity), ADR-042 (Contact lifecycle -- the aggregate this one
deliberately does *not* mirror), ADR-024 (AI processing modes -- what may be done
with the content this table now holds).

---

# ADR-047

## Title

TDLib Binary Acquisition, Verification and Distribution

Status

Accepted

Date

2026-07-28 (proposed), 2026-07-28 (accepted and implemented)

---

### Context

ADR-012 decided TDLib as the primary Telegram client and required, as its point
3, that native binary acquisition be resolved **during Milestone 0**. It was
not. The item has been carried in `ROADMAP.md` through M0.1, M0.2, M1.0, M1.1,
M1.2, M1.3, M1.4 and M1.5, and it is now the single prerequisite blocking every
Telegram slice.

ADR-012's own risk note states the reason it cannot be waved through:

> "an unverified `tdjson` binary has full access to the user's Telegram session."

`tdjson` is a shared library loaded into our process with `ctypes`. It sees the
session key, every message, and the network. Whatever supplies it is as trusted
as the application itself.

Four sources are available.

| Source | Provenance | Effort | Trust |
|---|---|---|---|
| Build from source per platform | Ours | High — CMake, OpenSSL, zlib, gperf; ~40 min per platform | Highest |
| Prebuilt from a Python wrapper on PyPI | Third party, often unsigned | Lowest | Lowest |
| Prebuilt from Telegram's own downloads | Telegram | Low | High, but coverage is partial and versions lag |
| System package (`apt`, `brew`, `vcpkg`) | Distribution | Low | Good, but versions vary widely between platforms |

The development machine has MSVC 2026 (toolset 14.51, `cl` 19.51) confirmed
present, so building on Windows is feasible today.

---

### Decision

**1. The application never loads `tdjson` without verifying it.**

A `TdjsonLoader` resolves a candidate library, computes its SHA-256, and
compares it against a **pinned manifest** committed to the repository, keyed by
platform and TDLib version. A mismatch or an absent entry is a startup failure,
not a warning. `tgassist doctor` reports which library was resolved, its
checksum, and whether it matched — so a contributor can see the state of this
before anything tries to log in.

**2. Resolution order is explicit and configurable**, highest precedence first:

1. `telegram.tdjson_path` in configuration — an operator override, still verified
2. `TGASSIST_TELEGRAM__TDJSON_PATH` in the environment — the same, for CI
3. A vendored library under `<data_dir>/tdlib/<version>/`
4. The platform loader (`ctypes.util.find_library`), for a system install

There is no automatic download. A binary that the application fetches for itself
is a binary whose provenance the user never saw.

**3. Distribution is staged, and the stages are honest about their trust.**

*Now (development):* the developer builds or obtains `tdjson` once, records its
checksum in the manifest via a documented `poe` task, and points configuration
at it. `DEVELOPMENT_WORKFLOW.md` gains the build recipe for Windows, Linux and
macOS.

*At packaging (Milestone 14):* the release CI builds `tdjson` from a **pinned
TDLib commit** for each target platform, publishes the artefacts with their
checksums, and the installer vendors them. At that point the manifest is
generated by the same CI that produced the binaries, so the chain from source to
loaded library is ours end to end.

**4. ADR-012 §4 is amended.** That point proposed implementing a Telethon
adapter "opportunistically… to validate the port". It is withdrawn as a planned
item. The port is validated by `FakeTelegramGateway`, which runs the same
contract suite and is needed regardless. Telethon is retained as a **replacement
path** if TDLib packaging proves impractical on a target platform — not as a
second adapter maintained alongside the first.

---

### Alternatives Considered

**Depend on a PyPI wrapper that bundles binaries** (`aiotdlib`, `python-telegram`
and similar). By far the least effort: `pip install` and the binary appears.
Rejected as the primary path for two reasons. The binaries are typically
unsigned and built by a single maintainer, so the trust question is answered by
"someone published a wheel" — for a library that holds the user's session. And
the wrappers wrap: they impose their own async model over an interface that is
five C functions, which is more coupling than the problem needs (§Risk 5 of
`TELEGRAM_ARCHITECTURE.md`).

**Require a system install and no vendoring.** Clean, and shifts the trust to
the distribution. Rejected for a desktop application: it makes "install
tgassist" mean "first install a C++ library", which no target user will do.

**Download and verify at first run.** Keeps the repository small. Rejected: it
puts a network fetch of executable code into the startup path, and the failure
mode when the host is unreachable is an application that cannot start.

**Switch to Telethon as primary.** Removes the entire problem — pure Python, no
binary, no manifest, no packaging risk. Genuinely tempting, and it is what a
smaller project should probably do. Rejected because ADR-001 and ADR-012 chose
TDLib for update-gap recovery, ordering and local caching (see
`TELEGRAM_ARCHITECTURE.md` §8.1), and reimplementing those correctly is a
larger and subtler task than building a binary. This decision should be
revisited only if slice 0 actually fails.

---

### Consequences

Pros

- Nothing loads unverified native code into a process holding the user's session
- The provenance chain is documented and, from Milestone 14, entirely ours
- Contributors can run the whole suite except the live smoke tests without the
  binary, because the fake and the replay fixtures do not need it
- The Telethon amendment removes a planned second protocol implementation whose
  only justification was validating a port the fake already validates

Cons

- A contributor who wants live tests must obtain a binary and record a checksum.
  That friction is the point.
- The manifest must be updated on every TDLib upgrade, and a stale one blocks
  startup. That is the correct failure direction, but it will be inconvenient at
  least once.
- Release CI gains a native build step per platform, which is the cost ADR-012
  accepted in advance.

---

### Risks

If the manifest is ever updated carelessly — a checksum recorded from whatever
happened to be on disk — the verification becomes theatre. The recording task
must therefore print the resolved path and require explicit confirmation, and
the manifest diff must be reviewed like any other security-relevant change.

---

---

### As Implemented

Slice 0 of `TELEGRAM_ARCHITECTURE.md`. Three refinements emerged from building
it, none of which changes the decision.

**The configured path and its environment variable are one candidate, not two.**
The proposal listed them as precedence steps 1 and 2. The configuration system
already layers environment over file, so `TGASSIST_TELEGRAM__TDJSON_PATH` *is*
how `telegram.tdjson_path` is set from the environment. Implementing them
separately would have been a second, divergent precedence mechanism.

**Verification precedes loading, and a failure stops the search.** A candidate
that exists but is not in the manifest is refused; the search does not continue
to the next location. Falling through would mean planting a library in a
high-precedence directory earns a silent retry rather than a refusal. Only an
*absent* candidate advances the search. Asserted by test.

**Version comes from `td_execute`, not from a client.** `getOption` for
`version` is answered synchronously, so the whole of this slice runs with no
thread, no client and no network — which is what keeps ADR-048's receive loop in
the next slice rather than smuggled into this one.

Four checks were added that the proposal did not name.

- **Entry points.** The library must export `td_create_client_id`, `td_send`,
  `td_receive` and `td_execute`. A build offering only the deprecated
  `td_json_client_*` interface is a real TDLib, and the wrong one.
- **Manifest cross-check.** When an entry records a version, it is compared with
  what the loaded library reports. A disagreement means the entry is stale or
  the file was swapped, and it is refused rather than trusted.
- **Architecture**, read from the binary's own headers before it is loaded. A
  32-bit library under a 64-bit interpreter otherwise fails with an `OSError`
  naming nothing useful; read first, it becomes "this is x86, we are amd64".
- **Runtime dependencies**, likewise read before loading. This one closes a hole
  in the original decision, described below.

#### The dependency check, and why it was missing

The proposal reasoned about *the file*. It did not reason about what the file
loads. The manifest checksums one artefact, so a `tdjson` that pulls in
`libcrypto`, `libssl` and `zlib1` at runtime has three unverified files inside
the trust boundary — and the digest says nothing about them.

The gap is invisible rather than noisy, which is what makes it dangerous.
CPython resolves a library path in full and adds that directory to the search
order (`LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR`), so a dynamically linked `tdjson`
with its OpenSSL beside it **loads and works perfectly**. Nothing fails. The
verification simply covers less than it appears to.

Two consequences:

1. Imports are read from the binary's headers and classified. System libraries
   are accepted; OpenSSL and zlib are rejected; anything unrecognised is
   rejected, because an allow-list that admits the unknown is not an allow-list.
   Rejection raises a `SecurityError`, not a configuration error.
2. The documented build links OpenSSL and zlib **statically**, so the artefact
   that is checksummed is the whole of what gets loaded.

Imports are parsed directly from PE and ELF headers rather than by shelling out
to `dumpbin` or `ldd`. Those need a toolchain present, differ per platform, and
cannot be tested without one; a handful of struct reads can be exercised with
synthetic binaries on any machine. Format coverage is uneven and reports itself:
PE fully, ELF architecture only, Mach-O not at all — each gap reported as *not
checked* rather than passed, so an unverified platform never reads as verified.

#### What was actually built and recorded

| Property | Value |
|---|---|
| TDLib commit | `022d60202e446ad1287b9fb68e687c8a0760788b` (version 1.8.66) |
| Compiler | MSVC 19.51.36248, Visual Studio 18 Enterprise, toolset 14.51.36231 |
| CMake / generator | 4.3.1 / Ninja |
| Build type | Release |
| C runtime | `MultiThreaded` (static) |
| Dependencies | vcpkg `x64-windows-static`: OpenSSL 3.6.3, zlib 1.3.2, both built from source |
| Platform | `windows-amd64` |

The build procedure is committed as `scripts/build-tdjson.bat` rather than
described in prose. An earlier prose version in `DEVELOPMENT_WORKFLOW.md` was
written without being run and was wrong in three places: it omitted the vcpkg
toolchain file, it assumed a Visual Studio CMake generator that the bundled
CMake does not offer, and it did not name `GPERF_EXECUTABLE`, which vcpkg
installs somewhere TDLib's `find_program` does not search. A script that
demonstrably produced the recorded binary is worth more than instructions that
might not.

TDLib's own logging is set to `telegram.log_verbosity` (default 0) immediately
after loading. TDLib defaults to verbosity 5 on standard error, which would put
library chatter into command output — the concern behind ADR-040 arriving by a
different route.

**The shipped manifest is empty.** That is the correct initial state: nothing is
trusted, and `tgassist tdlib verify` prints the entry to add once a binary has
been obtained and its provenance established. There is deliberately **no**
`allow_unverified` escape hatch — recording an entry is one command, and an
opt-out would become the documented path within a week.

---

### Related Decisions

ADR-001 (TDLib chosen), ADR-012 (gateway strategy; §3 finally resolved here, §4
amended), ADR-022 (local data encryption), ADR-048 (how the library is driven).

---

# ADR-048

## Title

The TDLib Update Loop Runs on a Dedicated Thread, Bridged to asyncio

Status

Accepted

Date

2026-07-28 (proposed), 2026-07-28 (accepted and implemented)

---

### Context

ADR-013 made the application asyncio-first and gave SQLite one dedicated worker
thread, because SQLite is synchronous and its connections are thread-affine. It
did not say how TDLib is driven, beyond "runs natively on the asyncio event
loop" — which is not quite possible.

TDLib's C interface is five functions. Two matter here:

* `td_send(client_id, request)` — thread-safe, returns immediately.
* `td_receive(timeout)` — **blocking**, and must be called from one thread only.
  It returns responses *and* updates, for *all* clients, tagged with
  `@client_id` and, for responses, the `@extra` the request carried.

Nothing about that fits an event loop directly. Calling `td_receive` on the loop
blocks every other coroutine for the timeout duration; calling it with a zero
timeout busy-waits.

---

### Decision

**One dedicated receive thread per process, mirroring the database executor.**

* `TdjsonClient` owns a thread named `tgassist-td` running a `td_receive` loop.
* Requests are sent directly from the event loop — `td_send` is thread-safe — and
  each carries a generated `@extra`. The client keeps a registry of `@extra` to
  `asyncio.Future`.
* The receive thread never touches application state. It hands every frame to
  the loop with `loop.call_soon_threadsafe`: a response resolves its future, an
  update is put on a bounded `asyncio.Queue`.
* **One thread serves every client.** TDLib multiplexes clients on a single
  receive loop, so a second account costs a client, not a thread.
* The update queue is **bounded**. When it fills, the receive thread blocks
  before its next `td_receive`, so TDLib buffers internally rather than the
  process growing an unbounded Python queue. Queue depth is a reported metric,
  because a full queue means ingestion is behind and that should be visible
  rather than absorbed.

This adds no new concurrency concept. It is the pattern `DatabaseExecutor`
already establishes — a blocking, thread-affine resource owned by one thread,
reached from the loop through a narrow interface — applied to the other such
resource in the system.

---

### Alternatives Considered

**Run `td_receive` in `loop.run_in_executor` repeatedly.** Superficially
simpler: no explicit thread. Rejected because it hides that the work is
thread-affine, and a default executor with several workers would call
`td_receive` from more than one thread, which is undefined behaviour. Making the
thread explicit makes the constraint visible.

**Use a TDLib Python wrapper that provides its own async interface.** Removes
this code entirely. Rejected in ADR-047: the wrapper imposes its own model over
five C functions, and this is the part that would be hardest to replace later.

**Poll `td_receive` with a zero timeout from a coroutine.** No thread at all.
Rejected: it burns a core, and the latency floor is whatever the poll interval
is.

**Unbounded update queue.** Never blocks the receive thread. Rejected: it
converts backpressure into memory growth, and the failure arrives as an
out-of-memory kill during a large first sync rather than as a visible stall.

---

### Consequences

Pros

- The blocking call is isolated where it belongs, with one owner
- Multi-account costs clients, not threads
- Backpressure is explicit and measurable
- The shutdown ordering has one place to live (`TELEGRAM_ARCHITECTURE.md` §7.2)

Cons

- Three threads now carry meaning in the process (loop, `tgassist-db`,
  `tgassist-td`), and a future contributor must understand which owns what.
  `ARCHITECTURE.md` gains a table.
- A hung `td_receive` would block shutdown; the join therefore has a timeout and
  logs rather than waits forever.

---

### As Implemented

`TdjsonClient` in `infrastructure/telegram/client.py`. The decision stands
unchanged; three things it did not specify had to be settled.

**End of stream is an event, not a sentinel on the queue.** The obvious design
places a sentinel value on the update queue at shutdown. It cannot work: the
queue is bounded, and a stalled consumer is precisely what leaves it *full*, so
the sentinel would be blocked by the condition it exists to report. `receive()`
returns `None` instead, driven by an `asyncio.Event` that no amount of queued
data can delay. Anything already queued is still drained first -- shutdown does
not discard what was already received.

**Backpressure is `run_coroutine_threadsafe`, polled.** The receive thread
cannot `await` a full queue, so it submits the put to the loop and waits on the
resulting concurrent future in short slices, rechecking the stop flag between
them. Without the polling, a client stopped while its queue was full would hang
until a consumer that is never coming drained it.

**Restart is not supported**, and the state machine has no edge for it. A closed
client's TDLib identifier is dead, and reusing the object would mean tracking
which generation each pending future belonged to. Nothing needs it.

Shutdown is deterministic and idempotent. `close()` returns only once the thread
has stopped, every pending request has been failed with `TdlibNotRunningError`,
and every waiting `receive()` has been released. A thread that ignores the stop
request raises `TdlibShutdownTimeoutError` **after** the waiters are released --
a hung thread must not also hang the application, but it is a defect and is
reported as one.

A dying receive thread is not silent: the client moves to `FAILED`, the reason
is on `health()`, pending requests are failed and waiting receivers are
released. `FAILED` survives `close()`, because "never started" and "died" need
different responses.

Malformed frames are counted, not raised. One frame that is not a JSON object
must not cost every update queued behind it, and `health().malformed_frames`
makes a pattern of them visible.

Only `@type` is ever logged. A TDLib frame can carry an authorization code, a
session key or message text (`SECURITY.md` section 9), so no frame body reaches
a log.

**The single-caller constraint is asserted by test**, not left to review:
`td_receive` is reachable from exactly one file, and within it from exactly one
method. Two threads calling it is undefined behaviour rather than an error, so
a violation would be silent.

Verified against the real library: `getOption` round-trips through
`td_send`, the receive thread and the correlation registry, and the client shuts
down cleanly. That test skips where no verified binary is recorded.

---

### Related Decisions

ADR-013 (concurrency model — this extends it), ADR-034 (single connection),
ADR-047 (how the library is obtained).

---

# ADR-049

## Title

Session Models Authorization and Connection as Separate Axes

Status

Accepted and implemented

Date

2026-07-28

---

### Context

`DOMAIN_MODEL.md` §5.3 gives Session a single state machine:

```
disconnected → connecting → awaiting_phone → awaiting_code
             → awaiting_password (2FA) → ready
             → reconnecting → disconnected | ready → logged_out
```

with the invariant "only the `ready` state permits sending".

TDLib reports **two** states, and they vary independently. `authorizationState`
answers "do we have valid credentials"; `connectionState` answers "is the socket
up". A single enum cannot express *authorized but currently reconnecting* —
which is the ordinary condition after any network interruption.

Under the documented model a reconnect must overwrite `ready` with
`reconnecting`, discarding the fact that the account is authorized; when the
connection returns, the code has to *infer* that authorization survived. That
inference is the kind of thing that works until a login expires during a
reconnect.

ADR-037 already moved authentication state out of Account and into Session for
exactly this class of reason: two owners of one fact eventually disagree. This
is the same argument applied one level down — one field is being asked to own
two facts.

---

### Decision

**Session carries two independent states.**

```python
class AuthorizationState(StrEnum):
    UNAUTHORIZED, WAITING_PHONE, WAITING_CODE, WAITING_PASSWORD, READY, LOGGED_OUT

class ConnectionState(StrEnum):
    OFFLINE, CONNECTING, UPDATING, READY, WAITING_FOR_NETWORK
```

Each mirrors what TDLib reports, so the adapter translates rather than infers.

**The send invariant becomes a derived property over both:**

```python
@property
def can_send(self) -> bool:
    return (
        self.authorization_state is AuthorizationState.READY
        and self.connection_state is ConnectionState.READY
    )
```

"Only the `ready` state permits sending" was ambiguous — ready in which sense?
`can_send` answers it once, so no call site decides for itself.

**Identity is the account.** One session per account, so `account_id` is the
primary key rather than a surrogate beside it — the same reasoning ADR-038
applied to `UserProfile`, for the same reason.

`DOMAIN_MODEL.md` §5.3 is corrected accordingly.

---

### Alternatives Considered

**Keep one enum and add the missing combinations** (`ready_reconnecting`,
`waiting_code_offline`, …). Preserves a single field. Rejected: the states
multiply as a product of the two axes, and most combinations are unreachable, so
the enum would document possibilities that cannot occur while still being
awkward to reason about.

**Model connection state only in memory, persisting authorization.** Connection
state is genuinely transient, and this is close to right. Rejected because
`tgassist sync status` and the UI both need to report why nothing is happening
after a restart, and "we are offline" is the answer. It is cheap to persist and
expensive to reconstruct.

**Derive both from TDLib on demand.** No stored state at all, no possibility of
disagreement. Rejected: it makes every status query require a live client, so
the application could not tell the user anything about an account it has not
connected yet.

---

### Consequences

Pros

- *Authorized but reconnecting* is representable, which is the common case
- The adapter translates two TDLib states into two fields rather than collapsing
  them and re-deriving
- `can_send` states the rule once
- ADR-037's separation is carried through consistently

Cons

- Two fields where the document promised one, so `DOMAIN_MODEL.md` and the
  `telegram_sessions` schema both change before either is implemented — which is
  the cheapest possible moment.
- A reader must now know that "ready" is qualified. `can_send` is the mitigation.

---

### As Implemented

`domain/model/session.py`, `telegram_sessions` (migration `0007`) and
`SqlSessionRepository`. The decision stands; implementation settled three things
it left open.

**`connected` begins at `updating`, not at `ready`.** TDLib's sequence is
`connecting` -> `updating` -> `ready`, and the socket is up from `updating`
onwards -- that state means *connected and catching up*. Dating a connection from
`ready` would time it from the moment its backlog finished draining, which after
a week offline is a long way from when it connected. `is_connected` and the
`connected_at` stamp therefore both use `CONNECTED_STATES = {UPDATING, READY}`,
and moving between two connected states keeps the original stamp.

**`can_send` is deliberately stricter than `is_connected`.** It still requires
`connection_state is READY`. A session that is `updating` has a connection but
has not finished replaying what it missed, so it may not know that the
conversation it is about to reply to has moved on. Suggesting a reply into a
stale view of a chat is the mistake this application exists to avoid, and
waiting costs seconds.

**The unconnected/timestamp rule is an invariant in both places.** A session
that is not connected cannot carry `connected_at`, checked by the entity and
restated as a table `CHECK`, so a row written by a repair script or a future
migration cannot violate it either.

Two tests assert that every member of each enumeration is storable, because the
enums and the `CHECK` constraints would otherwise drift apart silently -- a state
the entity accepts but the table refuses would fail only when a real user
reached it.

`encryption_key_ref` holds a `SecretStore` *name*; the key is generated with
`secrets.token_urlsafe` and written to the credential store. Deliberately not
behind an injectable port: a seam there would let anything substitute a
predictable generator for the one protecting every message the user has sent.

---

### Related Decisions

ADR-037 (Account lifecycle separated from Session), ADR-038 (identity is the
account), ADR-012 (gateway strategy), ADR-021 (the key is a name, not a value).
Corrects `DOMAIN_MODEL.md` §5.3.

---

# ADR-050

## Title

Synchronisation Cursors, Batch Boundaries and Batched Event Publication

Status

Proposed

Date

2026-07-28

---

### Context

Three constraints already in force meet for the first time during a history
backfill.

**ADR-034**: one database connection, one transaction at a time. Every
transaction the sync engine holds is latency for every reader.

**ADR-031**: `EventBus.publish` is synchronous — it returns only after every
handler has run.

**`PROJECT_SPEC.md` §4.1**: the default per-chat cap is 50 000 messages and the
default horizon is 365 days.

Published naively, one event per ingested message, a first sync would run every
subscriber 50 000 times inside the sync loop, and the backfill would proceed at
the speed of the slowest handler. Written naively, in one transaction, a
backfill would hold the single connection for its entire duration and the
application would appear frozen.

Separately, `DOMAIN_MODEL.md` §5.22 specifies `SyncCursor` without saying *when*
it is written, and that timing is the entire resumability mechanism.

---

### Decision

**1. The cursor advances in the same transaction as the messages it accounts
for.** A committed batch is accounted for; an uncommitted one never happened.
Interruption at any point therefore leaves a contiguous stored range and a
cursor that agrees with it, with no reconciliation pass and no repair logic.

**2. One transaction per fetched history page** — default 100 messages. Between
batches the engine yields, so live updates and reads interleave. 50 000 messages
is roughly 500 short transactions rather than one long one.

**3. One event per committed batch, not per message.**

```python
@dataclass(frozen=True, slots=True)
class MessagesIngested(DomainEvent):
    account_id: int
    chat_id: int
    count: int
    newest_sent_at: datetime
```

A live update is the degenerate case with `count=1`, so subscribers have one
shape to handle rather than two. ADR-031 is unchanged — synchronous delivery is
still right — it simply stops being applied at a granularity it was never meant
for.

**4. Backfill and live updates share one ingestion serialiser.** Both write, and
ADR-034 permits one transaction at a time. Rather than let them contend on the
unit-of-work lock, both flow through a single task consuming one queue.
Serialisation becomes an explicit, orderable design element instead of an
emergent property of lock contention.

**5. Our cursors are for our resumability, not for MTProto gap recovery.** TDLib
owns update-gap detection, reordering and deduplication — that is why ADR-001
chose it. A design that re-derives gaps from message identifiers would duplicate
subtle work TDLib already does correctly, and would be large.

**6. Two fields are dropped from `DOMAIN_MODEL.md` §5.22.** `last_error` — an
error string on a row is a log entry in the wrong place, and it is
`consecutive_failures` that drives behaviour. `backfill_target_date` is renamed
`backfill_horizon`, matching the configuration key.

---

### Alternatives Considered

**One transaction for the whole backfill.** Atomic, and the cursor would not
need to be written until the end. Rejected: it holds the single connection for
minutes to hours, and an interruption discards everything.

**One transaction per message.** Maximum interleaving. Rejected: 50 000
transactions each with their own commit and fsync, for no gain over 500.

**Make event delivery asynchronous for bulk ingest.** Would let per-message
events stay. Rejected: it reverses ADR-031 for one caller, and ADR-031 exists
because fire-and-forget delivery made failures invisible. Changing the
granularity keeps the guarantee.

**Publish nothing during backfill, one event at the end.** Fewest events.
Rejected: progress reporting is a stated requirement (`PROJECT_SPEC.md` §4.1
"first sync — with progress, cancellable"), and a subscriber that wants
per-batch progress has nowhere to get it.

**Let backfill and live updates write concurrently and rely on the lock.**
Less code. Rejected: the ordering would be whatever the lock happened to grant,
and a stall would surface as an unexplained pause rather than a queue depth.

---

### Consequences

Pros

- Resumability needs no repair logic — it is a property of the transaction
  boundary
- The single-connection constraint is respected by design rather than discovered
  under load
- ADR-031 holds unchanged
- Progress is observable per batch
- Write ordering between the two producers is explicit

Cons

- A subscriber wanting per-message granularity must read the messages itself.
  None does.
- The batch size is a tuning parameter, and 100 is an estimate until measured.
  It is configuration, and `TELEGRAM_ARCHITECTURE.md` §15 lists the measurement
  as a risk rather than assuming the number is right.
- The ingestion serialiser is one more long-lived task to supervise and shut
  down in order.

---

### As Implemented

Slice 6 built the backfill. Decisions 1, 2, 5 and 6 held exactly as written; the
cursor moves in the same transaction as its batch, one transaction per page,
TDLib keeps gap recovery, and `last_error` stayed dropped.

**Decision 3 is implemented as of slice 7.** `MessagesIngested` is published
once per committed batch, from both the backfill and live synchronisation, with
one field this decision did not name: `source`, which says whether a batch came
from `backfill`, `catch_up` or `live`. A subscriber that treated fifty thousand
back-filled rows like one arriving message would do fifty thousand times the
work it meant to, and `count` alone does not distinguish them.

**Decision 4's ingestion serialiser is satisfied without a queue.** Slice 7
found that the two producers never run concurrently: a live run catches up and
*then* drains, in one task, and a backfill is a separate command. A single
consumer task already serialises every write, and a queue with workers in front
of one connection (ADR-034) would be the same guarantee with more moving parts.
The decision stands; the machinery it anticipated turned out not to be needed.
If a second concurrent producer ever appears, this is where to add it.

What the implementation *did* need, and this decision did not say, is recorded
as ADR-054: what the cursor's key is, what it stores, and what "complete" means
when a run stops at a configured horizon rather than at the beginning of a chat.

---

### Related Decisions

ADR-031 (synchronous event delivery), ADR-034 (single connection), ADR-045
(ingestion idempotency — what makes an overlapping batch a no-op), ADR-046
(append-only messages), ADR-001 (TDLib, and therefore who owns gap recovery),
ADR-054 (the cursor's identity, value and terminating conditions).
Corrects `DOMAIN_MODEL.md` §5.22.

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

# ADR-051

## Title

Authorization Is Driven by a Dispatch Loop over a Single Update Stream

Status

Proposed

Date

2026-07-28

---

### Context

TDLib's login is **update-driven**. No request returns "you are logged in":
the client emits `updateAuthorizationState`, the application answers with the
matching request, and the next state arrives as another update. `TdjsonClient`
(ADR-048) exposes exactly two things that touch this — `request()`, correlated
by `@extra`, and `receive()`, a single-consumer queue of everything else.

Something has to turn that stream into a sequence a caller can `await`, and the
design must answer three questions the earlier ADRs left open:

1. **Who consumes `client.receive()`?** The queue holds one item per update, so
   a second consumer would not duplicate the stream — it would *split* it, and
   each consumer would silently miss whatever the other took first.
2. **Where do submissions run?** A `checkAuthenticationCode` awaits a reply.
3. **How much of `TelegramGateway` exists now?** `API.md` section 10.1 and
   `TELEGRAM_ARCHITECTURE.md` section 5.1 both specify a port with reading,
   updates and sending on it, none of which this slice has a caller for.

---

### Decision

**One dispatch loop, owned by the gateway, is the only consumer of
`client.receive()`.** It routes `updateAuthorizationState` and
`updateConnectionState` into a small mutable view, wakes anything waiting, and
counts everything else. An architectural test asserts the single-consumer
constraint, exactly as ADR-048's single-caller constraint is asserted.

**Submissions run on the caller's task, never inside dispatch.** A submission
that awaited its reply inside the dispatch loop would stall every other update
behind it — including the state change it was waiting for. So
`start_authorization` reads the view, asks the handler, calls
`client.request()` itself, and then waits for the view to move.

**The waiter compares the raw TDLib state, not the domain one.** Two TDLib
states collapse to `UNAUTHORIZED`, and a wait that could not tell
`waitTdlibParameters` from `waitEncryptionKey` would return before anything
happened.

**`TelegramGateway` is declared one slice at a time.** This slice declares
lifecycle, authorization and `get_me`. Reading, updates and sending arrive with
the code that calls them. A protocol listing methods no caller uses cannot be
verified by a contract suite, and a fake would have to invent behaviour for
them.

**Unhandled updates are counted, not discarded silently.** `unhandled_updates`
grows during a login, which is honest; slice 4 consumes them.

---

### Alternatives Considered

**Expose `updates()` now and let the use case drive login.** Puts the state
machine in the application layer, where it can be tested without a gateway.
Rejected: it makes every consumer of the gateway responsible for knowing TDLib's
protocol, which is the one thing the port exists to prevent. It also forces
`updates()` to exist before anything reads it.

**Drive the flow from `request()` alone, treating each reply as the next
state.** Simpler, and it removes the dispatch loop entirely. Rejected because it
is not how TDLib works: `checkAuthenticationCode` answers `ok`, and the state
that matters arrives separately. A design that inferred the state from the reply
would be right until two-factor authentication, where `ok` means *two* different
things.

**A per-request future keyed on the expected next state.** Precise, and it
removes the polling in `_await_change_from`. Rejected: Telegram can move to a
state nobody asked for — a revocation mid-login — and a design that only waits
for expected states cannot see it.

**Declare the whole port now, raising `NotImplementedError` for the rest.**
Matches the documents. Rejected outright: that is placeholder code, and a fake
implementing it would be inventing behaviour to satisfy a signature.

---

### Consequences

Pros

- The application layer never sees a TDLib type or a TDLib state name
- The single-consumer rule is asserted rather than trusted, like ADR-048's
- Login, retry and two-factor are testable with no network and no account: the
  contract suite runs the real adapter against a TDLib that runs the real state
  machine
- The port grows with its consumers, so every method has a contract test

Cons

- `TelegramGateway` in `API.md` and `TELEGRAM_ARCHITECTURE.md` is larger than
  the one that exists. Both now mark which methods are implemented; a reader
  must check.
- The gateway owns a task as well as a client, so `disconnect()` has two things
  to release and a half-started `connect()` must undo both. It does.
- A dispatch loop that dies would leave every waiter hanging, so it is written
  never to raise.

---

### Future Considerations

Slice 4 adds `updates()`. It must be fed *from the dispatch loop* rather than
from a second `client.receive()` consumer, or the stream splits — which is why
the constraint is asserted by test now, before there is a second candidate.

`RetryDecision` currently has two members. A `RETRY_AFTER` for flood waits is
the obvious third, and `errors.is_flood_wait` already recognises the condition;
it waits for the sync engine, which is what will actually cause one.

---

### As Implemented

Slice 3 built the dispatch loop and declared lifecycle, authorization and
`get_me`. Slice 4 extended the port with `list_chats`, `get_chat` and
`fetch_history` — the growth this decision anticipated, and the first test of
whether growing a protocol one slice at a time is workable.

It was: the contract suite gained a fixture and twenty-odd obligations, both
implementations grew together, and nothing had to be un-invented. `get_contact`
is still absent because nothing calls it, which is the rule doing its job rather
than an oversight.

Slice 5 then added `get_contact` and `list_contacts` — the callers arrived, so
the methods did. `updates()` and `send_message` remain absent for the same
reason `get_contact` was.

The single-consumer constraint has held. Reads go through `client.request()`,
correlated by `@extra`, so they never touch the update stream and the dispatch
loop remains the only consumer of `client.receive()`.

---

### Related Decisions

ADR-048 (the receive thread this consumes from), ADR-049 (the two state axes it
reports), ADR-012 (gateway strategy), ADR-021 (credentials by name), ADR-023
(no typing indicators — enforced structurally by the port's absence of one).

---

# ADR-052

## Title

The Operator's Telegram Identity Is the Account's, and It Is Enforced in a Domain Service

Status

Proposed

Date

2026-07-30

---

### Context

`DOMAIN_MODEL.md` section 5.4 states an invariant it could not enforce:

> "A Contact cannot be its own Account's operator identity."

`ROADMAP.md` recorded it as unenforced because nothing knew the operator's own
Telegram identifier. `TELEGRAM_ARCHITECTURE.md` section 2.6 assigned enforcement
to the contact-synchronisation slice, on the reasoning that authentication would
supply the identifier through `getMe`.

Implementation found that reasoning half right. The identifier is available —
but it has been available since Milestone 1.2, on `Account.telegram_user_id`,
which is a required column set when the account is created and verified at every
login by `AuthenticateAccount`. Nothing had to be obtained, and no field had to
be added. The invariant was unenforced because nobody had written the check.

Two questions remained genuinely open.

**Where does the rule live?** It spans two aggregates: it needs an Account to
state anything about a Contact. A `Contact` knows its `account_id` but not the
operator's Telegram identifier, and giving it one would store the same fact on
every contact row. The database cannot express it either — SQLite's `CHECK`
cannot reference another table, so the alternative would be a trigger.

**What happens to Saved Messages?** Telegram's chat with oneself arrives as
`chatTypePrivate` whose `user_id` is the operator. Every real account has one.
Synchronisation that treated it as an ordinary private chat would try to create
the forbidden contact on its very first run, against every account.

---

### Decision

**The operator's Telegram identity is `Account.telegram_user_id`.** No new
field, no new source, no new lookup. `Account.is_operator(telegram_user_id)`
names the question so that no caller compares columns for itself.

**The invariant is enforced by a domain service**, `require_not_operator`, in
`domain/services/operator_identity.py`. Every write path that can create a
contact calls it: `CreateContact` and both synchronisation use cases.

**Saved Messages is stored as `ChatType.SAVED`.** The domain already had the
type. Synchronisation recognises the operator on the far side of a private chat
and stores the chat with Telegram's own title, creating no contact. The decision
is made where the account is known, not in the pure TDLib mapper, which has no
opinion about who is running it.

**Enforcement is on write only.** A database written before this slice may hold
a contact that is its own operator; nothing scans for one, and nothing refuses
to open such a database. Retrofitting a check onto read paths would make
existing installations fail to list contacts, which is a worse outcome than the
row being there.

---

### Alternatives Considered

**A `CHECK` constraint.** Cannot reference another table in SQLite.

**A trigger.** Would work, and would be a second home for a rule the application
already has to state — the two would eventually disagree, and the trigger's
message would name a column rather than the problem.

**A field on `Contact`, such as `is_operator`.** Storing a derived fact, on
every row, that only one row could ever have. It would also be settable, which
makes the invariant a value rather than a rule.

**Skipping the Saved Messages chat entirely.** Smaller, and it loses data the
operator can see in Telegram. `ChatType.SAVED` exists precisely so this chat is
representable, and skipping it would make that enum member dead.

**Mapping Saved Messages in `mapping.py`.** The mapper is a pure function over
TDLib JSON and has no account. Passing one in would make every mapping call
account-relative to serve one case.

---

### Consequences

**Positive**

- An invariant that has been documented-but-unenforced since Milestone 1.3 is
  now enforced, on every write path, with one implementation.
- The first synchronisation run against a real account works, rather than
  failing on Saved Messages.
- `Account.is_operator` gives later slices — message ingestion deciding whether
  a sender is the operator — a question to ask rather than a comparison to
  repeat.

**Negative**

- The rule is application-enforced rather than structural. A future write path
  that bypasses `require_not_operator` would bypass the invariant. The
  mitigation is that there are two write paths, both call it, and both are
  tested.
- Existing rows are not checked. A contact created before this slice that is the
  operator stays.

---

### Related Decisions

ADR-041 (contact identity is local; `telegram_user_id` is external), ADR-043
(cross-account ownership is structurally impossible), ADR-044 (the chat is the
only edge), ADR-053 (what synchronisation may do with what it finds).

---

# ADR-053

## Title

Synchronisation Is Additive, Per-Item Transactional, and Never Overrules the Operator

Status

Proposed

Date

2026-07-30

---

### Context

Slice 5 is the first code that reads Telegram and writes the database, so it is
the first place the two models have to be reconciled. Four questions had to be
answered before any of it could be written, and each one has a wrong answer that
looks reasonable.

1. **What does Telegram own?** Telegram is authoritative about names, handles
   and titles. It is not authoritative about `sync_enabled`, or
   `ai_processing_mode`, or whether the operator archived somebody.
2. **What happens to what Telegram no longer lists?** A chat the operator left
   is still their history.
3. **Where is the transaction boundary?** ADR-034 permits one transaction at a
   time for the whole application, so a run-long transaction is a latency budget
   for everything else — and the goal states that no run may leave half-written
   results.
4. **What ends a run, and what does not?** Telegram being unreachable and one
   chat being undescribable are not the same failure.

---

### Decision

**Synchronisation is additive.** It creates and updates. It never deletes, and
it never soft-deletes. A record Telegram no longer mentions is left alone.

**It never overwrites an operator's decision.** `sync_enabled` and
`ai_processing_mode` are chosen when a chat is first discovered and never
revisited; a contact the operator deleted is not resurrected, and its fields are
not refreshed. A run that silently re-enabled AI processing on a chat somebody
had disabled it on would be a privacy defect, not a bug.

**It writes only what Telegram owns**: display names, handles, and the titles of
chats that have one. A private chat's name belongs to its contact, so a private
chat row is never rewritten by synchronisation at all.

**A repeat run over unchanged data writes nothing.** The entities already return
`self` from a transition that changes nothing, so `updated_at` continues to mean
"when this last changed" rather than "when we last looked".

**One transaction per item, not per run.** For a private chat the item is the
pair `(Contact, Chat)`, because ADR-043's composite key means a private chat
cannot exist without the contact it names. Each unit commits whole or not at
all, so an interrupted run leaves complete records and no partial ones.

**A transport failure ends the run; an item failure does not.** If Telegram or
the database is unreachable, the next item meets the same wall, and a report
listing two hundred identical failures helps nobody. A `DomainValidationError`,
`ConflictError` or `ConstraintViolationError` on one item is recorded and the
run continues — the same judgement the adapter already makes about a chat that
vanishes mid-listing.

**Every problem is reported.** `SyncReport` carries counts and a list of
`SyncProblem`, and a problem does not always cost an item: a handle this
application cannot store leaves the person recorded without one. Nothing is
dropped quietly, and no problem carries a name or any message content
(`SECURITY.md` section 9).

**Which chats are synchronised is configuration**, `telegram.sync_chat_types`,
defaulting to private only. Every kind of chat is still *recorded*, so the
operator can see it and switch synchronisation on; the setting decides only the
initial `sync_enabled`.

**Chats and contacts are two use cases, not one.** The chat list and the address
book are different populations — one holds people never saved, the other holds
people never messaged — and neither can be derived from the other.

---

### Alternatives Considered

**One transaction per run.** Atomic, and it holds the application's only
transaction across a network conversation with Telegram. It also makes one
undescribable chat cost the entire run.

**One transaction per record.** Would write a contact and leave its chat
unwritten if the second failed, which is the half-written result the pair
boundary exists to prevent.

**Deleting local records Telegram no longer lists.** Would make leaving a group
destroy its history, and would make a transient Telegram error indistinguishable
from a deletion.

**Refreshing a soft-deleted contact's fields without restoring them.** Half a
rule. If the operator deleted somebody, keeping their name current is work done
on the operator's behalf that they asked not to have done.

**Failing the whole run on the first bad item.** Rejected for the same reason
slice 4 skips a chat that vanishes mid-listing: one bad chat must not cost the
operator the other two hundred.

**A generic `SyncEngine`.** Explicitly out of scope. Two use cases that share
one contact-upsert function is the whole of what is shared; an engine would be
an abstraction over two cases.

---

### Consequences

**Positive**

- Running `tgassist sync chats` twice is safe, and provably so: the second run
  reports everything unchanged and commits nothing.
- An interrupted run is resumable by re-running it, with no cursor and no extra
  machinery.
- The privacy settings a user chooses per chat survive every subsequent run.

**Negative**

- Local records can drift from Telegram in one direction: something deleted
  there stays here. That is deliberate, and it means "what is in this database"
  is not a question Telegram can answer.
- N+1 lookups. A run resolves each chat's counterpart individually. TDLib serves
  these from its local database, so the cost is real but small; if it stops
  being small, the fix is a batch call rather than a different design.
- The report is counts, not entities. A caller wanting to know *which* chat was
  created must read the database.

---

### Related Decisions

ADR-034 (one connection, one transaction at a time), ADR-041 (local identity),
ADR-042 (soft deletion is a timestamp), ADR-043 (cross-account ownership),
ADR-045 (idempotent message writes, which this mirrors for chats and contacts),
ADR-052 (the operator is never a contact), ADR-051 (the port grows one slice at
a time — this slice added `get_contact` and `list_contacts`).


---

# ADR-054

## Title

A Sync Cursor Is Keyed by Its Chat, Stores a Message Identifier, and Records the Horizon It Reached

Status

Proposed

Date

2026-07-30

---

### Context

ADR-050 decided *when* a cursor is written — in the same transaction as the
messages it accounts for — and that one page is one transaction. Implementing
the backfill in slice 6 found three questions it did not answer, each with a
plausible wrong answer.

**What is the key?** `DATABASE.md`'s diagram gives `sync_cursors` a surrogate
`id` with a unique `chat_id` beside it. `DOMAIN_MODEL.md` §5.22 lists both `id`
and `chat_id` among its attributes and states the invariant "exactly one Cursor
per Chat" separately.

**What does the cursor store?** §5.22 names `oldest_synced_message_id` without
saying why a message identifier rather than a timestamp, and the choice is not
obvious: a timestamp is what a *user* would think in, and a horizon is expressed
in days.

**What does "complete" mean?** §8.3 gives three terminating conditions — the
beginning of the chat, the horizon, the per-chat cap — and `backfill_complete`
is a single boolean. A run that stopped at a 365-day horizon and a run that
reached the beginning of a chat both set it, and a later run configured to reach
further back cannot tell them apart.

---

### Decision

**1. The chat identifier is the primary key.** There is exactly one cursor per
chat, so a surrogate would be a second name for one row — the reasoning ADR-038
applied to `user_profiles` and migration `0007` applied to `telegram_sessions`.
`account_id` is carried beside it, not as part of the key but so the foreign key
can be **composite**: `(account_id, chat_id) → chats(account_id, id)`, which is
what makes a cursor for one account's chat unattachable to another's (ADR-043).

This supersedes the shape in `DATABASE.md`'s diagram.

**2. The cursor value is a Telegram message identifier**, not a timestamp and
not an opaque token. Three properties decide it:

- Telegram pages history **by message identifier**. A timestamp cursor would
  need converting on every resume, through a call that can land on a different
  message when two share a second.
- Message identifiers are unique and totally ordered **within a chat**, which is
  exactly one cursor's scope. Timestamps are neither, so a timestamp cursor
  either re-reads or skips at every boundary depending on whether the comparison
  is inclusive.
- The identifier is ours to hold. An opaque continuation token would make
  resumability depend on that token surviving a restart and a library upgrade,
  neither of which Telegram promises. `getChatHistory` has no such token anyway.

**3. Both ends of the range are stored, and either both are set or neither is.**
`newest_synced_message_id` is established by the first batch and never lowered.
It is not read by this slice, and it is stored anyway because it records a
**historical fact that cannot be recomputed**: what the top of the stored range
was when we synced. Deferring it would mean a later migration plus a value only
Telegram could supply, and only if the chat had not moved on. A check constraint
refuses a cursor with one end and not the other, because a floor with no ceiling
describes a range whose extent nobody can state.

**4. `backfill_complete` is stored beside `backfill_horizon`, and they are read
together.** "Complete" means *complete for that horizon*. A run configured to
reach further back than its predecessor reopens the cursor and continues from
the same floor; everything above it is already stored. A run configured to reach
*less* far back changes nothing and deletes nothing.

**5. `consecutive_failures` is not implemented.** It exists to drive exponential
backoff and, past a threshold, to disable a chat and raise a Notification.
Neither exists, so the counter would be written and never consulted. Unlike
`newest_synced_message_id` it records nothing historical — it starts at zero
whenever it is added — so deferring it costs a migration and nothing else.

**6. The per-chat cap is not implemented; the horizon is.** `PROJECT_SPEC.md`
§4.1 names both. The cap needs a count of stored messages per chat, which needs
a repository method and an index that should be chosen by the query using it
(`DATABASE.md` §20). The horizon already bounds every run, and `--max-batches`
bounds any one of them.

**7. The batch size is `telegram.backfill_batch_size`, default 100.** Aligned
with TDLib's practical history page size, so one batch is one round trip rather
than a fraction of one. At the documented per-chat cap that is roughly 500 short
transactions rather than one long one. **Two measurements would justify changing
it**, and neither can be taken yet: the p99 duration of a backfill transaction
against the p95 latency a UI read tolerates while one is running, and the share
of a batch's wall-clock spent in `getChatHistory` rather than in SQLite. If the
first is too high the number goes down; if the second dominates, it goes up.

---

### Alternatives Considered

**A surrogate key with a unique `chat_id`.** What the diagram specified. It buys
nothing: no child table references a cursor, so there is no foreign key that a
narrower key would simplify, and the unique index would be the real key with an
integer beside it.

**A cursor per account rather than per chat.** Smaller, and wrong: chats are
backfilled independently and at different depths, so one bookmark could only
record the least-advanced of them.

**A cursor per account and source.** Anticipates a second source of history.
There is one, and a key shaped for a second would be a guess about what
distinguishes them.

**A timestamp cursor.** Reads naturally next to a horizon expressed in days.
Rejected on the three properties above; the horizon is a *filter* applied to
fetched messages, which is a different job from a cursor and needs no ordering
guarantee.

**Advancing the cursor after the commit.** One line simpler. It is also the
single defect this whole slice exists to prevent: a crash between the two leaves
messages stored and the bookmark behind them, and the next run re-reads a range
it already has. Harmless here only because writes are idempotent — and relying
on that would make idempotency load-bearing for correctness rather than for
efficiency.

**Advancing the cursor before persistence.** Fails in the direction that loses
data: a crash leaves the bookmark past messages nobody stored, and the next run
skips them permanently.

**One transaction for the whole backfill.** Considered and rejected in ADR-050;
implementation confirmed it, because the loop holds the connection across a
network call to Telegram between every page.

**A single `backfill_complete` with no horizon.** Would make raising the
configured horizon a no-op that looks like success, which is the worst kind.

---

### Consequences

**Positive**

- Resumability needed no repair logic and no reconciliation pass. It is a
  property of the transaction boundary, and the test that proves it throws an
  exception one statement before the commit.
- The cursor cannot describe a range it does not have: the check constraint and
  the entity both refuse one end without the other.
- Raising the horizon resumes rather than restarts.
- Migration `0008` adds one table and no index beyond its key, because the only
  query is by that key.

**Negative**

- `newest_synced_message_id` is written and not yet read. Justified in decision
  3, and it is one column.
- The backfill is unbounded in count. A chat with 200 000 messages inside the
  horizon fetches all of them, one batch at a time. `--max-batches` is the
  manual bound until the cap arrives.
- A cursor per chat means a run over an account with many chats is a loop of
  loops, and there is no scheduler ordering it. `sync history` takes chats in
  listing order.

---

### Related Decisions

ADR-050 (when the cursor is written, and one page per transaction — this
completes it), ADR-034 (one transaction at a time, which is why a batch is
bounded), ADR-038 (one row per parent means the parent's key is the key),
ADR-043 (composite ownership), ADR-045 (idempotent message writes, which make a
re-read cheap rather than wrong), ADR-053 (synchronisation never overrules the
operator — a backfill refuses a chat with `sync_enabled` off).
Supersedes the `sync_cursors` shape in `DATABASE.md` §3.


---

# ADR-055

## Title

Live Updates Are Dispatched by the One Receive Loop, Buffered from Connect, and Consumed Serially

Status

Proposed

Date

2026-07-30

---

### Context

Slice 3 established that **one dispatch loop is the sole consumer of
`TdjsonClient.receive()`** (ADR-051), because the queue holds one item per
update and a second consumer would split the stream rather than duplicate it.
Slice 7 has to deliver those updates to the application without breaking that.

Four questions had to be answered, and each has a plausible wrong answer.

**Where does dispatch belong?** The gateway already owns the loop. But
`API.md` §10 constraint 1 says the gateway never writes to the database, so it
cannot be what ingests.

**What is the start-up order, and why can it not lose an update?** A backfill
walks *downwards* from the oldest stored message. Anything arriving while it
runs is above the top of the stored range, so backfill will never fetch it. If
the update consumer starts after the backfill, that window is a permanent hole.

**How much concurrency?** ADR-034 permits one transaction at a time for the
whole application.

**What happens to an update kind nothing consumes?** TDLib has hundreds and adds
more with every release.

---

### Decision

**1. The dispatch loop feeds a queue; `updates()` drains it.** The gateway maps
`updateNewMessage` into a `NewMessage` DTO and offers it to a bounded
`asyncio.Queue`; `updates()` is an async iterator over that queue. There is
still exactly one consumer of `client.receive()`, and the architectural test
that asserts it is unchanged.

Ingestion belongs to an **application use case**, `SyncLive`, which consumes
`updates()` and writes. The gateway emits; the application persists. That split
is what lets live synchronisation be tested against a fake gateway and a real
database, exactly as the backfill is.

**2. The queue starts filling at `connect()`, before anything consumes it.**
This is the whole answer to "why can the ordering not lose an update". The order
is: authenticate → connect (**the queue begins filling here**) → sync chats →
back-fill → catch up → drain. Whatever arrives during the middle three steps is
already held and is delivered the moment the drain begins.

**3. A live run catches up before it drains.** The queue covers a *running*
process; it cannot cover one that was not running. So a live run first pages
forward from `newest_synced_message_id` until it meets the stored range. This is
the reader that field was recorded for (ADR-054), and without it a restart would
leave a permanent hole between the last run's newest message and the first
update the new run happens to see.

**4. Updates are processed serially, one transaction each.** Per-chat or pooled
concurrency would contend for the one connection ADR-034 permits and buy nothing
but nondeterministic ordering. Correctness over throughput, as the goal says.

**5. An unknown update kind is counted and dropped; the loop continues.** So is
a `updateNewMessage` whose payload this version cannot map. A TDLib release that
adds a kind must not be able to stop the loop, because a stopped loop leaves
every waiter hanging on a state that can no longer change.

**6. Backpressure, not loss, while running.** A full queue stops the dispatch
loop, which stops draining `client.receive()`, whose own bounded queue fills,
which stops the receive thread, which leaves TDLib holding the backlog (§7.1).
Nothing is dropped anywhere along that chain.

**7. During shutdown, queued updates are dropped and counted.** `disconnect()`
sets a closing flag first, after which the dispatch loop drops rather than waits
— a loop blocked on a queue nobody will drain again is a shutdown that hangs.
What is lost is bounded by the queue and recovered by the next run's catch-up.

**8. Shutdown order lives at the composition root.** `Container.run_live_sync`
cancels the consumer, lets its in-flight transaction roll back through the unit
of work's own exit path, and only then lets the caller close the gateway and the
database. It is there rather than in a use case because joining an
infrastructure supervisor to an application use case is what a composition root
is *for*, and an architectural test refuses the alternative.

**9. `MessagesIngested` is published after the commit, never inside it.** A
handler observing a fact that then rolled back would be acting on something that
never happened. A process that dies between the commit and the publication keeps
the messages and loses the event, which is the right way round: the `EventBus`
is explicitly neither durable nor transactional (contract points 4 and 6), and
anything that must survive is a database write.

---

### Alternatives Considered

**A second consumer of `client.receive()` for updates.** The obvious shape, and
wrong for the reason ADR-051 already gave: the queue holds one item per update,
so two consumers take turns and each silently misses what the other took.

**The gateway writing to the database.** Removes a hop. Rejected: it breaks
`API.md` §10 constraint 1, and it would make live synchronisation testable only
against a real TDLib.

**A dedicated dispatcher class between the gateway and the use case.** A third
component to route one update kind to one consumer. It would be an abstraction
over a single `if`.

**Draining updates before the backfill, or instead of a catch-up.** Draining
first would work for a running process and still leave the restart hole; the
catch-up is what closes it, and it is needed either way.

**An unbounded update queue.** No backpressure to design. Rejected: a consumer
that stops turns memory into the failure mode, and the failure appears far from
its cause.

**Dropping the oldest update when the queue is full.** Bounded memory and
silent loss, which is the combination this project avoids everywhere else.

**Per-chat parallelism.** Genuinely appealing — different chats are independent.
Rejected on ADR-034: one connection means the transactions serialise anyway, so
the only thing gained is a nondeterministic order to reason about.

**Failing the run on an unknown update.** Would surface TDLib drift immediately.
It would also mean a Telegram feature release could stop the application, which
is a far worse failure than an uncounted update.

**Restarting the consumer on any failure, forever.** Rejected: a permanently
broken consumer becomes an endless log. It restarts a bounded number of times
(`telegram.live_max_restarts`, default 5) and then reports the failure.

---

### Consequences

**Positive**

- One ingestion path. Backfill, catch-up and live updates all go through
  `IngestMessages`, so the idempotency rule has one home and a duplicate update
  costs nothing.
- Ordering is deterministic and testable: arrival order in, arrival order out,
  asserted against both gateway implementations.
- A restart loses nothing that Telegram still has. The catch-up closes the gap
  the queue cannot.
- Shutdown is one method and one order, and cancelling mid-update leaves the
  database consistent because the transaction boundary already guaranteed it.

**Negative**

- A slow consumer eventually stalls the dispatch loop, which delays *state*
  reporting as well as messages. Acceptable while nothing waits on a state
  change during a live run; a metric on queue depth (§10.3) is the warning.
- `sync status` reads stored state only. A live run in another process is not
  observable without a control channel between processes, which nothing needs
  yet.
- The catch-up is bounded (`telegram.catch_up_pages`, default 20). A chat that
  received more than that while the process was down needs a backfill re-run,
  and the report says so rather than walking forward indefinitely.
- Exactly-once persistence is a property of the *database*, not of the stream.
  An update delivered twice is stored once because of the partial unique index
  (ADR-045); the stream itself promises at-least-once.

---

### Related Decisions

ADR-051 (one consumer of the receive stream — this extends it rather than
contradicting it), ADR-048 (the receive thread), ADR-050 (batch boundaries and
event granularity — decisions 3 and 4 are settled here), ADR-054 (the cursor
`newest_synced_message_id` the catch-up reads), ADR-034 (one transaction at a
time, which is why processing is serial), ADR-031 (synchronous event delivery),
ADR-045 (idempotent writes, which is what makes a duplicate update free),
ADR-011 (the dependency rule, which decides where the supervisor is joined).


---

# ADR-056

## Title

A Conversation Is a Time Range with a Matched Identity, and Membership Is Not Stored

Status

Proposed

Date

2026-07-30

---

### Context

`DOMAIN_MODEL.md` §5.7 defines a Conversation and gives the segmentation rule —
a gap of `conversation_gap_minutes`, a cap of `conversation_max_messages` — and
states that "re-segmenting a Chat from its messages yields identical
boundaries". Implementing that found four questions it does not answer, and one
place where it contradicts a decision made since.

**How does a message know which conversation it is in?** The obvious answer is
a `conversation_id` column on `messages`. But `Message` is append-only and
`MessageRepository` has no update path *at all* — ADR-046 made that structural
rather than conventional, on the grounds that a guarantee which exists only as
a convention is one somebody eventually breaks.

**What keeps an identity stable?** Segmentation recomputes boundaries from
scratch. If each computed segment simply took a fresh identifier, every rebuild
would replace every conversation in the chat with an identical-looking new one,
and anything that had referenced one would point at a row that no longer exists.
Milestone 8's summaries, plans and analyses all attach to a Conversation.

**What does `is_open` mean once segmentation is a re-derivation?** §5.7 gives
Conversation an `is_open` flag and `ended_at` that is "null while open", and
`DATABASE.md` gives a partial unique index enforcing at most one open
conversation per chat. But a conversation derived from messages that already
exist always has a last one.

**How much of a chat does a new message force to be recomputed?** Everything,
or something smaller that is still provably correct.

---

### Decision

**1. Membership is the time range. Messages carry no `conversation_id`, and
there is no join table.** A message belongs to the conversation whose
`[started_at, ended_at]` contains its `sent_at`.

Two things make that exact rather than approximate. Conversations within a chat
do not overlap — enforced by a unique `(account_id, chat_id, started_at)`,
because each is a contiguous run and two cannot begin at the same instant. And a
boundary requires a gap, so no instant falls inside two of them.

It also keeps a rebuild cheap: fifty thousand messages produce a few hundred
conversation rows, and only those are written. A stored link would mean
rewriting fifty thousand rows to say what a handful of timestamps already say.

**2. Identity is matched, not generated.** Each computed segment claims **the
stored conversation that owns the plurality of its messages**; a stored
conversation may be claimed by at most one segment, the earliest in order; ties
break to the lowest identifier. A segment that claims nothing gets a new
conversation; a stored conversation nothing claimed is deleted.

Both tie-breaks exist to make the result a function of the arguments alone.
Without them a rebuild could produce different identities on two runs over the
same data, which is the failure the whole rule exists to prevent.

The rule is four lines and gets the four interesting cases right: **extension**
(the last conversation keeps its identity and grows), **a new episode** (created;
earlier ones untouched), **merge** (the larger contributor's identity survives,
the other is deleted), **split** (the earlier half keeps the identity, the later
is new — because a stored conversation can only be claimed once).

**3. `is_open` is derived, not stored, and `ended_at` is not nullable.** Whether
a conversation may still grow depends on how long ago it ended — on *now*. A
stored flag would be true when written and wrong an hour later, with no job to
correct it. `Conversation.is_open_at(now, gap)` asks the question against a
supplied instant.

This supersedes `DOMAIN_MODEL.md` §5.7's `is_open` attribute and
`DATABASE.md`'s partial unique index on it. The unique
`(account_id, chat_id, started_at)` replaces that index and enforces something
stronger: non-overlap, which is what membership-by-range depends on.

**4. Re-segmentation is windowed.** A boundary depends only on the gap to the
message before it, so a new message can change nothing earlier than the
conversation it follows. A pass therefore re-segments from the start of the
conversation returned by `latest_before(chat, since)` and leaves everything
before it alone. Widening to that conversation's *start* rather than its end is
necessary: a window opening mid-conversation would see its first message with
nothing before it, call it a boundary, and split an episode at whatever instant
the caller happened to name.

**5. What is stored, and what is not.** Stored: `started_at`, `ended_at`,
`message_count`. The first two are the membership rule and the listing order;
the third is what the cap rule and the listing need, and counting rows per
conversation for a listing is a query that would otherwise need its own index.

Not stored: `is_open` (decision 3); `initiated_by`, which is recomputable from
the first message's sender and which nothing reads until relationship metrics;
`dominant_language`, which needs language detection this slice may not
introduce; `first_message_id` and `last_message_id`, which the two timestamps
already locate.

**6. The rule stays the documented one — an inactivity gap plus a message cap.**
Deterministic because it reads only `sent_at` and a count, both immutable once
stored, over a total order — `(sent_at, telegram_message_id, id)` — whose every
component is immutable too.

**7. Whoever commits, announces.** `IngestMessages.execute` publishes
`MessagesIngested`; `ingest_within` does not, because its caller owns the
commit. Segmentation subscribes at the composition root, so every path that
stores messages segments them the same way and neither component imports the
other.

---

### Alternatives Considered

**A `conversation_id` column on `messages`.** The conventional design, and it
would need an update path on an append-only aggregate — reopening exactly what
ADR-046 closed. It would also make every rebuild an O(messages) write.

**A `conversation_messages` join table.** Keeps messages immutable, at the cost
of storing a fact two timestamps already imply and of a second thing for the
segmentation transaction to get wrong. Same O(messages) rebuild cost.

**Identity from the first message.** Simple, and it fails on the case backfill
produces constantly: a message arriving *before* the current first one moves the
key, so an extended conversation looks like a new one and the old is deleted.
Plurality matching handles that and costs a `Counter`.

**Identity from `(chat_id, started_at)` as the primary key.** Same failure, plus
a primary key that changes.

**Rebuilding the whole chat on every ingest.** Correct and simple. Rejected: a
live message would recompute fifty thousand messages' worth of boundaries, and
the guarantee that earlier conversations are untouched would hold only because
the answer happened to be the same rather than because nothing looked at them.

**Calendar-day segmentation.** Cuts an evening exchange at midnight, and needs a
timezone Telegram does not report.

**Sender-change segmentation.** Turns an ordinary back-and-forth into dozens of
conversations.

**Telegram reply chains.** `Message` has no `reply_to_message_id` — deliberately
— most messages are not replies, and a reply to a month-old message would splice
two episodes a person would never call one.

---

### Consequences

**Positive**

- Segmentation is a pure function plus a matching rule, and both are testable
  without a database. "Running it twice changes nothing" is a test, not a hope.
- A rebuild writes conversation rows only, so it stays cheap however long the
  chat is.
- `Message` keeps its append-only discipline intact. Nothing about a stored
  message changes when it is segmented.
- Non-overlap is a unique index rather than a rule anybody has to remember.

**Negative**

- A conversation describes the messages that existed when segmentation last ran.
  `message_count` and `ended_at` go stale if messages are ingested with
  `conversation.segment_on_ingest` off, and `conversation rebuild` is the fix.
- Reading a conversation's messages is a range query rather than an indexed
  lookup by key. It is served by the index the history listing already needed,
  so the cost is a scan bounded by the conversation's own length.
- Changing `gap_minutes` or `max_messages` changes boundaries for every chat on
  the next rebuild, and a summary attached to an old boundary would be attached
  to a conversation that no longer means the same thing. Milestone 8 must decide
  what a boundary change does to what hangs off it; this decision only makes the
  change visible rather than silent.
- The identity match is a plurality vote, which is exact for extension and
  creation and a *judgement* for merge and split. Two conversations merged
  50/50 give the identity to the lower identifier. That is deterministic, and it
  is a choice rather than a truth.

---

### Related Decisions

ADR-046 (messages are append-only — the reason membership is not stored on
them), ADR-043 (composite ownership, applied to `conversations`), ADR-038 (one
row per parent means the parent's key is the key — *not* applied here, because a
chat has many conversations), ADR-050 (one event per committed batch, which is
what segmentation subscribes to), ADR-031 (synchronous delivery, which is why a
failing subscriber cannot break ingestion), ADR-034 (one transaction at a time,
which is why a pass is one unit of work).
Supersedes `DOMAIN_MODEL.md` §5.7's `is_open` and `DATABASE.md`'s partial unique
index on it.


---

# ADR-057

## Title

One Provider Port, One Execution Use Case, and an AI Call That Records a Digest Rather Than an Answer

Status

Proposed

Date

2026-07-30

---

### Context

Everything after this milestone -- memory extraction, summaries, reply
suggestion, emotion assessment -- invokes a model. Each of them needs the same
five things: the privacy gate checked, a timeout applied, tokens and cost
counted, failures normalised, and a record written. Built once per feature,
those five would be built five times and would drift, and the one that drifted
would be the privacy gate.

`DOMAIN_MODEL.md` §5.24 describes an AI Provider as a *stored configuration
record* with a `capabilities` set discovered by `ai check`, `is_default_for`
routing and a `priority`. §5.25 describes an AI Call and says it **never stores
prompt or response content**. `PROJECT_SPEC.md` §4.2 requires every AI feature
to degrade rather than break when no provider is configured. ADR-024 makes the
data boundary a per-chat permission. ADR-029 §6 requires cost and latency to be
measurable from the first milestone.

Implementing that raised five questions those documents do not settle.

**How many ports?** The obvious decomposition is one per capability --
completion, embedding, classification -- because that is how the vendors'
own SDKs are shaped.

**What is a provider, in the code?** §5.24 reads as a row in a table with a
priority and a routing policy. Nothing in this milestone routes anything.

**How is deterministic replay possible if nothing about the response is
stored?** Two runs of the same prompt at temperature 0 should be comparable,
and §5.25 forbids keeping what would make them comparable in the obvious way.

**What is the fake provider, and where does it live?** Tests need a provider
that answers without a network. So does a fresh installation with no API key.

**What may the execution use case interpret?** A use case that parsed what came
back would put the shape of every future task inside the one component every
future task shares.

---

### Decision

**1. One port, `AiProvider`, with two members: `model` and `generate`.** A
capability is a property of the model, not a class of client. Anthropic's
Messages API answers a completion, a classification and a structured extraction
through one endpoint; splitting the port would mean three adapters over one
endpoint, and a caller choosing between them by guessing which one the vendor
implemented.

The port declares what this slice's caller needs and nothing else (ADR-051).
When embeddings arrive they will need a second port, because an embedding is
not text and no honest signature covers both -- and that port will be declared
by the slice that has a caller for it.

**2. `AiModel` is a value object, not a stored `AiProvider` record.** What a
call needs to know is which model answered and what using it implied. That is
data, not an aggregate: it has no lifecycle, nothing transitions it, and there
is nothing to look it up by. §5.24's table -- `capabilities`, `is_default_for`,
`priority`, `is_enabled` -- is a *routing* record, and routing is a decision for
the milestone that has two providers to route between. Configuration names one
model; the container builds it.

The `data_boundary` is derived from the **vendor**, not read from
configuration. A cloud model is external however a settings file describes it,
and a boundary a user could edit would put the privacy guarantee in a file.

**3. `ExecuteAiTask` is the only place a model is invoked, and it interprets
nothing.** It resolves the account, reads the chat's `ai_processing_mode`,
refuses or proceeds, applies the timeout, calls the provider, and records what
happened. It returns the response text verbatim. No parsing, no schema, no
repair. Those belong to the task that knows what shape it asked for.

The gate is a table, and the fourth row is the one worth stating:

| chat mode | local model | external model |
| --- | --- | --- |
| `disabled` | refused | refused |
| `local_only` | allowed | refused |
| `cloud_allowed` | allowed | allowed |
| *no chat named* | allowed | **refused** |

Content that names no chat carries no permission, and in a local-first
application the absence of a permission is not a permission.

**4. A refusal is recorded, not merely raised.** "Why did nothing happen"
deserves an answer, and an audit that only contains the calls that were allowed
cannot show that a call was blocked. The refusal record spends no tokens and
carries no finish reason; the raised error names the record's identifier.

**5. `AiCall` is append-only, and the repository has no `update` and no
`delete`.** The same structural discipline as `Message` (ADR-046): an audit
record that nothing can edit needs no rule saying not to edit it. The cost is
computed by `AiCall.record` from the model's rates rather than supplied, so no
caller can write a number the rates do not support.

**6. The response is stored as a truncated SHA-256 digest. The text itself is
stored only when `ai.store_responses` is on, which the production profile
refuses.** This resolves the tension between deterministic replay and §5.25's
prohibition, and it resolves it in the direction §5.25 chose: what replay
actually needs is *did these two runs produce the same answer*, and a digest
answers that without the answer being readable. Storing the text is a
diagnostic, arranged exactly as `logging.diagnostic_mode` already is -- opt-in,
refused by the production profile, and visible in configuration rather than
implied.

The prompt is never stored, under any setting. It is reconstructible from the
prompt version and the message rows, and storing it would duplicate message
content into a table with a different retention class.

**7. `PromptVersion` is required on every call, from the first one.** A prompt
version costs one column now. Without it, the first time an output changes
there is no way to tell whether the model changed or the prompt did -- and that
question is asked *after* the change, about data that was already being
collected when it happened. It is not a field that can be added retroactively.

**8. The scripted provider is shipped source, not a test fixture.** It lives in
`infrastructure/ai/scripted.py` and the container builds it when
`ai.vendor = fake`, which is the default. Two consequences, both intended: a
fresh installation has a working AI boundary that costs nothing and reaches no
network, and the provider the tests exercise is the provider that ships. It is
deterministic by construction -- answers and failures are queued, latency is a
parameter, token counts are `len(text) // 4`. Nothing in it samples randomness.

**9. Costs are stored as text, not as `REAL`.** Money in fractions of a cent,
accumulated over many rows, is exactly where binary floating point drifts.
`Decimal` in the domain, `String` in the column.

**10. `ai_calls.chat_id` is part of a composite foreign key that cascades.**
Composite so that a call in one account cannot name another account's chat
(ADR-043). Cascading because a record derived from a chat the user deleted is
residue of that chat, and every other child of `chats` already cascades for
that reason -- and because SET NULL is not available on a composite key at all:
it nulls every column in the key, including the NOT NULL `account_id`.

**11. The real adapter takes an injected `HttpTransport`.** A one-method seam
with a stdlib `urllib` implementation. It adds no dependency, and it lets the
contract suite exercise the real adapter in full -- body, headers, parsing, stop
reason, usage -- without opening a socket.

---

### Alternatives Considered

**A port per capability (`CompletionProvider`, `EmbeddingProvider`, ...).**
Rejected for this slice. It is the shape the SDKs suggest, not the shape the
callers need, and there is exactly one caller. It would also force a decision
now about a capability nobody has written a use case for.

**A stored `ai_providers` table, as §5.24 describes.** Rejected until there are
two providers to choose between. A routing table with one row is a routing
table that has never been exercised, and `is_default_for` cannot be designed
honestly without knowing what it is choosing between.

**Storing the full response.** Rejected as the default. It reintroduces message
content -- a model's summary of a conversation is conversation content -- into
a table whose retention class is logs rather than conversations. Available as a
diagnostic because reproducing a bad extraction without it is guesswork.

**Storing nothing about the response.** Rejected because deterministic replay
was a stated objective of this slice and nothing would satisfy it. The digest
is the smallest thing that does.

**Letting `ExecuteAiTask` parse structured output.** Rejected. Every task's
schema would land in the one component every task shares, and the first
malformed response would make the shared component the thing that has to
change.

**A `retry_count` field and retry inside the use case.** Rejected for now.
§5.25 lists it; nothing retries yet, and a column nobody writes is a column
nobody keeps correct. Retry is a decision for the slice that has a task worth
retrying, and it will need a policy -- which failures, how many, with what
backoff -- that this slice has no evidence to choose.

**Storing the model's price list alongside each call.** Rejected. The cost is
stored, so the rates that produced it have no reader. Repricing the vendor's
catalogue still cannot change a past call, because what it cost was written
down at the time.

**Adding `httpx`.** Rejected. The transport seam gets the same testability with
no new dependency, and a dependency added for one adapter is one the whole
application then carries.

---

### Consequences

**Positive**

- Every later AI feature gets the privacy gate, the timeout, the cost
  accounting and the audit record by calling one use case, rather than by
  remembering four things.
- The privacy gate exists in one place, and its fourth row -- no chat, no cloud
  -- is a default rather than a check somebody has to add.
- A fresh installation has a working AI boundary with no key, no network and no
  cost, and the provider it uses is the one the tests exercise.
- The real adapter is fully tested without a network, so the first real call is
  not the first time its request body is exercised.
- Prompt versions exist from the first call, which is the only time they can be
  introduced for free.
- Cost is exact. A spend total is a sum of `Decimal`, not of floats.

**Negative**

- Deleting a chat removes its calls from the spend history. That is the price
  of not keeping residue of a deleted chat, and it is deliberate.
- Deterministic replay compares digests, not answers. It can show that two runs
  differ; it cannot show *how*, without `ai.store_responses` -- which is not
  available in production.
- One port means one shape. An embedding model will need a second port, and the
  slice that adds it will have to decide whether `AiCall` records both.
- Nothing routes. Configuring a second model means changing configuration, not
  choosing per task, until the milestone that needs routing arrives.
- `ExecuteAiTask` returns text. Each caller parses its own output, and the
  first two callers may well parse it the same way before a shared helper is
  justified.

---

### Related Decisions

ADR-024 (per-chat AI processing mode — this is the code that reads it),
ADR-046 (append-only aggregates, applied to `AiCall`), ADR-043 (composite
ownership, applied to `ai_calls`), ADR-021 (secrets are referenced by name,
never stored — `api_key_ref`), ADR-051 (ports are declared one slice at a time,
which is why there is one), ADR-034 (one transaction at a time, which is why
the record is its own unit of work), ADR-013 (asyncio, which is why the timeout
is `asyncio.timeout`), ADR-029 §6 (cost and latency measurable from the first
milestone).
Refines `DOMAIN_MODEL.md` §5.24 (an `AiModel` value object now, a routing record
later) and §5.25 (the outcome set, and the digest).

---

# ADR-058

## Title

The Model Proposes and the User Decides: Versioned Prompts, Validated Output, and Immutable Proposals

Status

Proposed

Date

2026-07-30

---

### Context

ADR-057 built the boundary through which a model is reached. This is the first
thing to go through it, and it is the one that decides what "AI-assisted" means
for the rest of the project: whether the model writes to the system's memory, or
suggests things a person writes.

`DOMAIN_MODEL.md` §5.10 already describes a Memory Proposal, and ADR-019
already says proposals exist to keep hallucinated or injected content out of
permanent memory. `PROMPTS.md` specifies a prompt registry, front matter and
untrusted-content delimiters. `ADR-020` requires structured output with one
repair attempt. None of those had been implemented, and implementing them raised
five questions the documents do not settle.

**What may a model decide?** The obvious reading of "extract memories" is that
the model returns memories. But a returned memory has an identifier, a status
and a provenance, and every one of those is a thing the model could get wrong in
a way nobody would notice.

**Where does the boundary between validation and interpretation fall?** A schema
can say a confidence is a number between zero and one. Whether 0.4 is worth a
person's attention is a different kind of statement, and putting both in one
component means the component changes whenever either does.

**How is an extracted fact checked at all?** Re-reading the conversation is the
only real check, and doing that for every proposal defeats the purpose.

**What does one repair attempt cost, and what is it worth?** A second model call
per extraction, and a second AI call row, on every malformed answer.

**Which transactions does a model call sit inside?** ADR-034 permits one
transaction at a time for the whole application; a model call takes seconds.

---

### Decision

**1. Extraction produces `MemoryProposal`s, never `Memory`s.** Nothing this
milestone writes is believed. A proposal enters a review queue as `pending`, and
there is no code path in this slice that changes that -- Slice 9c adds the one
transition.

The alternative -- writing what the model extracted and letting the user correct
it -- fails in a way that is hard to recover from. A wrong memory is not merely
wrong: it is *retrieved*, put into later prompts and used to justify later
suggestions, so an error propagates into work the user never connected to the
extraction that caused it. By then "why does it think that" has no visible
answer. Proposals invert the default: the worst a bad extraction can do is waste
a moment of review.

**2. The model supplies four fields and nothing else: category, value,
confidence, evidence.** The schema sets `additionalProperties: false`, so an
answer carrying an `id` or a `status` fails validation rather than being
partially trusted. Identifier, timestamp, conversation, AI call, prompt version
and status are all assigned by the application.

That is not defensive coding for its own sake. A model that could name an
identifier could name one already in use, and so overwrite a proposal a person
had already read; one that could set a status could approve itself.

**3. Prompts are versioned files inside the package, indexed by a registry.**
No prompt text in Python source (ADR-008). Discovery is by `_registry.yaml`,
never by globbing a directory: a loader that globbed would silently lose a
prompt on rename and silently gain one on a stray file, and both surface as a
model answering the wrong question.

The registry names *which prompts exist* and *which schema each is bound to*.
The **version lives only in the prompt's own front matter** -- `PROMPTS.md` §3
puts it in both places, and two places recording a version is one too many for
them to agree.

Everything is validated when the registry loads, at startup: the file exists,
its front matter parses, its declared id matches its registry key, it declares a
version, its declared inputs match the variables its body actually uses in
**both** directions, and its schema exists, parses and uses only keywords the
validator implements. A prompt discovered to be broken while a user waits for a
suggestion is the same defect found at the worst moment (ADR-026 §7).

**4. Untrusted content is delimited and neutralised by the prompt model, not by
the prompt file.** A template that had to remember the delimiters is a template
that can forget them. Any run of three or more angle brackets in untrusted text
is collapsed to two before insertion, so no message can forge a boundary and
continue as though it were the prompt. Previously-stored proposal values are
treated as untrusted too: they are model output derived from conversation
content that nobody has reviewed.

This is a mitigation, not a solution. The architectural answer is that a
successful injection reaches nothing valuable: the output is schema-validated,
becomes a proposal rather than a memory, and is shown to a person before it
counts.

**5. Structured output is validated before anything interprets it, by a
validator with no business rules.** It checks required fields, types,
enumerations, numeric ranges and lengths. It does not know what a confidence
threshold is, what "already proposed" means, or what a conversation is. That
separation is what lets the same validator serve summaries and reply
suggestions later without acquiring an argument for each.

**6. The validator implements a deliberately small subset of JSON Schema, and
refuses any schema using more.** `require_supported` rejects an unknown keyword
when the schema is *loaded*, so "unsupported" can never mean "silently ignored"
-- which is the failure that would make a hand-written validator worse than no
validator. Adding a dependency was the alternative; this is one file, exercised
by the tests that matter, and the escape hatch is a startup error rather than a
wrong answer.

**7. Exactly one repair attempt.** A model that answered with prose, or with
JSON missing a field, is usually right about the content and wrong about the
format, and telling it precisely what was wrong usually fixes it. Once. A second
repair is a different failure -- the model has now been told twice -- and costs
another call to arrive in the same place. The repair returns the model's own
answer to it, not the original request, and forbids adding facts: a repair that
changed the content would be a second, unreviewed extraction.

**8. Three deterministic filters run after validation, and the first is the one
that matters.**

* **Ungrounded evidence.** The quotation must appear in the text the model was
  shown, compared with whitespace collapsed and case folded. This is the
  cheapest anti-hallucination check available and it catches the failure that
  matters most: a fluent, plausible fact about somebody that nobody ever said. A
  model cannot quote what nobody said. Nothing more forgiving is used -- fuzzy
  matching would start accepting nearly-right quotations, which is exactly what
  an invented fact produces.
* **Low confidence**, below a configured threshold. Self-reported confidence is
  poorly calibrated (`AI_MODELS.md` §15); it is used as a coarse filter and
  nothing more is claimed of it.
* **Already proposed**, against what is stored *including rejected proposals*,
  and against the batch itself.

Every discard is counted in the report. A run that returned nothing and a run
that discarded eight invented claims are very different events, and a report
that could not tell them apart would make a broken prompt look like a quiet
conversation.

**9. Re-running extraction over a conversation is free, and a unique index makes
it so.** `(account_id, conversation_id, category, value)` is unique. The
application checks for duplicates before writing; the index is what makes it
true rather than usually true, because the read and the write are different
transactions.

**10. The model call sits inside no transaction.** Read in one, call the model
in none, write in another. Holding the single application-wide transaction
across a call that takes seconds would stop everything else in the process,
including the live update loop. The cost is that the world can change in
between; what that can produce is a duplicate, which the index refuses -- so the
worst case is a proposal that is not stored, never a proposal that is wrong.

**11. A proposal is immutable, and terminal states are terminal by absence.**
`MemoryProposalRepository` has no `update` and no `delete`; `MemoryProposal` has
no method that returns a changed one. `accepted` and `rejected` exist in the
enumeration and in the check constraint because Slice 9c writes them -- until
then they cannot be reached at all, which is a stronger guarantee than a rule
saying they must not be reached twice.

Slice 9c will add exactly one transition, and it will need a `decided_at`
column and a named method restricted to pending-to-terminal. Neither is added
now: a column nobody writes is a column nobody keeps correct.

---

### Alternatives Considered

**Writing memories directly, with an undo.** Rejected. Undo needs the user to
notice, and the failure mode of a wrong memory is precisely that it is used
quietly. It also makes every downstream feature depend on state a model wrote.

**Letting the model return an identifier or a status.** Rejected; see decision
2. The schema refuses the fields rather than the code ignoring them, so an
attempt is a validation failure rather than a silent discard.

**Taking a `jsonschema` dependency.** Reasonable, and the fallback if the subset
proves limiting. Rejected for now because the load-time keyword check makes the
subset safe, and because a dependency added for one validator is one the whole
application then carries. **This is the decision most worth revisiting** -- if a
later prompt needs `oneOf` or `$ref`, take the dependency rather than growing
this file.

**Prompts at the repository root, as `PROMPTS.md` §2 specifies.** Rejected. A
prompt is an asset the application cannot run without, and one outside the
package would be missing from every installation that is not a git checkout.
Same tree, in a place that ships.

**Repeating the version in the registry as well as the front matter.**
Rejected. Two sources of truth for a version is one too many for them to agree,
and the file's own version is the one a person edits when they change the text.

**A confidence threshold inside the validator.** Rejected; see decision 5.
Shape and worth change for different reasons.

**Storing `key`, `contact_id`, `conflicts_with_memory_id` and
`rejection_reason`** (all named by `DOMAIN_MODEL.md` §5.10). Deferred. Each
belongs to a capability that does not exist: supersession, memories, conflict
detection and deciding.

**Retrying a malformed answer more than once.** Rejected; see decision 7.

**Extracting per message rather than per conversation.** Rejected. A fact is
often assembled across several messages ("I moved to Lisbon" / "for the new
job"), and per-message extraction would either miss those or attribute them to
an arbitrary one of the messages involved.

---

### Consequences

**Positive**

- Nothing a model produces can become believed state without a person. That is
  the project's core principle made structural rather than aspirational.
- Every proposal can be checked without re-reading the conversation, because it
  carries the sentence it came from.
- An extraction is reproducible: given the same conversation and the same
  answer, the same proposals are stored, and the second run stores nothing.
- Prompt versions are recorded on both the AI call and the proposal, so "which
  proposals came from the prompt we changed last week" is answerable.
- A broken prompt or schema stops the application at startup, on the machine of
  whoever built it, rather than while a user waits.
- The four layers -- prompt, execution, validation, interpretation -- can each
  be replaced without touching the others.

**Negative**

- A malformed answer costs two model calls, both billed and both recorded.
- The grounding check discards true facts a model paraphrased rather than
  quoted. That is a deliberate trade: a paraphrase cannot be checked, and the
  prompt asks explicitly for quotations.
- The queue can grow without bound. Nothing expires proposals, and
  `DOMAIN_MODEL.md` §5.10's ninety-day expiry is not implemented.
- `MemoryProposalsCreated` is published with no subscriber, departing from the
  rule the earlier slices followed. The consumer is known rather than guessed --
  the `memory_proposals_pending` notification (§5.23) -- and the alternative is
  a component that polls the table.
- The JSON Schema subset is a maintenance liability. It is bounded by the
  load-time check, but it is still a validator this project owns.
- The shipped scripted provider cannot produce a valid extraction. Running
  `tgassist memory extract` with the default `ai.vendor: fake` exercises the
  whole pipeline and then honestly reports that the answer could not be read.

---

### Related Decisions

ADR-019 (proposals keep hallucinated content out of memory -- this implements
it), ADR-057 (the execution boundary this is built on), ADR-008 (prompts as
files), ADR-020 (structured output and one repair), ADR-026 (registry and
startup validation), ADR-024 (per-chat AI processing mode, inherited through
`ExecuteAiTask`), ADR-043 (composite ownership, applied to `memory_proposals`),
ADR-046 (append-only aggregates), ADR-034 (one transaction at a time -- the
reason the model call sits outside them), ADR-056 (conversations, the unit
extraction reads).
Refines `DOMAIN_MODEL.md` §5.10 (the field set and the status set), `PROMPTS.md`
§2 and §3 (where prompts live, and what the registry records).

---

# ADR-059

## Title

A Memory Is Not a Proposal: Application-Owned Identity, One Irreversible Decision, and No Edits

Status

Proposed

Date

2026-07-30

---

### Context

ADR-058 built the review queue and stopped there deliberately: proposals could
be created and read, and nothing could act on them. This completes the
lifecycle, and in doing so it fixes three things for every later milestone —
what a *memory* is, where its identity comes from, and what a decision about it
means.

`DOMAIN_MODEL.md` §5.9 already describes a Memory, with `key`, `provenance`,
`importance`, `is_pinned`, decay, revisions and a five-state lifecycle. §5.10
describes a Proposal with `approved` / `superseded` / `expired` states and an
auto-approval rule. `PRIVACY.md` §6 and `CLAUDE.md` both require that a user can
delete what the application remembers.

Implementing the review step raised five questions those documents do not
settle.

**Where does a memory's key come from?** §5.9 makes
`(account, contact, category, key)` unique among live memories and says that
constraint "is what makes deduplication tractable" — but never says who chooses
the key. The obvious source is the model that proposed the fact, and the obvious
source is wrong.

**What does "terminal" mean when the terminal state has consequences?** A
rejection changes nothing but a status. An acceptance creates a Memory that is
then read, quoted, and eventually acted upon.

**Is a memory editable?** §5.9 says a value change creates a `MemoryRevision`,
which presumes editing exists.

**What is a fact from a group chat about?** Every memory is "about a Contact",
and a group conversation has no single counterpart.

**What happens to a memory when the conversation it came from is deleted?**
`ai_calls` and `memory_proposals` both cascade from `chats`, so a chat deletion
would take a memory's whole provenance with it — or the memory itself.

---

### Decision

**1. `Memory` and `MemoryProposal` are different aggregates, in different
tables, with different identifiers.** A proposal is what a model said; a memory
is what a person decided to believe. Every later feature that asks "what do we
know about this person" reads a table nothing can write to without a decision,
and that is the whole architecture of the feature.

A memory takes a **new** identifier at acceptance rather than inheriting the
proposal's. They have different lifetimes — the proposal is a permanent record
of an extraction, the memory can be forgotten — and one identifier for both
would make the first indistinguishable from the second in every later
reference.

**2. The key is a deterministic normalisation of the value, derived by the
application.** Case folded, punctuation dropped, whitespace collapsed,
truncated to 120 characters. "Lives in Lisbon." and "lives  in lisbon" produce
one key; the value keeps its original form for a person to read.

The model never supplies it. A key is an *identity*, and identity is the one
thing this project has consistently refused to let a model own (ADR-058 §2):
a model that could name a key could collide with an existing memory and so
silently prevent a true fact from being stored, or claim the identity of one
already there.

**What this buys:** storing the same fact twice is structurally impossible, so
accepting the same fact from two overlapping conversations yields one memory.

**What it does not buy, and this is the important part:** it does **not**
detect a contradiction. "Lives in Lisbon" and "Lives in Porto" normalise to
different keys, so both are stored. Deciding which is true is *conflict
detection*, not deduplication, and it needs either a model or a person —
`MemoryConflictDetector` in `DOMAIN_MODEL.md` §6 is where it belongs. This
limitation is deliberate, and it is the price of not letting a model name the
subject of a fact.

**3. A decision is made once, and two independent things enforce it.** The
entity refuses `decided()` on anything but a pending proposal — the check that
can explain itself — and the repository's one update names `pending` in its
`WHERE` clause and reports how many rows it changed — the check that survives
two decisions racing. A check-then-write could be overtaken between the steps;
a conditional write cannot.

**There is no undo and no reopen.** Reversing an acceptance would have to decide
what becomes of a Memory that has since been read, quoted and acted upon;
reopening a rejection would mean a fact a person declined could appear anyway.
Both situations are already recoverable by ordinary means — forget the memory,
or accept the fact next time it is proposed — and neither needs a mechanism
capable of rewriting a decision.

**4. Acceptance creates exactly one Memory, in the same transaction as the
decision.** A committed acceptance with no memory would be a fact the user
believes they kept and cannot find; a memory with a pending proposal would be
knowledge nobody approved.

"Exactly one" is a **unique index on `memories.proposal_id`**, not a rule the
use case keeps. Rules can be forgotten by the next caller.

**5. A Memory is immutable and has no edit method.** Correcting one means
forgetting it and accepting a new proposal. An edit in place would keep the
identifier and the provenance while changing the fact, so the AI call it cites
would no longer be the call that produced what it now says — and the provenance
is the only thing that makes a stored claim checkable.

That also removes the need for `MemoryRevision` in this milestone: revisions
record edits, and there are none.

**6. Deletion is soft, and it is the one mutation.** `deleted_at` is a
timestamp rather than a flag, because retention has to ask "deleted before
when" and a boolean cannot answer that (the reasoning `Contact` already
follows). The repository has no `update`; it has a named `delete` with one
meaning, so no caller can reach it while intending something else.

Deleting **frees the key**, so the same fact can be accepted again. That is the
only route to a correction, and it is why the uniqueness index is partial on
`deleted_at IS NULL`.

**7. `contact_id` is resolved at acceptance, from the conversation's chat, and
is nullable only for chats with no single counterpart.** A proposal is about a
*conversation*; a memory is about a *person*. The resolution belongs at the
moment of acceptance because that is when a fact becomes something the
application believes about somebody.

Two partial unique indexes rather than one, because SQL treats NULLs as
distinct: without the second, two identical facts from group conversations
would both be stored.

**8. Provenance is `SET NULL`, not `CASCADE`, and it is all-or-nothing.** This
is the one place in the schema where SET NULL is right. `ai_calls` and
`memory_proposals` both cascade from `chats`, so without it deleting a chat
would silently erase approved memories — and a memory is user-approved
knowledge that does not stop being known because the exchange it came from was
deleted. What is lost is the trail, not the fact.

The entity therefore permits a memory with **no** trail and refuses one with
half a trail: a partial provenance is a state nothing can produce and nothing
could interpret.

**9. Confidence is kept as the model reported it.** A person accepting a fact is
saying "this is worth keeping", not "the model was certain". Raising it to 1.0
on acceptance would flatten two different claims into one and lose the one that
came from a machine.

---

### Alternatives Considered

**A model-supplied key.** The natural design — ask the model for a short label
like `home_city`, and supersession follows for free. Rejected: it hands
identity to the component this whole feature exists to distrust, and the labels
are unstable across prompt versions, so the same fact would collide with itself
after a prompt change. It is also the only rejected option that would have
*worked* for supersession, which is why the limitation in decision 2 is stated
plainly rather than buried.

**A UUID or a random key.** Rejected: it makes the uniqueness constraint
vacuous. Every fact would be distinct from every other, so accepting the same
proposal twice from two conversations would produce two memories, and §5.9's
"deduplication is tractable" would stop being true.

**A hash of the normalised value.** Same deduplication behaviour as the chosen
scheme, and rejected only for readability: a user told that two facts collided
can see why when the key is `lives in lisbon` and cannot when it is
`8f437e53`. The key is shown by `memory show`, so it is user-facing.

**The category as the key** (one fact per category per person). Rejected as too
coarse: a person has many interests.

**A `MemoryStatus` enum.** Rejected. `deleted_at IS NULL` is the whole of
"active", and a status column beside a timestamp would be two sources of truth
for one fact — the mistake ADR-056 corrected for `Conversation.is_open`.
`superseded` and `archived` are transitions, and this milestone has one.

**Hard deletion.** Rejected for now: retention needs to know what was deleted
and when, and a purge is a bulk operation belonging to Milestone 10 rather than
a repository method here.

**Editing a memory in place.** Rejected; see decision 5.

**`decided_by`.** Rejected. Every decision in this milestone is a person's —
there is no other way to make one — so the column would record a constant.
Auto-approval is the feature that gives it a second value, and it can add it.

**Storing `contact_id` on the proposal.** Rejected: a proposal is about a
conversation, and resolving the person at extraction time would make the
extractor depend on the chat graph for a field only acceptance uses.

---

### Consequences

**Positive**

- Nothing a model produces becomes believed state without a person, and now
  there is a complete path for a person to say yes or no.
- Every memory carries the provenance to check it: the proposal, the
  conversation and the AI call.
- Accepting the same fact twice is impossible, whether from one conversation or
  several.
- A decision cannot be repeated or reversed, including under concurrency.
- Deleting a chat does not silently erase approved knowledge.
- A user can forget anything the application remembers, satisfying the standing
  privacy commitment from the moment memories first exist.

**Negative**

- **Contradictions accumulate.** Nothing detects that two live memories
  disagree; the queue and the memory list will both eventually contain stale
  facts. This is the largest known gap, and conflict detection should be
  scheduled soon after retrieval.
- Correcting a memory takes two steps — forget, then accept a fresh proposal —
  and the second is only possible if the fact is proposed again.
- A memory that lost its trail cannot be audited. The alternative was losing
  the memory.
- Proposals never expire, so the queue still only grows. §5.10's ninety-day
  rule remains unimplemented.
- `importance`, `is_pinned`, decay and every retrieval-shaped field are absent,
  so nothing yet ranks or selects memories. That is Slice 9d.

---

### Related Decisions

ADR-019 (proposals keep hallucinated content out of memory — this is the other
half), ADR-058 (the queue this decides about), ADR-046 (append-only aggregates —
`Memory` extends the discipline to one that can be forgotten but not changed),
ADR-043 (composite ownership, applied to `memories`), ADR-039 (account-scoped
repositories), ADR-034 (one transaction at a time — the decision and its
consequence share it), ADR-056 (`Conversation.is_open`: the precedent for
refusing a stored flag beside a timestamp), ADR-041 (locally generated
identifiers).
Refines `DOMAIN_MODEL.md` §5.9 (the field set, the lifecycle and where the key
comes from) and §5.10 (the status set).

---

# ADR-060

## Title

Memory Retrieval Is Deterministic Before It Is Semantic

Status

Proposed

Date

2026-07-30

---

### Context

ADR-059 gave the application durable knowledge. Nothing read it. This decides
how a memory reaches a prompt, and it is the last piece before AI output starts
depending on stored state.

`VECTOR_SEARCH.md`, `AI_MODELS.md` §9 and Milestone 7 all assume the answer is
embeddings: embed each memory, embed the conversation, retrieve by cosine
similarity. That is the industry default and it is probably where this ends up.
Building it now would nonetheless be a mistake, for a reason that has nothing to
do with whether it works.

**There would be nothing to compare it against.** "Semantic retrieval improved
the suggestions" is a claim with no denominator unless something simpler was
measured first. A vector index shipped as the first implementation is a vector
index whose value is assumed rather than shown — and whose failures are
invisible, because there is no baseline behaving differently to notice them
against.

Five questions had to be settled, and none of them is about embeddings.

**What decides the order?** Anything that reads like relevance is a weighting,
and a weighting is a set of numbers somebody invented.

**What happens when the chosen memories do not fit?** A context has a token
budget, and the budget will bite long before the memory count does.

**When is a retrieval recorded?** Retrieval is a read that has a write
side-effect, which cuts against every discipline established since ADR-046.

**Does retrieval cross contacts?** A memory about one person reaching a
conversation with another is the single worst failure this feature can have.

**Is `Memory` still immutable?** ADR-059 says it is, and retrieval increments a
counter on it.

---

### Decision

**1. Retrieval is deterministic, and it is a baseline rather than an
endpoint.** Given the same memories and the same budget, the selector returns
the same memories in the same order, every time. No embeddings, no similarity,
no model.

The purpose is measurement. When semantic retrieval arrives it will be run
against the same inputs and judged against this, which is possible because this
side is pure and exhaustively pinned down by tests. It also forces the parts a
semantic version still needs and cannot borrow from a model: the token budget,
the order a context degrades in, and the record of what was left out.

**2. Ranking is lexicographic over five stored facts. No weights.**

| # | Key | Why here |
| --- | --- | --- |
| 1 | **Category priority** | Ordered by what it costs to get wrong. A `constraint` the person asked for is first, because ignoring one is worse than saying nothing. `open_question` next: the likeliest thing to need saying. Then time-sensitive facts (`plan`, `important_date`), because a durable fact is equally true next week and a plan is not. Then identity, then durable context, then how to talk to them, then what to talk about. `other` last. |
| 2 | **Importance** | A person's judgement of what is worth knowing, set when they accepted the fact. |
| 3 | **Confidence** | The model's estimate of whether it is *true*. Below importance deliberately: a machine's view of truth does not outrank a human's view of relevance, and self-reported confidence is poorly calibrated (`AI_MODELS.md` §15). |
| 4 | **Recency** | When the fact was *accepted*, newest first. |
| 5 | **Identifier** | Arbitrary but total, so ordering never depends on what the database returned. |

**Rejected: a weighted score** summing normalised keys. It reads as principled
and is not — the weights would be invented, no test could show one set beats
another, and changing one silently reorders everything. A lexicographic order is
explainable one comparison at a time: *this came first because it is a
constraint and that is a preference*.

**Rejected: recency alone.** It is the one ranking that needs no justification
and answers no question: the most recently learned fact about somebody is not
usually the most useful one, and a constraint learned a year ago outranks an
interest learned yesterday.

**Rejected: random sampling.** Cheap, unbiased, and unusable. Two runs over
unchanged data would produce different contexts, so no suggestion could be
explained by re-reading what was sent, no prompt version could be compared
against another, and no semantic retriever could ever be shown to beat it —
the comparison would be against noise. It also fails the one thing a baseline
must do: be the *same* baseline tomorrow.

**Rejected: asking a model which memories to use.** Superficially the most
capable option — a model reading the conversation could pick the relevant
facts, which is exactly what this ranking cannot do. Rejected on three counts.
It is a model call *inside* retrieval, so assembling a prompt would require a
prompt, and the cost of a suggestion would double before anything was
suggested. It is non-deterministic, so it forfeits everything above. And it
puts the decision about what the model sees in the hands of the model, which is
the inversion this project has refused at every level since ADR-019: a model
that selects its own context can quietly exclude the constraint it is about to
break.

**Rejected: retrieving inside repository queries.** A `MemoryRepository` that
ranked and budgeted while reading — returning "the memories for this context"
rather than "the memories for this contact" — would look tidier at the call
site and would be a mistake. Ranking would become untestable without a
database; the rule would live where a query planner rather than a person could
read it; and the contract suite could no longer check the two implementations
against identical behaviour, because the behaviour would include a policy the
in-memory fake would have to reimplement. The repository answers *what exists*;
the selector decides *what is used*.

**3. `last_retrieved_at` is not a ranking input.** Ranking by it would make a
retrieved memory rank higher and therefore be retrieved again — a feedback loop,
not a relevance signal. It is recorded precisely so that the *absence* of
retrieval is visible: a memory nobody has used is evidence about the ranking,
and a ranking that promoted its own past choices could never produce that
evidence.

**4. The budget is spent after ranking, and never changes it.** Ranking decides
priority; the budget decides fit. A memory too long for what remains is
**skipped and the walk continues**, so one long fact near the top does not empty
the context beneath it. Order among the selected is untouched, and every
omission is reported with its reason.

**Rejected: stopping at the first thing that does not fit.** Simpler, and wastes
most of a budget on one long memory.

**Rejected: shortening a memory to make it fit.** A truncated fact is a
different fact, and the selector has no business inventing one.

**Rejected: model-based compression.** It would put a model call inside
retrieval, which is exactly what this slice exists to avoid.

**5. Retrieval accounting happens in the same transaction as the read, and only
when the context is actually built.** Two use cases, and the difference is the
whole reason there are two:

* `BuildMemoryContext` selects **and counts**. What a prompt assembler calls.
* `GetMemoryContext` selects and counts nothing. What `tgassist memory context`
  calls, because looking at what *would* be sent is not using it — an
  inspection that inflated the counters would corrupt the measurement it exists
  to expose.

The count is incremented **in SQL** (`retrieval_count + 1`) over all selected
rows in one statement, so two contexts built at once cannot lose an increment
and a context of twenty memories costs one write. It happens **before** the
model call rather than after: the memories were used whether or not the model
then succeeded, and an accounting that depended on the outcome would
under-report exactly the expensive failures.

**6. Retrieval never crosses contacts, and the partition is strict in both
directions.** A private chat retrieves that contact's memories. A chat with no
single counterpart retrieves the memories about *nobody in particular* — the
ones extracted from group conversations — and nobody else's. A private chat does
**not** see contactless memories either: a fact from a group conversation is not
a fact about this person.

Enforced by the repository rather than the selector, so there is one place to
get it wrong. `contact_id IS NULL` rather than `= NULL`, because the second
matches nothing in SQL and would make every group chat's context permanently
empty.

**7. `Memory` is still immutable; retrieval writes bookkeeping, not the fact.**
This refines ADR-059 rather than contradicting it. What cannot change is the
*claim*: category, key, value, confidence, provenance. What can change is what
is known *about* the claim — how often it has been used. `mark_retrieved` is a
named operation with one meaning, exactly like `delete`, rather than a general
`update`.

**8. Importance is set at acceptance and not changed.** `memory accept
--importance low|normal|high|critical`, defaulting to normal. The moment
somebody is looking at a fact is the moment they can judge it, and adding a
setter now would be a second interface for a decision nobody has yet wanted to
revise. Whether it should stay immutable is an open question — a fact's
importance genuinely changes with circumstances — and it needs a real interface
to be worth answering.

**8a. The token estimate is four characters per token, and its error is
bounded and stated.** No tokeniser library: the exact count depends on the
model's own tokeniser, which is the provider's business and cannot be consulted
before the call — a budget that needed it could not be enforced.

The approximation is good enough because of what it is used for. Measured
against a byte-pair tokeniser, four characters per token **over-estimates**
ordinary English prose by roughly 5–15% (English averages nearer 4.5 characters
per token), and **under-estimates** text that tokenises badly: non-Latin
scripts, long unbroken identifiers, and heavy punctuation can reach two or three
times the estimate in the worst cases.

Two things make that acceptable. The error is *systematic* rather than random,
so the same text always costs the same and a budget decision stays reproducible.
And the budget is small relative to any model's context window — a few hundred
tokens against tens of thousands — so even a threefold under-estimate does not
approach a limit that would truncate the request. What it can do is make a
context slightly larger or smaller than intended, which is a cost question
rather than a correctness one.

The estimate is defined once, in the domain, and the scripted provider's token
accounting delegates to it — two rules of thumb that disagreed would let a
context that fitted the budget cost more than the fake charged for.

**9. The index is chosen by the query.** Retrieval issues exactly one read:
one account's live memories about one contact, newest first. So the index leads
with those columns and is partial on `deleted_at IS NULL` — a forgotten memory
should occupy no space in an index every context walks. There is no index on
importance, confidence or category: ranking happens in memory over a set already
bounded to one contact, and no index serves a five-key lexicographic order.

---

### Consequences

**Positive**

- Retrieval is observable before anything depends on it. `tgassist memory
  context` shows the selection, the order, the reason for each placement, the
  token cost and every omission — with no model involved.
- The ranking is explainable to a user one comparison at a time, and therefore
  arguable. A ranking nobody can disagree with cannot improve.
- Semantic retrieval now has something to beat, on inputs already fixed by
  tests.
- The token budget, the degradation order and the omission record are built once
  and are not embedding-specific.
- Nothing about retrieval can leak one person's memories into another's
  conversation.

**Negative**

- **There is no notion of what the conversation is about.** A context is the
  same whatever is being discussed. That is the largest limitation, and it is
  precisely what embeddings fix.
- **Category priority is a judgement.** It is defensible and it is explained,
  but it was not measured, and a real corpus may show it wrong.
- **Retrieval bias by category is now structural.** A `constraint` will almost
  always be in the context and a `shared_experience` will often be squeezed out,
  regardless of the conversation.
- **Stale facts rank as well as fresh ones.** Nothing expires, nothing decays
  and nothing detects contradictions (ADR-059), so a memory that stopped being
  true keeps its place.
- Two long facts whose first 120 characters agree collapse to one key and the
  second is refused at acceptance. Conservative, but the message does not
  explain itself.

---

### Future evolution toward embeddings

Deliberately shaped so that adding them is additive rather than a rewrite.

1. **The `MemorySelector` interface is what a semantic selector implements.**
   `select(memories) -> Selection`. A semantic version takes the same
   candidates plus a query vector and returns the same shape, so the budget,
   the omission reporting and the CLI need no change.
2. **The candidate read is already separate from the ranking.** Today
   `list_for_contact` returns everything about one contact; a vector index would
   return the top *k* by similarity, and the ranking would run on those. The
   ordering keys still apply — similarity becomes a *sixth* key or a filter
   before the first, and which of those it should be is exactly the question
   this baseline lets us answer.
3. **The measurement is already collected.** `retrieval_count` and
   `last_retrieved_at` accumulate under deterministic retrieval starting now, so
   when semantic retrieval arrives there is a before.
4. **What must not be inherited:** the temptation to rank by embedding score
   alone. Similarity answers *what is this conversation about*; it says nothing
   about what it costs to omit a constraint. The likely end state is hybrid —
   category priority first, similarity within it — and that is a decision for
   the slice that has the numbers.

---

### Related Decisions

ADR-059 (memories, and their immutability — refined here for bookkeeping),
ADR-058 (proposals, and the categories retrieval ranks by), ADR-043 (composite
ownership), ADR-034 (one transaction at a time — the read and the accounting
share it), ADR-057 (the AI boundary a context will eventually be sent through),
ADR-056 (the precedent for a pure domain rule with an application layer that
gives it identity and persistence).
Refines `DOMAIN_MODEL.md` §5.9 (`importance`, `retrieval_count`,
`last_retrieved_at`) and defers `VECTOR_SEARCH.md` in its entirety.

---

# ADR-061

## Title

Prompt Assembly Separates Context Selection from Prompt Rendering

Status

Proposed

Date

2026-07-30

---

### Context

Everything built since Slice 9a produces something nothing consumes. Memories
are extracted, reviewed, stored and retrieved; no model has ever been shown one.
This is the slice where they arrive in a prompt, and it therefore has to settle
what a prompt *is* in this application.

Five questions, and the first four are all versions of the same one: who decides
what?

**What order do the parts appear in?** The obvious answer is "whatever reads
well", which is not an answer.

**What happens when the whole thing does not fit?** Something has to go, and
whichever thing that is will be the thing this application systematically
under-uses.

**How does a reader know why a suggestion said what it said?** A prompt that is
assembled and then discarded leaves a suggestion with no explanation but its own
fluency.

**Where does the wording live?** The assembler has the data and the template has
the prose, and either could plausibly own the other.

**What does the provider know about any of this?** ADR-057 said a provider
receives text; a suggestion feature is the first thing with enough structure to
be tempted to leak some of it downward.

---

### Decision

**1. The order is fixed: system prompt, memories, conversation, task and output
format.**

| Part | Why there |
| --- | --- |
| **System prompt** | It is what everything after it is read under, and it contains no conversation data at all, so it never varies. |
| **Memories** | They are the *frame*. A constraint like "do not mention the old job" changes how every message below should be read, and a model that met the messages first has already formed a reading by the time it learns the rule. |
| **Conversation** | Delimited, untrusted, oldest to newest — bracketed by trusted rules above and the actual instruction below, so an injection attempt is surrounded rather than trailing. |
| **Task and output format** | Last, and closest to the answer. A model's final instruction is the one it follows most reliably, and it is the one that must survive an injection that got past everything above. |

**Rejected: an order chosen for readability**, and **rejected: letting the model
decide what to read first** (tool-style retrieval, or "here is everything, use
what you need"). Both make the prompt unreproducible, and an unreproducible
prompt makes every later comparison meaningless — between prompt versions,
between retrieval strategies, between deterministic and semantic retrieval.

**2. Trimming has one direction, and two things are never removed.**

**Never removed:** the system prompt, the task, the output format, and the
**most recent message**. Without the last, there is nothing to respond to, and a
"suggestion" built without it is a guess about a conversation the model cannot
see.

**First to go: the oldest messages.** In a chronological record, recency *is*
relevance: the oldest turn is the one whose absence changes the answer least.

**Then the lowest-ranked memories.** This is the ordering the goal warned
against choosing without justification, so here it is. Memories arriving at the
assembler have **already survived a budget** — retrieval ranked them and spent
its own allowance, so each one was deliberately chosen. The message history is
bulk, kept because it is cheap to keep. Dropping a curated fact about somebody
costs more than dropping a line of small talk from an hour ago.

**Rejected: dropping whichever item is largest.** Unpredictable from outside,
and it would systematically discard exactly the detailed memories most worth
having. **Rejected: truncating a memory or a message to make it fit.** A
truncated fact is a fact that was never stated. (The per-message character
limit is a different thing: a declared bound applied before assembly, and
marked in the text so the model can tell.)

**3. Attribution is required, checked, and reported.** The prompt supplies each
memory with its key; the model returns `used_memory_keys`; the application
**verifies every returned key against the keys actually supplied**. A key that
was not supplied is a fabricated attribution — the model claiming to have used
something it was never given — and it is discarded and counted rather than
shown.

The check is the same idea as extraction's evidence grounding (ADR-058): a model
cannot cite what it was not given. What it catches is narrow but real, because a
suggestion that claims grounding it does not have is worse than one that claims
none — the first invites trust.

**What attribution does not prove** is that a memory reported as used actually
influenced the text. Nothing available here can prove that. What it buys is a
reader who can ask "why did it say that?" and be shown the exact facts that were
in front of the model.

**Rejected: an opaque prompt** — assembling, sending and discarding. It would
make every suggestion unexplainable, and `tgassist chat suggest` exists largely
to make the opposite true.

**4. The assembler decides *what* and *in what order*; the template decides
*what it means and what to do about it*.** Not one imperative sentence in
`context_assembly.py` reaches a model: it emits neutral data lines
(`- [key] category: value`) and a labelled transcript. Every instruction,
constraint and output-format description is in `chat/suggestion.md`, versioned
as an asset (ADR-008).

**Rejected: a template that decides retrieval** — a prompt file with the power
to ask for more memories would make retrieval unreviewable and untestable.
**Rejected: an assembler that embeds wording** — the prompt version recorded
against a call would then be a claim about only half the text that was sent.

**5. The provider receives a rendered prompt and a schema, and nothing else.**
No `Memory`, no `Chat`, no domain type crosses the port. `StructuredAiTask`
takes text plus a `JsonSchema`; `AiProvider` takes an `AiRequest`. A provider
that knew what a memory was could not be replaced by one that did not.

**6. The one-repair loop moved into `StructuredAiTask`.** ADR-058 put it inside
`ExtractMemories`, where it was the only structured task. There are now two, and
"exactly one repair" must not be able to become "one here and two there". The
loop is shared; everything about *what* is asked stays with each caller.

**7. Two use cases, because the deterministic half is worth reading alone.**
`BuildPromptContext` retrieves and assembles and asks nothing. It is what makes
the prompt inspectable before it is paid for, and it is where a future prompt
diff will be taken. `GenerateConversationSuggestion` adds the call and the
attribution check.

**8. Nothing is stored.** A suggestion is a return value. There is no
`suggestions` table, no aggregate and no identifier, because nothing yet decides
about a suggestion — and a row nobody reads is a row nobody keeps correct. The
only thing written is the `AiCall` that `ExecuteAiTask` writes for any call.

---

### Consequences

**Positive**

- A suggestion can be explained completely: the memories supplied, the ones
  retrieval left out, the ones the budget trimmed, the transcript, the prompt
  version, the token estimate and the model's own attribution — all in one
  command, with nothing sent anywhere.
- The prompt is byte-for-byte reproducible from stored data, so a prompt version
  can be compared against the next one, and a deterministic retriever against a
  semantic one.
- Retrieval quality is now *observable*: "it suggested the wrong thing because
  the wrong memories were retrieved" is a claim somebody can check.
- The provider stays ignorant. Nothing about memories, chats or contacts has
  crossed the AI port.
- Two callers now share one repair rule, so it cannot drift.

**Negative**

- **Attribution is self-reported.** A model can say it used a memory it
  ignored, or ignore one it lists. The fabrication check catches only the case
  where the key was never supplied.
- **The token estimate is a rule of thumb.** Four characters per token
  under-counts for some languages, so a prompt that fits the budget may exceed
  the real one. Bounded, since the budget is small relative to a context window.
- **The trim order is a judgement.** It is argued rather than measured, and a
  real corpus may show that dropping an old message costs more than dropping a
  memory.
- **The prompt grows with the memory count.** Twenty memories and twenty
  messages is a large prompt for a small suggestion, and nothing yet decides
  that a particular memory is irrelevant to *this* conversation. That is
  retrieval's gap (ADR-060), and it is now visible in the cost of every call.
- **Nothing is stored**, so there is no history of what was suggested. The next
  slice needs one.

---

### Future evolution

1. **A `Suggestion` aggregate**, when there is something to decide about one.
   The moment a person can accept, edit or dismiss a suggestion, it needs an
   identifier and a row — and the shape is already fixed by what
   `GenerateConversationSuggestion` returns.
2. **More context types.** `PROMPTS.md` §8 lists user preferences, the
   conversation goal, the relationship profile, the style profile and a rolling
   summary. Each is a new slot in the same assembler, in the same fixed order,
   trimmed by the same rule. None of them changes this decision.
3. **Semantic retrieval** plugs in underneath without touching the assembler:
   it changes which memories arrive, not what happens to them.
4. **Multiple candidate suggestions** would change the schema and the CLI, not
   the assembly. The prompt already asks for one deliberately: a person choosing
   between three drafts is doing more work than a person editing one.

---

### Related Decisions

ADR-060 (retrieval, which supplies the memories and whose ranking order the
assembler preserves), ADR-058 (prompts as versioned assets, untrusted-content
delimiting, and the one-repair rule this slice generalised), ADR-057 (the AI
boundary, unchanged), ADR-020 (structured output), ADR-008 (prompts as files),
ADR-024 (the per-chat gate, inherited rather than reimplemented), ADR-034 (one
transaction at a time — retrieval, the message read and the call are three
separate ones and none spans the model call).
Implements `PROMPTS.md` §8's assembly order for the parts that exist.

---

# ADR-062

## Title

AI Suggestions Require Explicit Human Review

Status

Proposed

Date

2026-07-31

---

### Context

Slice 9e produced a working suggestion: a chat, its memories, an assembled
prompt, a model, a draft on the screen. The draft then vanished. It existed for
the length of one command, was reviewable only by whoever happened to be
watching the terminal, and left no record that it had ever been made.

That is a problem in two directions.

**Backwards**, there is no measurement. "Is the generator any good?" is answered
by counting what people kept against what they threw away, and neither number
existed. So is "did the prompt change help?" and "is retrieval picking the right
memories?" -- every question about quality needs a decision to have been
recorded.

**Forwards**, there is a temptation. The obvious next feature is sending, and
the obvious cheap way to build it is to have generation send. Every autonomous
system that has embarrassed its owner got there by exactly that increment: the
draft was good, so the review became a formality, so the review became optional,
so the review disappeared. This project has refused that inversion three times
already -- ADR-019 for memories, ADR-058 for extraction, ADR-059 for approval --
and this is the fourth and most consequential, because a wrong memory is private
and a wrong message is not.

---

### Decision

**1. Every generated suggestion is stored, and generation publishes
``SuggestionsCreated``.** A suggestion that existed only as a return value was
observable to one person for one moment. Storing it makes "observable before any
action" a property of the system rather than of who was watching.

**2. A ``Suggestion`` is an aggregate with the same shape as a
``MemoryProposal``: created pending, decided exactly once, no undo, no reopen,
no edit.** The repetition is deliberate and worth stating as a pattern rather
than a coincidence. *A model produces a candidate; a person decides; the
decision is final and recorded.* Three aggregates now follow it, and a fourth
should be suspicious of itself if it does not.

**3. Accepting executes nothing.** ``AcceptSuggestion`` changes a status,
publishes a fact, and returns. It is not a stub to be filled in: the use case is
**given nothing that could act**. No gateway, no scheduler, no provider, no
executor -- and a test asserts on its constructor signature, so wiring one in is
a change somebody has to make deliberately and defend.

The repository is shaped the same way. It has ``add``, ``get``,
``list_pending``, ``list_by_chat`` and ``decide``, and no ``execute``, ``send``
or ``schedule``. The absence of an operation is a stronger guarantee than a rule
about not calling one.

**4. Three fields for three audiences.** ``title`` for a list, ``description``
for the **person deciding**, ``payload`` for a **machine that does not exist
yet**. For a reply draft, the description *is* the draft, because that is the
thing being judged.

The split is what keeps review possible as the kinds of suggestion multiply: a
reviewer decides about a title and a description whatever ``proposal_type``
says, so a new kind needs new payload handling and **no new review**. It also
means no reviewer ever has to read JSON to decide.

**5. The queue is what has not been decided; a chat's history is everything.**
``list_pending`` excludes decided suggestions -- a queue is by definition what
is outstanding -- and ``list_by_chat`` includes them, because a listing that hid
the dismissals would make a generator that is usually wrong look like one that
is rarely used.

**6. Everything cascades from the chat.** Unlike a memory, whose provenance is
``SET NULL`` because approved knowledge outlives the exchange it came from
(ADR-059), a suggestion is a draft *about* a conversation and means nothing once
that conversation is gone.

---

### Alternatives Considered

**Automatic execution -- generation sends the reply.** Rejected, and it is the
decision this ADR exists to make. It would make the model's output an action
rather than a proposal, and every control built since Slice 9a -- the privacy
gate, schema validation, attribution checking, the review queue itself --
protects a person's *decision*. Remove the decision and they protect nothing.
The concrete failure is easy to state: the first message sent to a real person
that should not have been is not recoverable by any amount of good architecture.

**Auto-approval -- accept above a confidence threshold.** Rejected, and it is
the more tempting version because it sounds measured. Self-reported model
confidence is poorly calibrated (`AI_MODELS.md` §15), so the threshold would be
a number nobody could justify; and the suggestions it would auto-approve are
exactly the fluent, confident ones, which are also the ones a person is least
likely to catch afterwards. `DOMAIN_MODEL.md` §5.10 foresees auto-approval for
*memories* under narrow conditions; a memory is private and reversible, and a
message is neither.

**Editable suggestions.** Rejected. An edited suggestion is one whose recorded
text is not what the model produced, which destroys the only thing the record is
for: knowing what was actually suggested when somebody agreed with it. It would
also quietly corrupt the measurement -- a generator whose output is always
edited before acceptance looks, in the numbers, like a generator whose output is
always accepted. A person who wants to send something different is writing their
own message, which needs no aggregate.

**Reopening decisions.** Rejected, for the reason ADR-059 gave and one more:
here there is nothing to recover. Dismissing loses nothing (generate another),
and accepting did nothing (so changing your mind costs nothing). A reopen
mechanism would add a way to rewrite history in exchange for no capability.

**Background execution -- a worker that acts on accepted suggestions.**
Rejected for this slice and worth naming precisely, because it is the shape the
next mistake would take. The objection is not that automation is wrong; it is
that a background worker makes "accepted" mean something different from what the
person clicking accept was told it meant. If acting automatically is ever
wanted, it must be a separate, explicit, per-chat opt-in whose name says what it
does -- not a consequence of a decision made under a different description.

**Deleting dismissed suggestions.** Rejected. A record containing only what was
agreed with cannot show what the generator is getting wrong, which is the
measurement that decides whether a prompt needs rewriting.

**A table per suggestion type.** Rejected while there is one type. It would put
review behind a type switch, and review is the part that must stay uniform.
Revisit if a payload ever needs to be queried rather than read.

---

### Consequences

**Positive**

- Every suggestion the system has ever made is inspectable, alongside the AI
  call, the prompt version and the cost that produced it.
- The generator can now be *measured*: kept against thrown away, per prompt
  version.
- "Nothing acts automatically" is structural. The use cases hold nothing that
  could act, the repository offers no operation that could, and a test asserts
  both.
- A decision cannot be repeated or reversed, including under concurrency.
- Adding a second kind of suggestion needs no new review path.

**Negative**

- **The queue only grows.** Nothing expires suggestions, and a person who
  generates and never reviews accumulates rows indefinitely. Retention owns
  this, and it is now the third unbounded queue in the system.
- **`payload` is an unvalidated blob** beyond being a JSON object. The schema
  that produced it is known at generation time and not checked at read time; a
  future executor will have to validate what it finds.
- **`conversation_id` has no writer.** The column exists because the field was
  specified; generation reads a chat's recent messages rather than a segmented
  conversation, so it is always NULL today.
- **"Accepted" means only "agreed with".** A person who accepts and then does
  nothing leaves a row that looks like a completed action and was not. Until
  something acts, the status is a note to oneself.
- Three aggregates now implement the same decision semantics separately.

---

### Related Decisions

ADR-019 (proposals keep model output out of believed state), ADR-058 (extraction
proposes), ADR-059 (a decision is made once, and terminal states are terminal --
this repeats its shape deliberately), ADR-061 (the assembly and generation this
stores the output of), ADR-057 (the AI call a suggestion cites), ADR-043
(composite ownership), ADR-034 (one transaction at a time -- a decision is one),
ADR-039 (account-scoped repositories).

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
