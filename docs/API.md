# API.md

# Telegram AI Conversation Assistant

Internal API & Interface Specification

Version: 2.0

Status: Active

Last Updated: 2026-07-28

---

# 1. Purpose

This document defines the internal contracts between modules: the **ports** the domain declares and the **adapters** infrastructure supplies.

Goals:

- Decouple components
- Enable dependency injection
- Allow provider replacement
- Standardize interfaces
- Improve testability
- Support future plugins

This document does **not** define HTTP or REST APIs.

Signatures are written in Python-like pseudocode for precision. They are specifications, not implementations.

---

# 2. Design Principles

1. Every port is declared in `domain/ports/` and imports nothing outside the domain (ADR-011).
2. Ports accept and return **domain objects**, never rows, dictionaries or provider SDK types.
3. Interfaces are small and focused. A port with unrelated methods is two ports.
4. Prefer composition over inheritance.
5. Every port has at least three implementations available: **real**, **fake** (in-memory, behaviourally correct) and **mock** (assertion-focused).
6. Errors are normalized at the adapter boundary into the domain hierarchy defined in `ERROR_HANDLING.md`. No provider-specific exception escapes an adapter.
7. **All I/O methods are `async`** (ADR-013). Pure domain services are synchronous.

---

# 3. Layer Communication

```
Presentation  (Qt UI, CLI)
     │  depends on
     ▼
Application   (use cases, orchestration, composition root)
     │  depends on
     ▼
Domain        (entities, value objects, ports, pure services)
     ▲
     │  implements
Infrastructure (Telegram, persistence, AI, embeddings, config, logging, security)
```

Only infrastructure knows about Telegram, databases, AI providers and the filesystem. Only `application/container.py` may construct infrastructure classes.

---

# 4. Port Catalogue

| Category | Ports |
|---|---|
| **Persistence** | `UnitOfWork`, 27 repositories (§7–§9), `MessageSearchPort`, `MigrationRunner` |
| **Telegram** | `TelegramGateway`, `AuthorizationHandler` |
| **AI** | `LLMProvider`, `EmbeddingProvider`, `VectorStore`, `PromptRepository`, `StructuredOutputValidator` |
| **AI services** | `ConversationAnalyzer`, `EmotionAnalyzer`, `MemoryExtractor`, `RelationshipAnalyzer`, `ConversationPlanner`, `ReplyGenerator`, `ConversationSummarizer`, `UncertaintyEstimator` |
| **Cross-cutting** | `Clock`, `IdGenerator`, `EventBus`, `Cache`, `Scheduler`, `SecretStore`, `FileStore`, `NotificationPort`, `Logger` |
| **Extension** | `PluginHost`, `PluginContext` |
| **Configuration** | `ConfigurationProvider`, `SettingsPort` |

---

# 5. Cross-Cutting Ports

## 5.1 `Clock`

**Responsibility.** Supplies the current time. Injected everywhere so that time-dependent behaviour is deterministic in tests (`TESTING.md` §19). Nothing in the domain or application layer calls `datetime.now()`.

```python
class Clock(Protocol):
    def now(self) -> datetime: ...          # always UTC, timezone-aware
    def monotonic(self) -> float: ...       # for durations, never wall clock
```

Implementations: `SystemClock` (`infrastructure/clock.py`); `FixedClock` and
`AdvanceableClock` (`tests/fakes/clock.py`, per `TESTING.md` section 14 -- test
doubles are not shipped in the distributed package).

## 5.2 `IdGenerator`

**Responsibility.** Produces identifiers for entities and correlation IDs, replaceable with a deterministic sequence in tests.

```python
class IdGenerator(Protocol):
    def new_id(self) -> int: ...              # positive, time-ordered, 64-bit safe
    def new_uuid(self) -> str: ...            # canonical, time-ordered UUID string
    def new_correlation_id(self) -> str: ...
```

Identifiers are **time-ordered** (UUID version 7, ADR-033) so that inserts append
to the end of a database index rather than scattering through it. They therefore
encode their creation time and must never be used where guessing one would be a
security problem.

Implementations: `UuidV7IdGenerator` (`infrastructure/ids.py`);
`SequentialIdGenerator` (`tests/fakes/id_generator.py`).

## 5.3 `EventBus`

**Responsibility.** Decoupled publish/subscribe between application components and plugins.

```python
class EventBus(Protocol):
    async def publish(self, event: DomainEvent) -> None: ...
    def subscribe(self, event_type: type[DomainEvent], handler: EventHandler,
                  *, name: str) -> Subscription: ...
    def unsubscribe(self, subscription: Subscription) -> None: ...
```

**Delivery semantics — binding, because plugins depend on them:**

1. Delivery is **synchronous and in-process**. `publish()` returns only once every matching handler has completed; a caller that awaits it can rely on the handlers having run. The method is `async` because handlers perform I/O, not because delivery is deferred (ADR-031).
2. Events from a single publisher are delivered to a given handler **in publication order**, and handlers for one event type run in **registration order**. A handler registered for a base class also receives subclasses, after the exact-type handlers.
3. **Handler exceptions are isolated.** A raising handler is logged with its name, its failure count is incremented, and the exception never propagates to the publisher or to other handlers. This is what makes ADR-025's "a faulty plugin must not crash the application" true.
4. A handler that fails repeatedly (default 5 consecutive) is **automatically unsubscribed** and a `NotificationRaised` event is emitted.
5. Delivery is **at-most-once and non-durable**. Events are not persisted and do not survive restart. Anything requiring durability is a database write, not an event.
6. Handlers must be idempotent; no ordering is guaranteed *between* different publishers.
7. Events are **immutable**; a handler cannot modify an event observed by another handler.
8. **Handlers may be plain functions or coroutine functions.** Forcing every handler to be `async` would add ceremony without adding capability.
9. **A handler may publish**, up to a bounded depth. Beyond it the bus raises `EventDispatchError` rather than recursing without limit; that error is *not* isolated, because silently swallowing an event cycle would hide a serious defect.

Implementations: `InProcessEventBus` (`infrastructure/events/bus.py`);
`RecordingEventBus` (`tests/fakes/event_bus.py`).

## 5.4 `Cache`

**Responsibility.** Bounded, typed, in-process caching of expensive derived values (vector matrices, active goals, rendered prompts, settings).

```python
class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...
    async def invalidate(self, key: str) -> None: ...
    async def invalidate_prefix(self, prefix: str) -> None: ...
    async def clear(self) -> None: ...
    def stats(self) -> CacheStats: ...
```

Rules: caches are bounded (LRU with a configured maximum); every cached value has a documented invalidation trigger; caching is never used to hide a slow query that should be indexed.

## 5.5 `Scheduler`

**Responsibility.** Runs periodic and deferred background work (§18 of `DATABASE.md`), with lifecycle control.

```python
class Scheduler(Protocol):
    def schedule_periodic(self, job: Job, interval_seconds: int,
                          *, name: str, run_on_start: bool = False) -> JobHandle: ...
    def schedule_once(self, job: Job, run_at: datetime, *, name: str) -> JobHandle: ...
    async def start(self) -> None: ...
    async def stop(self, *, timeout_seconds: int = 30) -> None: ...
    async def pause(self, handle: JobHandle) -> None: ...
    async def resume(self, handle: JobHandle) -> None: ...
    def status(self, handle: JobHandle) -> JobStatus: ...
    def list_jobs(self) -> list[JobStatus]: ...
```

Rules: every job is cancellable and reports progress; a job never overlaps itself; job failures raise notifications rather than terminating the scheduler.

## 5.6 `SecretStore`

**Responsibility.** Storage and retrieval of secret values (ADR-021). The only component permitted to hold key material.

```python
class SecretStore(Protocol):
    async def get(self, name: str) -> SecretValue | None: ...
    async def set(self, name: str, value: SecretValue) -> None: ...
    async def delete(self, name: str) -> None: ...
    async def list_names(self) -> list[str]: ...
    async def is_available(self) -> bool: ...
```

Rules: names may be logged, values never; resolution order is environment variable → OS credential store → not configured.

`get()` returns a `SecretValue` rather than a `str` (ADR-032). A bare string cannot satisfy the rule that a secret must not appear in a `repr`, because its `repr` *is* the value. `SecretValue` masks itself in `repr`, `str` and `format`, refuses to pickle, and requires an explicit `reveal()` — which makes every disclosure point searchable.

Further rules: an unknown name returns `None` rather than raising, because "not configured" is an ordinary state; `delete()` is idempotent; a read-only backend raises `ReadOnlySecretStoreError` rather than silently discarding a write; a backend that cannot enumerate returns an empty list from `list_names()` rather than raising.

Implementations: `KeyringSecretStore`, `EnvironmentSecretStore`, `ChainedSecretStore` (`infrastructure/security/secret_store.py`); `InMemorySecretStore`, `UnavailableSecretStore` (`tests/fakes/secret_store.py`).

## 5.7 `FileStore`

**Responsibility.** Abstracts filesystem access for attachments, exports, backups and models, so paths, quotas and permissions are enforced in one place.

```python
class FileStore(Protocol):
    async def write(self, category: FileCategory, name: str, data: bytes) -> Path: ...
    async def read(self, path: Path) -> bytes: ...
    async def delete(self, path: Path) -> None: ...
    async def exists(self, path: Path) -> bool: ...
    async def size(self, path: Path) -> int: ...
    async def usage(self, category: FileCategory) -> StorageUsage: ...
    async def ensure_permissions(self, path: Path) -> None: ...
```

`FileCategory` ∈ `attachments | exports | backups | models | archives | temp`.

## 5.8 `NotificationPort`

```python
class NotificationPort(Protocol):
    async def raise_notification(self, notification: Notification) -> NotificationId: ...
    async def mark_read(self, id: NotificationId) -> None: ...
    async def dismiss(self, id: NotificationId) -> None: ...
    async def list_active(self, account_id: AccountId) -> list[Notification]: ...
```

## 5.9 `Logger`

**Responsibility.** Structured logging with mandatory context binding and central redaction (ADR-027).

```python
class Logger(Protocol):
    def bind(self, **context: Any) -> Logger: ...
    def debug(self, event: str, **fields: Any) -> None: ...
    def info(self, event: str, **fields: Any) -> None: ...
    def warning(self, event: str, **fields: Any) -> None: ...
    def error(self, event: str, exc_info: BaseException | None = None, **fields: Any) -> None: ...
    def critical(self, event: str, exc_info: BaseException | None = None, **fields: Any) -> None: ...
```

Every record carries: timestamp (UTC), level, component, event name, correlation ID, account ID. Redaction is applied by a processor before emission — never left to the call site.

---

# 6. `UnitOfWork`

**Responsibility.** Defines the transaction boundary and exposes the repositories participating in it. A use case is one unit of work.

```python
class UnitOfWork(Protocol):
    contacts: ContactRepository
    chats: ChatRepository
    conversations: ConversationRepository
    messages: MessageRepository
    memories: MemoryRepository
    memory_proposals: MemoryProposalRepository
    goals: GoalRepository
    # ... all repositories from DATABASE.md §14

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(self, *exc: Any) -> None: ...   # rollback unless commit() was called
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
    def collect_events(self) -> list[DomainEvent]: ...
```

Rules:

1. Repositories never commit. Only the unit of work does.
2. Exiting without `commit()` rolls back. There is no implicit commit: forgetting to commit loses the work loudly rather than persisting a half-finished operation quietly.
3. **Domain events are collected during the transaction and released only after a successful commit.** `collect_events()` returns nothing until the transaction commits, so publishing a rolled-back fact is not merely discouraged but impossible. Calling it twice returns the events once.
4. **Nesting is refused**; a nested `UnitOfWork` raises. Two overlapping boundaries mean neither is a boundary. A use case needing partial rollback uses `savepoint()`, which is explicit about what it does.
5. **`savepoint()`** opens a nested savepoint for partial rollback within the transaction -- the bulk-import case, where one bad record should not discard the batch. Leaving with an exception rolls back to the savepoint and re-raises; the enclosing transaction is unaffected.
6. **Transactions serialize.** One connection holds one transaction, so a second concurrent unit of work waits rather than failing (ADR-034). Acquisition is bounded, so an overlapping-transaction defect surfaces as a named error rather than a hang.
7. The fake implementation is fully in-memory and honours the same semantics, so use-case tests need no database. Both are held to one shared contract suite.

Use cases receive a **`UnitOfWorkFactory`**, not a unit of work: a use case decides when its transaction begins, and an injected open transaction would outlive the operation it was meant to bound.

---

# 7. Repository Ports — Common Contract

There is **no generic `Repository[T, ID]` interface** (ADR-035). Each aggregate
declares its own port exposing only the operations it supports, because the
aggregates do not share a lifecycle: `Message` is append-only and bulk-inserted,
`AuditEvent` has no mutation path at all, `RelationshipProfile` is upserted
whole, and `Memory` cannot be updated without simultaneously writing a revision.
A shared CRUD base would be wrong for four of those five and would break the
guarantee that an audit trail cannot be rewritten.

What every repository shares is a **contract**, enforced by a shared test suite
(`tests/support/repository_contract.py`) that every implementation runs, rather
than by inheritance:

Every repository:

1. Is account-scoped. Methods take an `AccountId` or an entity that carries one. **There is no unscoped query path.**
2. Returns domain objects or `None`; never raises for "not found" unless the method is named `get_or_raise`.
3. Excludes soft-deleted rows unless `include_deleted=True` is passed.
4. Uses keyset pagination via a `Page` object, never numeric offsets.
5. Raises typed persistence errors (`ERROR_HANDLING.md` §5), never database driver exceptions.
6. Performs no business logic — no validation beyond schema constraints, no derived values.

```python
class SortDirection(StrEnum):
    ASCENDING = "asc"
    DESCENDING = "desc"

@dataclass(frozen=True)
class SortOrder:
    field: str                      # a DOMAIN field name, never a column
    direction: SortDirection = SortDirection.DESCENDING

@dataclass(frozen=True)
class PageRequest:
    cursor: str | None = None       # opaque; belongs to the query that issued it
    limit: int = 50                 # clamped by effective_limit()
    sort: SortOrder | None = None

@dataclass(frozen=True)
class Page[T]:
    items: Sequence[T]
    next_cursor: str | None
    # has_more is derived from next_cursor
```

`SortOrder.field` is a domain field name. The repository maps it to a column,
which stops a schema rename reaching the application layer and stops a caller
ordering by a column that has no index. A request to sort by an unsupported
field is **rejected**, not ignored: ignoring it would return correctly-shaped
results in the wrong order.

**Pagination requires a unique tiebreaker.** Ordering by a non-unique column
alone silently skips rows -- three messages sharing a timestamp, a page boundary
inside that group, and `WHERE sent_at < :last` drops the other two. Every
paginated query therefore sorts by `(sort_column, unique_column)` and compares
the pair. This is enforced by `KeysetPaginator`, which cannot be constructed
without a tiebreaker, because the failure it prevents is silent.

## 7.1 Repository construction

Use cases receive `RepositoryFactory[R]` -- a callable taking a `UnitOfWork` --
as constructor parameters, and create repositories inside their transaction:

```python
class IngestMessage:
    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        messages: RepositoryFactory[MessageRepository],
        chats: RepositoryFactory[ChatRepository],
    ) -> None: ...
```

Two common alternatives are deliberately not used. Hanging repositories off the
unit of work (`uow.messages`) requires that interface to catalogue every
repository in the system and tells a reader nothing about what a use case
touches. Passing the container and asking it for repositories is a service
locator: the real dependencies vanish from the signature, which is exactly the
information a reader and a test need most.

## 7.2 Account-scoped repositories

Nearly every aggregate after Account belongs to one account. Those repositories
take `ScopedRepositoryFactory[R]` instead -- a callable taking a `UnitOfWork`
**and** an `AccountId`:

```python
ScopedRepositoryFactory = Callable[[UnitOfWork, AccountId], R_co]

class GetUserProfile:
    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        profiles: ScopedRepositoryFactory[UserProfileRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
    ) -> None: ...
```

The scope is supplied once, inside the transaction, and **no repository method
accepts an account identifier** (ADR-039). The conventional alternative --
`get(account_id)` on every method -- puts correctness in the hands of every
call site forever, and its failure mode is silently returning or overwriting
another account's data. Removing the parameter removes the mistake: there is no
value left to get wrong.

Writes are checked as well as reads. The scope makes a cross-account read
impossible, but a caller could still hand over an entity built for another
account, which would overwrite the wrong row; every write verifies the entity's
`account_id` against the scope and raises `DomainValidationError` otherwise.

The contract suite asserts this structurally, by inspecting the signatures, so a
future method that reintroduces an account parameter fails a test rather than a
review.

---

# 8. Core Repository Ports

## `ContactRepository`

*Implemented in Milestone 1.3.*

```python
class ContactRepository(Protocol):
    @property
    def account_id() -> AccountId
    async def add(contact: Contact) -> None
    async def get(contact_id: ContactId, *, include_deleted: bool = False) -> Contact | None
    async def get_by_telegram_id(
        telegram_user_id: TelegramUserId, *, include_deleted: bool = False
    ) -> Contact | None
    async def list_contacts(
        request: PageRequest, *, include_archived: bool = False
    ) -> Page[Contact]
    async def update(contact: Contact) -> None
```

Scoped at construction (§7.2), so no method takes an account.

Five operations, each with a caller. Four methods from the version 1.0 sketch
are **not** implemented:

- `get_by_username` and `search` — nothing looks a contact up by handle or by
  free text yet, and each implies an index whose shape should be chosen by the
  query that needs it. `search` is also dialect-specific, which is why message
  search is a separate port (`MessageSearchPort`).
- `soft_delete` — deletion is `update` with `Contact.deleted` applied. The rule
  for what may be deleted belongs to the entity, and a repository method would
  be a second place for it to be wrong. The same argument removes `archive` and
  `restore`.
- `purge` — removing a Contact must also remove every Memory, Proposal, Goal,
  Relationship Profile, Style Profile and Suggestion referencing it. None of
  those tables exists; a partial version would appear to work while leaving
  orphans. Milestone 11 owns it.

`include_deleted` and `include_archived` are separate flags because the two
states hide differently. Archived means "not in my way": excluded from the
listing, still returned by `get`, so restore can find it. Deleted means "gone":
excluded from both, unless a caller says otherwise. Creation is the caller that
says otherwise — a soft-deleted row still holds `(account_id, telegram_user_id)`,
so looking only at live contacts would report a constraint violation instead of
the truth, which is that this person is already known and was deleted.

`purge()` removes every row referencing the contact across all tables in one transaction and returns counts per table — the operation behind a contact's erasure request (`PRIVACY.md` §7).

## `ChatRepository`

*Implemented in Milestone 1.4.*

```python
class ChatRepository(Protocol):
    @property
    def account_id() -> AccountId
    async def add(chat: Chat) -> None
    async def get(chat_id: ChatId) -> Chat | None
    async def get_by_telegram_id(telegram_chat_id: TelegramChatId) -> Chat | None
    async def get_private_with(contact_id: ContactId) -> Chat | None
    async def list_chats(request: PageRequest) -> Page[Chat]
    async def update(chat: Chat) -> None
```

Scoped at construction (§7.2), so no method takes an account.

`get_private_with` is the graph traversal in the direction the application reads
it: from a person to the conversation with them. It returns a single chat rather
than a page because a contact has at most one private chat, enforced by a partial
unique index.

Four methods from the version 1.0 sketch are **not** implemented:

- `list_by_activity` — orders by `last_message_at`, which is written by
  ingestion and does not exist yet. `list_chats` orders by `created_at` until
  then, and the recency index arrives with the column.
- `list_sync_enabled` — synchronisation will want it, and it should be added
  with the query that needs it so its index can be chosen and measured rather
  than guessed.
- `set_ai_processing_mode` — a field-specific write where `update` already
  serves. The rules for what a chat may become belong to the entity, and a
  repository method deciding one would be a second place for them to be wrong.
- `purge` — removing a chat must also remove its messages, conversations,
  summaries, suggestions and attachments. None of those tables exists. Milestone
  11 owns it; deletion today happens by cascade from the account or the contact.

There is no `delete` either. A chat is not something a user removes: it exists
because a conversation exists in Telegram. What they control is `sync_enabled`
and `ai_processing_mode` (ADR-044).

## `ConversationRepository`

*Implemented in Milestone 3.0. Scoped per account at construction (ADR-039), so
no method takes an account identifier.*

```python
class ConversationRepository(Protocol):
    @property
    def account_id() -> AccountId
    async def get(conversation_id: ConversationId) -> Conversation | None
    async def list_by_chat(chat_id: ChatId, request: PageRequest) -> Page[Conversation]
    async def list_from(chat_id: ChatId,
                        started_at: datetime | None = None) -> tuple[Conversation, ...]
    async def latest_before(chat_id: ChatId, instant: datetime) -> Conversation | None
    async def add(conversation: Conversation) -> None
    async def update(conversation: Conversation) -> None
    async def delete(conversation_id: ConversationId) -> None
```

**`get_open` and `close` are gone.** Whether a conversation may still grow
depends on how long ago it ended -- on *now* -- so it is a question asked of an
entity with an instant, not a column to query or a transition to record
(ADR-056). "The newest conversation in this chat" is the first page of
`list_by_chat`.

**`list_from` and `latest_before` are new**, and both exist for the segmentation
pass: the first reads the window it is about to rewrite, the second tells it
where that window starts. A pass that opened its window mid-conversation would
see its first message with nothing before it, call it a boundary, and split an
episode at whatever instant the caller named.

**`delete` exists here and on no other repository.** Every other aggregate
records something somebody decided; a Conversation records something this
application computed from messages that are still there, so a stale one is a
wrong answer rather than history.

## `MessageRepository`

*Implemented in Milestone 1.5.*

```python
class MessageRepository(Protocol):
    @property
    def account_id() -> AccountId
    async def add(message: Message) -> None
    async def get(message_id: MessageId) -> Message | None
    async def get_by_telegram_id(
        chat_id: ChatId, telegram_message_id: TelegramMessageId
    ) -> Message | None
    async def list_by_chat(chat_id: ChatId, request: PageRequest) -> Page[Message]
```

Scoped at construction (§7.2), and **append-only**: no `update`, no `delete`, no
`soft_delete`. A message is the immutable factual record everything else is
derived from, and the absence of those methods is what makes that a property of
the code rather than a sentence in a document (ADR-046). A test asserts the
absence on the port and on both implementations.

`get_by_telegram_id` takes the chat because a Telegram message identifier is
unique only within one.

Five methods from the version 1.0 sketch are **not** implemented:

- `add_batch` — batching is the *pipeline's* concern, not the repository's.
  `IngestMessages` takes a sequence, resolves each against
  `get_by_telegram_id`, and writes in one transaction. A repository method
  deciding what counts as a duplicate would put the idempotency rule in two
  places.
- `update` — see above.
- `list_by_conversation` — Conversation does not exist (ADR-044).
- `list_for_metrics` — response-time metrics are Milestone 9, and the index
  serving them should be chosen by the measured query.
- `list_recent` with a `before` cursor — replaced by `list_by_chat` taking a
  `PageRequest`, so message history uses the same keyset pagination as
  everything else rather than a bespoke cursor.

## Ingestion

*Implemented in Milestone 1.5.*

```python
@dataclass(frozen=True)
class IncomingMessage:
    sender_kind: SenderKind
    sent_at: datetime
    text: str | None = None
    message_type: MessageType = MessageType.TEXT
    telegram_message_id: int | None = None

class IngestMessages:
    async def execute(
        chat_id: int,
        incoming: Sequence[IncomingMessage],
        *,
        account_id: AccountId | None = None,
    ) -> IngestionReport
```

`IncomingMessage` is deliberately **not** a `Message`: a source knows what
arrived, not what local identifier it will be given or when this application
stored it. Keeping the two apart stops a caller inventing either.

There is no `MessageSource` port. There is one source; a protocol with one
implementation is an interface designed against a guess. Synchronisation will
construct `IncomingMessage` values exactly as the CLI does, and if a third
source then fits neither, the abstraction can be extracted from two real
examples.

The batch is validated **whole before anything is written**, so a batch
containing one malformed message is refused entirely — a guarantee that does not
depend on the store rolling back. Repeats are reported as `skipped` rather than
raised, because a backfill overlapping live updates is the ordinary case rather
than an error.
async def mark_deleted_remotely(account_id: AccountId, message_id: MessageId) -> None
async def count(account_id: AccountId, chat_id: ChatId) -> int
```

`add_batch()` is idempotent on `(account_id, chat_id, telegram_message_id)`, returning inserted and skipped counts — the property that makes backfill resumable.

`list_for_metrics()` returns a projection (sender, timestamps, length) rather than full messages, so metric computation over 100,000 messages does not materialise 100,000 objects.

## `MemoryRepository`

```python
async def add(memory: Memory) -> Memory
async def update(memory: Memory, revision: MemoryRevision) -> Memory   # atomic: value + revision
async def get(account_id: AccountId, memory_id: MemoryId) -> Memory | None
async def find_by_key(account_id: AccountId, contact_id: ContactId,
                      category: str, key: str) -> Memory | None
async def list_by_contact(account_id: AccountId, contact_id: ContactId, *,
                          categories: Sequence[str] | None, include_deleted: bool = False
                          ) -> list[Memory]
async def list_candidates(account_id: AccountId, contact_id: ContactId,
                          limit: int) -> list[Memory]     # ranking candidates
async def list_pinned(account_id: AccountId, contact_id: ContactId) -> list[Memory]
async def record_retrieval(memory_ids: Sequence[MemoryId], at: datetime) -> None
async def soft_delete(account_id: AccountId, memory_id: MemoryId) -> None
async def hard_delete(account_id: AccountId, memory_id: MemoryId) -> None
async def revisions(account_id: AccountId, memory_id: MemoryId) -> list[MemoryRevision]
```

`update()` requires a revision — the interface makes `DOMAIN_MODEL.md` invariant 10 ("memory values are never overwritten without a revision") structurally unavoidable rather than a convention.

## `MemoryProposalRepository`

```python
async def add_batch(proposals: Sequence[MemoryProposal]) -> list[MemoryProposal]
async def get(account_id: AccountId, proposal_id: ProposalId) -> MemoryProposal | None
async def list_pending(account_id: AccountId, *, contact_id: ContactId | None,
                       cursor: str | None, limit: int) -> Page[MemoryProposal]
async def list_rejected_keys(account_id: AccountId, contact_id: ContactId) -> list[tuple[str, str]]
async def set_status(account_id: AccountId, proposal_id: ProposalId, status: ProposalStatus,
                     *, decided_at: datetime, reason: str | None = None) -> None
async def expire_older_than(account_id: AccountId, cutoff: datetime) -> int
async def count_pending(account_id: AccountId) -> int
```

`list_rejected_keys()` lets the extractor avoid re-proposing facts the user has already declined (ADR-019 §4).

## `GoalRepository`

```python
async def add(goal: Goal) -> Goal
async def update(goal: Goal) -> Goal
async def get(account_id: AccountId, goal_id: GoalId) -> Goal | None
async def get_active(account_id: AccountId, contact_id: ContactId) -> Goal | None
async def list_by_contact(account_id: AccountId, contact_id: ContactId,
                          *, include_deleted: bool = False) -> list[Goal]
async def set_status(account_id: AccountId, goal_id: GoalId, status: GoalStatus) -> None
async def soft_delete(account_id: AccountId, goal_id: GoalId) -> None
```

Activating a goal deactivates any currently active goal for that contact, in one transaction — enforcing the one-active-goal invariant.

---

# 9. Supporting Repository Ports

Condensed; all follow the §7 common contract.

| Repository | Key methods |
|---|---|
| `AccountRepository` | `add`, `get`, `get_by_telegram_id`, `get_active`, `list_accounts`, `set_active` — **implemented**, see below |
| `UserProfileRepository` | `get`, `upsert`, `update_preferences` |
| `SessionRepository` | `get`, `add`, `update` — **implemented**, see below |
| `AttachmentRepository` | `add`, `list_by_message`, `mark_downloaded`, `delete`, `total_size` |
| `RelationshipRepository` | `get`, `upsert`, `list_stale` |
| `StyleProfileRepository` | `get`, `upsert`, `list_stale` |
| `SummaryRepository` | `add`, `get_current`, `history`, `supersede`, `delete` |
| `PlanRepository` | `add`, `get_latest`, `mark_stale` |
| `SuggestionRepository` | `add`, `get`, `list_by_conversation`, `set_status`, `list_for_evaluation` |
| `BehaviorRepository` | `add`, `get_latest` |
| `AnalysisRepository` | `add`, `get_cached`, `invalidate_by_prompt_version`, `invalidate_by_subject` |
| `EmbeddingRepository` | `upsert`, `get`, `delete_by_owner`, `list_by_model`, `list_stale` |
| `SyncCursorRepository` | `get`, `add`, `update`, `save` — **implemented**, Milestone 2.8. `upsert` is called `save`; `reset` is `save` with a fresh cursor, so no chat is ever left with messages and no bookmark; `record_failure` awaits the backoff policy that would read it (ADR-054) |
| `NotificationRepository` | `add`, `list_active`, `mark_read`, `dismiss`, `purge_old` |
| `AIProviderRepository` | `list_enabled`, `get_default_for`, `upsert`, `set_enabled`, `update_capabilities` |
| `AICallRepository` | `add`, `aggregate_cost`, `list_recent`, `purge_older_than` |
| `PluginRepository` | `list`, `get`, `upsert`, `set_enabled`, `record_error` |
| `SettingsRepository` | `get`, `get_all`, `set`, `delete` |
| `RetentionPolicyRepository` | `list`, `upsert`, `delete`, `record_applied` |
| `AuditRepository` | `append`, `list` — **no update or delete methods exist** |

`AuditRepository` deliberately omits mutation methods. Append-only is expressed in the interface, not merely in policy.

## `AccountRepository`

```python
class AccountRepository(Protocol):
    async def add(account: Account) -> None
    async def get(account_id: AccountId) -> Account | None
    async def get_by_telegram_id(telegram_user_id: TelegramUserId) -> Account | None
    async def get_active() -> Account | None
    async def list_accounts(request: PageRequest) -> Page[Account]
    async def set_active(account_id: AccountId, now: datetime) -> Account
```

Six operations, each traceable to a caller that exists. There is no `update`,
`delete`, `exists` or `count`: a method with no caller has no test, no measured
query and no index.

Deletion is deliberately absent. Removing an Account must remove everything it
owns, transactionally and across every table — the purge in `PRIVACY.md` §7.
A partial version now would appear to work while leaving orphans in tables that
do not exist yet. Milestone 11 owns it.

Account is the ownership root, so unlike every other repository this one is
**not** account-scoped: there is no outer scope to scope it to.

`set_active` deactivates before activating, because the partial unique index
permits only one active row and the reverse order would violate it
mid-statement. Both writes happen in the caller's transaction, so the invariant
is never briefly broken and never left broken by a failure between them.

## `UserProfileRepository`

```python
class UserProfileRepository(Protocol):
    @property
    def account_id() -> AccountId
    async def get() -> UserProfile | None
    async def add(profile: UserProfile) -> None
    async def update(profile: UserProfile) -> None
```

Scoped at construction (§7.2), so no method takes an account.

Three operations. There is no `delete`: the profile is removed by cascade when
its account is, and a second route to deletion could leave the two disagreeing
about whether the row exists. There is no `list` or pagination: the repository
holds exactly one row, so a collection interface would describe something that
cannot happen.

`get` returns `None` for an account with no profile yet. That is an ordinary
state rather than an error -- the profile is created with defaults on first
access, so adding an account does not require deciding preferences before the
application is usable.

`update` raises `RecordNotFoundError` when no row was affected rather than
silently succeeding, which is what an `UPDATE` matching nothing otherwise does.
It never rewrites `created_at`: a profile that appeared to have been created
when it was last edited would make its own history unreadable.

## `MessageSearchPort`

Separated from `MessageRepository` because the implementation is dialect-specific (FTS5 vs `tsvector`, ADR-016 §4).

```python
class MessageSearchPort(Protocol):
    async def index(self, message: Message) -> None: ...
    async def remove(self, message_id: MessageId) -> None: ...
    async def search(self, account_id: AccountId, query: str, *,
                     chat_id: ChatId | None, limit: int) -> list[MessageSearchHit]: ...
    async def rebuild(self, account_id: AccountId) -> None: ...
```

## `MigrationRunner`

```python
class MigrationRunner(Protocol):
    async def status(self) -> SchemaStatus: ...          # never modifies the database
    async def current_revision(self) -> str | None: ...
    def head_revision(self) -> str: ...
    async def upgrade(self, target: str = "head", *, backup_first: bool = True) -> MigrationReport: ...
    async def downgrade(self, target: str) -> MigrationReport: ...
    def set_pre_upgrade_hook(self, hook: PreUpgradeHook | None) -> None: ...
```

`SchemaStatus.state` is one of `empty`, `current`, `behind`, `ahead`, `unknown`. The last two mean the database was written by a newer application; startup refuses, because a migration that dropped a column cannot restore what it discarded.

`upgrade()` runs the registered pre-upgrade hook first and refuses to proceed if it fails. **No hook is registered until Milestone 11** implements backups; until then the report records `backup_taken=False` and a warning is logged, which is honest rather than claiming a safety net that does not exist.

## 9.2 `Database`

**Responsibility.** Connection lifecycle and health.

```python
class Database(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...
    async def health(self) -> HealthReport: ...
```

Rules: `connect()` and `close()` are idempotent; connection settings are **verified after connecting** rather than assumed; `health()` **never raises**, because it is the call a caller makes precisely to find out whether something is wrong.

---

## 9.1 `SessionRepository`

*Implemented in Milestone 2.4.*

Scoped to one account at construction (ADR-039), like every repository over
account-owned data. One row per account, so there is nothing to page and nothing
to look up by.

```python
class SessionRepository(Protocol):
    @property
    def account_id(self) -> AccountId: ...

    async def get(self) -> Session | None
    async def add(self, session: Session) -> None
    async def update(self, session: Session) -> None
```

**Constraints:**

1. **No `delete`.** A session goes with its account, by cascade. Logging out is
   a *transition* — it destroys the local store and the key and leaves a record
   saying so — because "this account was signed out" is a fact a deleted row
   cannot express.
2. **It never holds key material.** `Session.encryption_key_ref` is a name in
   the `SecretStore`; the key lives in the operating system credential store
   (`SECURITY.md` §7).
3. `get()` returning `None` is an ordinary state, not an error: a session record
   is written when a login is first prepared, not when the account is created.
4. `add` and `update` take the whole entity, so the invariants checked at
   construction are the invariants written. Handing over a session belonging to
   another account raises rather than overwriting the wrong row.

## 9.2 `PrepareSession`

*Implemented in Milestone 2.4. Application layer, not a port.*

Gives an account the storage and key a login will need, and the only thing that
puts a session key into the credential store.

```python
async def execute(self, account_id: AccountId | None = None) -> Session
```

**Constraints:**

1. **Idempotent.** An account that already has a session gets the existing one
   back. Generating a second key would make the store the first key encrypted
   permanently unreadable.
2. The key is written to the credential store **before** the row, and the
   transaction commits after both. The other order would allow a row naming a
   key that was never stored, which looks like a working session until the first
   login fails. The reverse leftover costs nothing: the name is derived from the
   account, so the next attempt overwrites it.
3. Raises `SecretStoreUnavailableError` when no credential backend is
   available — regardless of `security.require_secret_store`, which governs
   startup. There is nowhere else a session key may go; an unencrypted fallback
   does not exist (`SECURITY.md` §8).
4. It does not create the session directory. TDLib creates its own store, under
   a root that already carries owner-only permissions.

---

# 10. Telegram

## 10.0 `TdjsonClient`

*Implemented in Milestone 2.3. Infrastructure, not a port.*

The seam between TDLib's blocking receive call and the event loop (ADR-048). It
is **not** a domain port and has no second implementation: what varies is the
native library beneath it, and that is where the protocol sits.

```python
class TdjsonClient:
    def __init__(self, library: NativeLibrary, *, queue_capacity: int = 1024,
                 receive_timeout: float = 1.0, shutdown_timeout: float = 10.0) -> None

    @property
    def state(self) -> ClientState                       # stopped/starting/running/stopping/failed

    async def start(self) -> None
    def send(self, request: dict[str, Any]) -> None      # fire and forget
    async def request(self, payload: dict[str, Any], *,
                      timeout: float | None = 30.0) -> dict[str, Any]
    async def receive(self) -> dict[str, Any] | None     # None once the stream ends
    async def close(self) -> None
    def health(self) -> ClientHealth
```

**Thread ownership.** `tgassist-td` calls `td_receive` and nothing else ever
does — asserted by test. The loop calls `td_send`, which TDLib guarantees is
thread-safe, so only receipt needs a thread.

**Constraints:**

1. Contains no Telegram vocabulary. It moves JSON objects; what they mean is the
   gateway's business (§10.1).
2. `send` is synchronous because `td_send` is. Making it `async` would suggest
   it waits for something.
3. `request` generates its own `@extra` and replaces any the caller supplied,
   because correlation is the client's registry to own.
4. `receive` returning `None` is the only end-of-stream signal, and it is
   carried by an event rather than a queued sentinel — a bounded queue that is
   full cannot deliver one.
5. Restart is not supported. A closed client stays closed.
6. Only `@type` is logged, never a frame body.

## 10.1 `TelegramGateway`

**Responsibility.** The sole boundary to Telegram. Converts between platform structures and domain objects. Contains no business logic and no persistence.

**Declared one slice at a time (ADR-051).** The signature below is the full shape; the methods marked *implemented* are the ones that exist today. A protocol listing methods no caller uses cannot be verified by a contract suite, and a fake would have to invent behaviour for them.

```python
class TelegramGateway(Protocol):
    @property
    def account_id(self) -> AccountId: ...                                    # implemented

    # Lifecycle
    async def connect(self) -> None: ...                                      # implemented
    async def disconnect(self) -> None: ...                                   # implemented
    async def is_connected(self) -> bool: ...                                 # implemented

    # Authorization (drives the Session state machine)
    async def authorization_state(self) -> AuthorizationState: ...            # implemented
    async def connection_state(self) -> ConnectionState: ...                  # implemented
    async def start_authorization(self, handler: AuthorizationHandler) -> None: ...  # implemented
    async def logout(self) -> None: ...                                       # implemented

    # Reading
    async def get_me(self) -> TelegramUser: ...                               # implemented
    async def list_chats(
        self, *, limit: int = 200
    ) -> tuple[TelegramChatInfo, ...]: ...                                    # implemented
    async def get_chat(
        self, chat_id: TelegramChatId
    ) -> TelegramChatInfo | None: ...                                         # implemented
    async def fetch_history(
        self, chat_id: TelegramChatId, *,
        before_message_id: TelegramMessageId | None = None,
        limit: int = 100,
    ) -> HistoryPage: ...                                                     # implemented
    async def get_contact(
        self, user_id: TelegramUserId
    ) -> TelegramUser | None: ...                                             # implemented
    async def list_contacts(
        self, *, limit: int = 1000
    ) -> tuple[TelegramUser, ...]: ...                                        # implemented

    # Updates
    def updates(self) -> AsyncIterator[TelegramUpdate]: ...              # implemented

    # Writing — the only send path in the system
    async def send_message(self, chat_id: TelegramChatId, text: str,
                           *, reply_to: TelegramMessageId | None) -> TelegramMessage: ...
    async def edit_message(self, chat_id: TelegramChatId,
                           message_id: TelegramMessageId, text: str) -> TelegramMessage: ...
    async def delete_message(self, chat_id: TelegramChatId,
                             message_id: TelegramMessageId, *, for_everyone: bool) -> None: ...
    async def mark_read(self, chat_id: TelegramChatId,
                        up_to_message_id: TelegramMessageId) -> None: ...

    # Media
    async def download_file(self, remote_file_id: str, *, max_bytes: int) -> Path: ...
```

**`TelegramUpdate` variants:** `NewMessage`, `MessageEdited`, `MessageDeleted`, `ChatUpdated`, `UserStatusChanged`, `ConnectionStateChanged`, `AuthorizationStateChanged`.

**`fetch_history` replaces `iter_history`.** Version 1.0 specified an
`AsyncIterator[TelegramMessage]`, which cannot express *where to continue from*:
a caller draining an iterator has no cursor to store, so a backfill interrupted
part-way could not resume without re-reading. A page that carries its own
boundary can (`TELEGRAM_ARCHITECTURE.md` §2.4).

**`HistoryPage`** carries `messages` (newest first) and `oldest_message_id`.
`reached_beginning` is the single question a backfill asks, and it is true only
for an **empty** page — a short page is not proof, because Telegram returns short
pages for reasons of its own. That is the whole reason the boundary is returned
rather than derived.

**`updates()` is fed by the dispatch loop, not by a second consumer of the
receive stream.** The gateway already owns the only consumer of
`TdjsonClient.receive()` (ADR-051); the loop maps `updateNewMessage` into a
`NewMessage` and offers it to a bounded queue that `updates()` drains. The
stream begins filling at `connect()`, before anything consumes it, which is why
a run cannot lose an update to its own start-up (ADR-055).

One consumer at a time, enforced rather than documented: the queue holds one
item per update, so a second iterator would take turns rather than see a copy.

**`get_contact` and `list_contacts` are new** relative to version 1.0, and
they are two calls rather than one because they answer over two different
populations. The chat list holds people this account has never saved; the
address book holds people it has never messaged. Neither set contains the other,
so neither can be derived from the other (ADR-053).

A chat carries its counterpart's *name* but not their handle, which is why
`get_contact` exists at all: it is what supplies `Contact.username`.

**`connection_state()` is new** relative to version 1.0 of this document. ADR-049 gave Session two independent axes, and a caller recording both cannot derive the second from `is_connected()` — which answers "can I use it", not "which of the five states is it in".

**Constraints:**

1. Never writes to the database. Ingest is the application layer's job.
2. Handles rate limiting internally: `FLOOD_WAIT` produces bounded exponential backoff and a `RateLimited` domain error only if the wait exceeds the configured ceiling. *Not yet: `errors.is_flood_wait` recognises the condition, and absorbing it belongs with the code that issues enough requests to cause one.*
3. Reconnects automatically with backoff and emits `ConnectionStateChanged`.
4. `fetch_history()` returns one page and its boundary, never a whole chat. A page rather than a stream because a caller draining an iterator has no cursor to store, and resumption is the point.
5. **There is no method for sending typing indicators.** Its absence is the structural expression of ADR-023 §2, and a contract test asserts that no method with `typing` or `action` in its name exists on either implementation.
6. Bound to one account at construction (ADR-039). No method takes an account identifier.
7. `connect()` and `disconnect()` are idempotent; operations needing a connection raise `TdlibNotRunningError` rather than opening one implicitly. A method that silently opens a network connection is a method whose cost is invisible.

## `AuthorizationHandler`

**Responsibility.** Lets the presentation layer supply credentials during the multi-step login flow without the gateway depending on any UI.

*Implemented in Milestone 2.5.*

```python
class AuthorizationHandler(Protocol):
    async def request_phone_number(self) -> str: ...
    async def request_code(self, hint: CodeHint) -> str: ...
    async def request_password(self, hint: PasswordHint) -> str: ...
    async def on_state_change(self, state: AuthorizationState) -> None: ...
    async def on_error(self, error: Exception) -> RetryDecision: ...
```

Codes and passwords are passed through and never logged, stored or retained after use.

**Constraints:**

1. Nothing here stores what it returns. `ConsoleAuthorizationHandler` has two slots — an attempt counter and its limit — so there is nowhere a credential could survive, and a test asserts that.
2. `on_state_change` must not block on user input. It runs on the gateway's path, so a handler that waited there would stop every other update.
3. `on_error` receives the *reason* Telegram gave, never the value that was rejected. `AuthorizationError` carries `operation`, `telegram_code` and `telegram_message` and nothing else.
4. `on_error` takes `Exception` rather than `AuthorizationError`: the parameter is what the handler is shown, and narrowing it would force the gateway to construct a specific type before it knows the failure is recoverable.
5. Cancellation propagates, so shutdown is not delayed by a prompt nobody is answering.

**`CodeHint`** carries `delivery`, `length` and `timeout_seconds` — where the code was sent and how long it lasts, never the code. **`PasswordHint`** carries the user's own reminder text and a redacted recovery address, never the password.

## 10.3 `AuthenticateAccount` and `LogOutAccount`

*Implemented in Milestone 2.5. Application layer, not ports.*

```python
async def execute(self, gateway: TelegramGateway, handler: AuthorizationHandler,
                  account_id: AccountId | None = None) -> LoginResult
async def execute(self, gateway: TelegramGateway,
                  account_id: AccountId | None = None) -> Session | None
```

The gateway is a **parameter**, not a constructor dependency: it holds a live connection, and a use case built once per call has no lifetime to hang that on (`TELEGRAM_ARCHITECTURE.md` §7.3).

**Constraints:**

1. Login prepares the session first. The gateway cannot connect without the store path and the encryption key, and `PrepareSession` is what creates them.
2. **A login that authenticated as a different Telegram user is refused, not recorded.** The account already owns chats, contacts and messages belonging to the first person, and there is no way to unmix two histories.
3. Both session axes are written from what the gateway reports. An authorized account whose connection is still catching up is an ordinary state, and recording it as fully ready would be a lie the next command believes.
4. `LoginResult.was_already_authorized` distinguishes a restored session from a fresh sign-in, rather than implying one happened.
5. Logout tells Telegram, records the transition, then destroys the local store and the key — in that order, so a failure part-way leaves nothing usable rather than something half-usable. Removing the directory tolerates failure; deleting the key is what makes the remaining bytes unreadable.

---

# 11. AI Ports

## 11.1 `AiProvider`

**Responsibility.** The sole boundary to a language model. Builds the vendor's request, parses its response, and normalises its failures. Contains no business logic, no persistence and no privacy decisions.

**Declared one slice at a time (ADR-051).** Two members, because a capability is a property of the *model* rather than a class of client: one endpoint answers a completion, a classification and a structured extraction, and splitting the port would mean several adapters over one endpoint (ADR-057 §1).

```python
@runtime_checkable
class AiProvider(Protocol):
    @property
    def model(self) -> AiModel: ...                              # implemented

    async def generate(self, request: AiRequest) -> AiResponse: ...   # implemented
```

```python
@dataclass(frozen=True, slots=True)
class AiRequest:
    content: str                      # untrusted -- never joined into instructions
    prompt: PromptVersion
    task_kind: str
    instructions: str | None = None   # the system prompt, if the task has one
    max_output_tokens: int = 4096
    temperature: float = 0.0          # deterministic by default
    timeout_seconds: float = 60.0

@dataclass(frozen=True, slots=True)
class AiResponse:
    text: str
    finish_reason: FinishReason       # stop | length | content_filter | other
    usage: TokenUsage                 # input/output counts, None where unreported
    model: AiModel                    # what actually answered, which may be a dated snapshot
```

Rules:

1. `model.data_boundary` is consulted before every call. An `external` model is never invoked for a chat whose `ai_processing_mode` is `local_only` or `disabled`, nor for content that names no chat at all (ADR-024, ADR-057 §3).
2. `content` is untrusted and is placed in the user turn. An adapter that joined it into `instructions` would make prompt injection a system-prompt override (`AI_MODELS.md` §12).
3. Provider exceptions are normalized to `AiTimeoutError`, `AiRateLimitedError`, `AiProviderError` and `AiResponseError` (`ERROR_HANDLING.md` §6). A caller never sees a vendor's exception type.
4. `usage` fields are `None` when the vendor did not report them. `None` is *unreported*; zero would be a claim that a call was free.
5. The returned `model` is what answered, not what was asked for -- a vendor may route an alias to a dated snapshot, and an expensive call has to be traceable to the exact model that made it.
6. Timeouts are applied by the **caller**, not trusted to the adapter, so a provider that ignores its own timeout cannot hang the application.
7. Every call is instrumented into `ai_calls`, including failures and refusals.

**Implementations.** `AnthropicProvider` (over an injectable `HttpTransport`, so the adapter is exercised in full without a socket) and `ScriptedAiProvider` (shipped, deterministic, the default; ADR-057 §8).

**Not yet declared.** Streaming, token counting, health checks, capability discovery and embeddings. Each will be declared by the slice that has a caller for it. An embedding is not text, and no honest signature covers both.

## 11.1.1 `ExecuteAiTask`

The only place a model is invoked. Every AI feature calls this and inherits the privacy gate, the timeout, the cost accounting and the audit record (ADR-057 §3).

```python
class ExecuteAiTask:
    async def execute(
        self,
        *,
        content: str,
        prompt: PromptVersion,
        task_kind: str,
        instructions: str | None = None,
        chat_id: int | None = None,
        account_id: AccountId | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        timeout_seconds: float | None = None,
    ) -> AiTaskResult: ...
```

It **interprets nothing**. No parsing, no schema validation, no repair. Those belong to the task that knows what shape it asked for; put here, every task's schema would land in the one component every task shares.

The gate, in full:

| chat mode | local model | external model |
| --- | --- | --- |
| `disabled` | refused | refused |
| `local_only` | allowed | refused |
| `cloud_allowed` | allowed | allowed |
| *no chat named* | allowed | **refused** |

A refusal is recorded as an `AiCall` with outcome `refused` before the error is raised, and the error names the record. An audit that contained only the calls that were allowed could not show that a call was blocked.

## 11.1.2 `AiCallRepository`

```python
class AiCallRepository(Protocol):
    @property
    def account_id(self) -> AccountId: ...

    async def add(self, call: AiCall) -> None: ...
    async def get(self, call_id: AiCallId) -> AiCall | None: ...
    async def list_recent(self, request: PageRequest) -> Page[AiCall]: ...
```

No `update`, no `delete`. The absence of both is the append-only guarantee (ADR-057 §5), the same discipline `MessageRepository` has.

## 11.1.3 `HttpTransport`

```python
class HttpTransport(Protocol):
    async def send(self, request: HttpRequest) -> HttpResponse: ...
```

An infrastructure seam, not a domain port. It exists so the real vendor adapter can be tested in full -- request body, headers, response parsing, stop-reason mapping, usage -- without a network. The shipped implementation is `UrllibTransport`, stdlib `urllib` on a worker thread; no HTTP dependency is added (ADR-057 §11).

## 11.2 Structured output

Not a port: a pure domain service (`domain/services/structured_output.py`), because validating a shape needs nothing but the shape.

```python
def validate(text: str, schema: JsonSchema) -> ValidationOutcome: ...
def build_repair_prompt(text: str, violations: Sequence[str]) -> str: ...
def require_supported(definition: Mapping[str, Any], *, schema_id: str) -> None: ...
def parse(text: str) -> Any | None: ...
```

Applied to **every** model response regardless of how it was obtained. One repair attempt; a second failure raises `SchemaViolationError` (ADR-020 §4).

Rules:

1. **Shape only.** Required fields, types, enumerations, numeric ranges, lengths. It has no opinion about whether a confidence is high enough or a fact is worth storing -- that is the calling use case's policy, and a validator that knew about it would change every time a feature did (ADR-058 §5).
2. **A deliberately small subset of JSON Schema.** `SUPPORTED_KEYWORDS` is what is implemented; `require_supported()` refuses anything else **when a schema is loaded**, so an unimplemented keyword is a startup failure rather than a constraint that silently passes.
3. `parse()` tolerates exactly one habit -- a Markdown code fence -- and repairs nothing else. No quote fixing, no trailing-comma removal, no scanning for the first brace: a guess that parses is worse than a failure that does not.
4. There is **no partial result.** An invalid payload returns `None` with its violations; a caller that could reach a half-valid payload would eventually use one.
5. `build_repair_prompt()` returns the model's own answer to it with the violations named, and forbids adding content -- a repair that changed the facts would be a second, unreviewed extraction.

## 11.3 `EmbeddingProvider`

Deliberately separated from `VectorStore`: computing vectors and storing/searching them are independently replaceable concerns.

```python
class EmbeddingProvider(Protocol):
    def model(self) -> EmbeddingModelInfo: ...       # provider, name, dimension, normalization
    def data_boundary(self) -> DataBoundary: ...
    async def embed(self, text: str) -> Vector: ...
    async def embed_batch(self, texts: Sequence[str]) -> list[Vector]: ...
    async def health_check(self) -> HealthStatus: ...
```

## 11.4 `VectorStore`

```python
class VectorStore(Protocol):
    async def upsert(self, account_id: AccountId, owner: EmbeddingOwner,
                     vector: Vector, model_id: int, fingerprint: str) -> None: ...
    async def delete(self, owner: EmbeddingOwner) -> None: ...
    async def search(self, account_id: AccountId, query: Vector, *,
                     owner_kind: str, filter: VectorFilter | None,
                     top_k: int, min_score: float) -> list[VectorHit]: ...
    async def rebuild(self, account_id: AccountId, model_id: int) -> RebuildReport: ...
    async def stats(self, account_id: AccountId) -> VectorStoreStats: ...
```

Rules: vectors from different models are never compared; `search()` filters by model automatically; results carry a similarity score used by `MemoryRanker` alongside recency and importance (`VECTOR_SEARCH.md` §5).

## 11.5 `PromptRegistry`

**Declared one slice at a time (ADR-051).** Two members, because two is what has a caller.

```python
@runtime_checkable
class PromptRegistry(Protocol):
    def get(self, prompt_id: str) -> Prompt: ...                    # implemented
    def schema_for(self, prompt_id: str) -> JsonSchema | None: ...  # implemented
```

Rendering is **on the `Prompt`**, not on the registry: it is a pure function of a template and its variables, and putting it in the domain is what lets it be tested without a filesystem.

```python
@dataclass(frozen=True, slots=True)
class Prompt:
    id: str
    version: str            # from the file's front matter -- the only place it lives
    purpose: str
    inputs: tuple[str, ...]
    untrusted: tuple[str, ...]
    schema_id: str | None
    body: str

    def render(self, variables: Mapping[str, str]) -> RenderedPrompt: ...
    @property
    def version_ref(self) -> PromptVersion: ...
```

Rules:

1. `render()` **raises** on a missing declared variable rather than substituting empty text. A prompt silently missing its context section produces fluent, confident, ungrounded output (ADR-026 §3).
2. It also raises on a variable the prompt does not declare, and a `Prompt` whose declared inputs disagree with the placeholders in its body cannot be constructed at all. Both directions, so a template and its declaration cannot drift apart unnoticed.
3. **Untrusted content is delimited and neutralised by `render()`**, not by the template. Any run of three or more angle brackets is collapsed to two before insertion, so no message can forge a boundary (`SECURITY.md` §12, ADR-058 §4).
4. Everything is validated **when the registry loads**, at startup, and a mismatch is a fatal `PromptRegistryInvalidError`. By the time a caller can reach `get()`, every prompt is known to parse, to declare what it uses, and to name a schema that exists and is checkable.
5. **Immutable once loaded.** No reload, no setter: the version recorded against a model call has to be a claim about the text that was actually sent.

**Not yet declared.** `list_all()` (no caller until a diagnostic command wants one) and `validate_registry()` (loading *is* validating, so a separate method would be a second way to do the same thing).

**Implementation.** `FilePromptRegistry`, over `tgassist/prompts/` -- inside the package rather than at the repository root, because a prompt outside the wheel is a prompt an installed application does not have (ADR-058 §3).

## 11.5.1 `MemoryProposalRepository`

```python
class MemoryProposalRepository(Protocol):
    @property
    def account_id(self) -> AccountId: ...

    async def add(self, proposal: MemoryProposal) -> None: ...
    async def get(self, proposal_id: MemoryProposalId) -> MemoryProposal | None: ...
    async def list_recent(self, request: PageRequest) -> Page[MemoryProposal]: ...
    async def list_for_conversation(self, conversation_id: ConversationId) -> tuple[MemoryProposal, ...]: ...
```

`decide()` is the **one** mutation, and it is a named method with its restriction in the signature rather than a general `update`:

```python
    async def decide(
        self, proposal_id: MemoryProposalId, status: ProposalStatus, now: datetime
    ) -> bool: ...
```

It names `pending` in its `WHERE` clause and reports whether it changed a row, so a second decision — or two racing — cannot both succeed. Nothing returns a proposal to `pending`, so a decision cannot be undone (ADR-059 §3).

`list_for_conversation()` returns **every** proposal, including rejected ones: a rejected proposal is kept precisely so the same fact is not offered again.

## 11.5.1a `MemoryRepository`

```python
class MemoryRepository(Protocol):
    @property
    def account_id(self) -> AccountId: ...

    async def add(self, memory: Memory) -> None: ...
    async def get(self, memory_id: MemoryId) -> Memory | None: ...
    async def get_by_proposal(self, proposal_id: MemoryProposalId) -> Memory | None: ...
    async def list_active(self, request: PageRequest) -> Page[Memory]: ...
    async def delete(self, memory_id: MemoryId, now: datetime) -> bool: ...
```

No `update`. A memory is immutable: correcting one means forgetting it and accepting a new proposal, because an edit in place would keep the provenance while changing the fact (ADR-059 §5).

`delete()` is soft — a timestamp, not a removal — and is a *named* operation with one meaning, so no caller can reach it while intending something else. It returns whether a live memory was deleted, so "forgotten now" and "was already forgotten" are distinguishable without a second query, and deleting twice is not an error.

`get()` returns deleted memories; `list_active()` does not. "Show me what you deleted" is a question a person is entitled to ask of their own data; "what do you know about me" is not answered by something you told it to forget.

```python
    async def list_for_contact(self, contact_id: ContactId | None, *, limit: int) -> tuple[Memory, ...]: ...
    async def mark_retrieved(self, memory_ids: Sequence[MemoryId], now: datetime) -> int: ...
```

`list_for_contact()` is the retrieval read and **never crosses contacts**. `None` means the memories about *nobody in particular* — the ones from conversations with no single counterpart — and not "everybody": a group chat sees only those, and a private chat only that person's (ADR-060 §6). Live memories only.

`mark_retrieved()` is the second exception to "no update", and the same kind as `delete()`: a named operation with one meaning. It changes *bookkeeping about* a memory, not the fact, which is why a memory stays immutable while its counters move. One statement over the whole selection, incremented in SQL so concurrent contexts cannot lose a count.

There is still no `search` and no vector operation. Semantic retrieval is a later slice and will need a port shaped by an embedding index rather than by this one.

## 11.5.1b Review use cases

```python
class AcceptMemoryProposal:
    async def execute(self, proposal_id: int, *, account_id: AccountId | None = None) -> AcceptanceResult: ...

class RejectMemoryProposal:
    async def execute(self, proposal_id: int, *, account_id: AccountId | None = None) -> MemoryProposal: ...

class DeleteMemory:
    async def execute(self, memory_id: int, *, account_id: AccountId | None = None) -> bool: ...
```

Rules:

1. **One transaction.** The decision and the memory it creates are the same event: a committed acceptance with no memory would be a fact the user believes they kept and cannot find.
2. **Exactly one memory per acceptance**, enforced by a unique index rather than by these classes.
3. **Rejection creates nothing** and keeps the proposal, so the extractor does not offer the same fact again.
4. **Neither decision can be repeated or reversed.** An already-accepted proposal is refused with the identifier of the memory it produced — more useful than a bare no.
5. The contact a memory is about is resolved here, from the conversation's chat. `None` for a chat with no single counterpart.
6. None of these is `async` for the sake of a model: **deciding needs no AI**, which is the point of having separated extraction from review.

## 11.5.1c `MemorySelector` and the context use cases

Ranking is a **pure domain service** (`domain/services/memory_selection.py`): no repository, no clock, no model.

```python
def rank(memories: Sequence[Memory]) -> tuple[Memory, ...]: ...

class MemorySelector:
    def select(self, memories: Sequence[Memory]) -> Selection: ...
```

Five ordering keys, lexicographic, no weights: **category priority → importance → confidence → recency → identifier** (ADR-060 §2). Every one is a stored fact; none is a tuned number. The budget is spent *after* ranking and never changes it — what does not fit is skipped and the walk continues, and every omission is reported with a reason.

```python
class GetMemoryContext:
    async def execute(self, chat_id: int, *, account_id: AccountId | None = None) -> MemoryContext: ...

class BuildMemoryContext(GetMemoryContext):
    ...
```

The difference is one thing: `BuildMemoryContext` records the retrieval against the memories it selected, in the same transaction as the read; `GetMemoryContext` records nothing. Two names rather than a flag, because a caller choosing between `build(record=False)` and `build(record=True)` has to know which is which.

Rules:

1. **No model is called.** Retrieval happens before generation and is inspectable on its own.
2. **Contact scope is the repository's job**, not the selector's, so there is one place to get it wrong.
3. A truncated candidate set, an over-budget omission and an empty context are three different things, and `MemoryContext` says which.
4. `MemoryContext.why(memory)` explains a placement in one line — a selection nobody can read is one nobody can disagree with.

## 11.5.1d `ContextAssembler` and the suggestion use cases

Assembly is a **pure domain service** (`domain/services/context_assembly.py`).

```python
class ContextAssembler:
    def assemble(self, memories: Sequence[Memory], messages: Sequence[Message]) -> PromptContext: ...
```

`PromptContext` carries what will be sent (`render_memories()`,
`render_conversation()`), what the budget removed and why, the token estimate,
and `memory_keys` — the keys supplied, against which attribution is checked.

Rules:

1. **It writes no prose.** The assembler decides *what* is included and *in what
   order*; the prompt file decides *what it means and what to do about it*
   (ADR-061 §4). Not one imperative sentence in the service reaches a model.
2. The order is fixed: system prompt → memories → conversation → task and output
   format.
3. Trimming removes the oldest messages first, then the lowest-ranked memories.
   The system prompt, the task, the format and the most recent message are never
   removed, and **nothing is ever shortened to fit**.
4. Memories are **neutralised but not delimited**; the conversation is delimited
   by `Prompt.render`, which owns the markers (ADR-058 §4).

```python
class BuildPromptContext:
    async def execute(self, chat_id: int, *, account_id: AccountId | None = None) -> AssembledPrompt: ...

class GenerateConversationSuggestion:
    async def execute(self, chat_id: int, *, account_id: AccountId | None = None) -> Suggestion: ...
```

`BuildPromptContext` is the deterministic half and asks nothing — it is what
makes a prompt inspectable before it is paid for. `GenerateConversationSuggestion`
adds the call, through `StructuredAiTask`, and the attribution check.

**Nothing is sent and nothing is stored.** A `Suggestion` is a return value:
there is no table, no aggregate and no identifier, because nothing yet decides
about one. The only write is the `AiCall` that `ExecuteAiTask` records.

## 11.5.1e `StructuredAiTask`

```python
class StructuredAiTask:
    async def execute(self, *, content: str, instructions: str | None, prompt: PromptVersion,
                      task_kind: str, schema: JsonSchema,
                      chat_id: int | None = None, account_id: AccountId | None = None) -> StructuredAnswer: ...
```

Wraps `ExecuteAiTask` with the one rule every structured task shares: validate,
and on failure hand the model its own answer back **exactly once** (ADR-020 §4).
It exists because there are now two such tasks and that rule must not be able to
become "one repair here and two there" (ADR-061 §6). The gate, the timeout, the
accounting and the audit record all still belong to `ExecuteAiTask`.

## 11.5.2 `ExtractMemories`

```python
class ExtractMemories:
    async def execute(
        self, conversation_id: int, *, account_id: AccountId | None = None
    ) -> ExtractionReport: ...
```

The pipeline: load the conversation -> render the prompt -> `ExecuteAiTask` -> validate (one repair) -> filter -> persist -> publish `MemoryProposalsCreated`.

Rules:

1. **No proposal bypasses validation**, and no validated candidate bypasses the three filters: grounded evidence, confidence threshold, not already proposed.
2. The model supplies four fields. Identifier, timestamp, conversation, AI call, prompt version and status are assigned here.
3. Every discard is **counted** in the report, never silently dropped.
4. The model call sits inside **no** transaction (ADR-034, ADR-058 §10).
5. The privacy gate is inherited from `ExecuteAiTask`, not reimplemented.

## 11.5.1f `SuggestionRepository` and the review use cases

**Milestone 10b.** The review queue: every generated suggestion is stored, and
none of them does anything until a person decides about it (ADR-062).

```python
class SuggestionRepository(Protocol):
    async def add(self, suggestion: Suggestion) -> Suggestion: ...
    async def get(self, suggestion_id: SuggestionId) -> Suggestion | None: ...
    async def list_pending(self, page: PageRequest) -> Page[Suggestion]: ...
    async def list_by_chat(self, chat_id: ChatId, page: PageRequest) -> Page[Suggestion]: ...
    async def decide(
        self, suggestion_id: SuggestionId, status: SuggestionStatus, now: datetime
    ) -> bool: ...
```

**There is no `update`, and no `execute`, `send` or `schedule`.** The repository
owns exactly one mutation — `pending` to a terminal state — and `decide` is
conditional (`WHERE status = 'pending'`), returning `False` when the row was
already decided. The absence of an operation is a stronger guarantee than a rule
about not calling one, and a contract test asserts the port declares nothing
else.

`list_pending` excludes decided suggestions; `list_by_chat` includes them.

**Use cases.**

| Use case | Signature | Notes |
|---|---|---|
| `AcceptSuggestion` | `execute(suggestion_id, *, account_id=None) -> Suggestion` | Records agreement. **Executes nothing** — it is given no gateway and no scheduler, and a test asserts on its constructor. Publishes `SuggestionAccepted`. |
| `DismissSuggestion` | `execute(suggestion_id, *, account_id=None) -> Suggestion` | Keeps the row as dismissed; deletes nothing. Publishes `SuggestionDismissed`. |
| `GetSuggestion` | `execute(suggestion_id, *, account_id=None) -> Suggestion \| None` | |
| `ListSuggestions` | `execute(page, *, chat_id=None, account_id=None) -> Page[Suggestion]` | Without `chat_id`, the queue; with it, that chat's history including decided ones. |

A second decision raises `SuggestionAlreadyDecided`, which names the decision
that was already made and when. The guarantee is enforced twice: the entity
refuses, and the conditional write survives a race the entity cannot see.

## 11.6 AI Service Ports

Each service is a separate port (ADR-006). Several may be satisfied by one batched implementation (ADR-029).

```python
class ConversationAnalyzer(Protocol):
    async def analyze(self, context: ConversationContext) -> ConversationAnalysis: ...

class EmotionAnalyzer(Protocol):
    async def detect(self, subject: EmotionSubject) -> EmotionAssessment: ...

class MemoryExtractor(Protocol):
    async def extract(self, context: ConversationContext,
                      known: Sequence[Memory],
                      rejected_keys: Sequence[tuple[str, str]]) -> list[MemoryProposal]: ...

class RelationshipAnalyzer(Protocol):
    async def analyze(self, contact_id: ContactId, window: TimeWindow) -> RelationshipProfile: ...

class ConversationPlanner(Protocol):
    async def plan(self, context: ConversationContext, goal: Goal | None) -> ConversationPlan: ...

class ReplyGenerator(Protocol):
    async def generate(self, context: ConversationContext,
                       plan: ConversationPlan | None) -> ReplySuggestion: ...

class ConversationSummarizer(Protocol):
    async def summarize(self, conversation: Conversation,
                        messages: Sequence[Message],
                        previous: ConversationSummary | None) -> ConversationSummary: ...

class UncertaintyEstimator(Protocol):
    def estimate(self, context: ConversationContext,
                 model_confidence: Confidence,
                 signals: UncertaintySignals) -> ConfidenceAssessment: ...
```

Note: `RelationshipAnalyzer` and `UncertaintyEstimator` are **deterministic** implementations by default (ADR-029 §3) — `UncertaintyEstimator` is synchronous precisely because it performs no I/O.

**`ReplyGenerator` and `ConversationPlanner` have no reference to `TelegramGateway`.** They cannot send, by construction (ADR-023 §5).

---

# 12. Configuration Ports

```python
class ConfigurationProvider(Protocol):
    def get(self) -> AppConfig: ...              # typed, validated, immutable
    def reload(self) -> ReloadResult: ...
    def validate(self) -> ValidationReport: ...
    def resolved_sources(self) -> dict[str, str]: ...   # key → origin, for `config show`

class SettingsPort(Protocol):
    async def get(self, account_id: AccountId, key: str) -> Any | None: ...
    async def set(self, account_id: AccountId, key: str, value: Any) -> None: ...
    async def get_all(self, account_id: AccountId) -> dict[str, Any]: ...
    async def reset(self, account_id: AccountId, key: str) -> None: ...
```

Configuration is immutable at runtime; settings are mutable and emit `SettingChanged` (ADR-028 §4).

---

# 13. Plugin Ports

```python
class PluginHost(Protocol):
    async def discover(self) -> list[PluginInfo]: ...
    async def load(self, plugin_name: str) -> LoadResult: ...
    async def unload(self, plugin_name: str) -> None: ...
    async def enable(self, plugin_name: str) -> None: ...
    async def disable(self, plugin_name: str) -> None: ...
    def api_version(self) -> str: ...
    def loaded(self) -> list[PluginInfo]: ...

class PluginContext(Protocol):
    """The only surface a plugin may use. Plugins never touch the database."""
    def logger(self) -> Logger: ...
    def event_bus(self) -> EventBus: ...
    def config(self) -> Mapping[str, Any]: ...
    def storage(self) -> PluginStorage: ...                 # namespaced key/value
    def register_llm_provider(self, provider: LLMProvider) -> None: ...
    def register_ui_panel(self, panel: UIPanelSpec) -> None: ...
    def register_command(self, command: CommandSpec) -> None: ...
    def register_job(self, job: Job, interval_seconds: int) -> None: ...
```

Hook specifications, lifecycle and the trust model are defined in `PLUGIN_SYSTEM.md`.

---

# 14. Application Use Cases

Use cases orchestrate ports. Each has a single `execute()` and defines one transaction boundary.

| Use case | Purpose |
|---|---|
| `AuthenticateAccount` | Drives the authorization state machine to `ready` |
| `SyncChatHistory` | Resumable backfill using sync cursors |
| `IngestMessage` | Persists a message, segments the conversation, advances the cursor, emits events |
| `HandleMessageEdited` / `HandleMessageDeleted` | Applies remote mutations per the deletion policy |
| `AnalyzeConversation` | Produces or reuses cached analyses |
| `SummarizeConversation` | Creates or supersedes a summary |
| `ExtractMemoryProposals` | Produces proposals; never writes memories |
| `ReviewMemoryProposal` | Approves or rejects, creating memory and revision atomically |
| `EditMemory` / `ForgetMemory` | User-initiated memory changes |
| `RecomputeRelationship` / `RecomputeStyleProfile` | Deterministic metric refresh |
| `SetGoal` | Creates or activates a goal, deactivating the previous one |
| `GenerateSuggestion` | Assembles context, plans, generates, calibrates confidence, persists |
| `AcceptSuggestion` / `DismissSuggestion` | **Implemented**, Milestone 10b. Decide about a generated suggestion. Records the decision and **acts on nothing** (§11.5.1f) |
| `GetSuggestion` / `ListSuggestions` | **Implemented**, Milestone 10b. Read the review queue, or one chat's history |
| `RecommendTiming` | Deterministic behaviour advice |
| `SendMessage` | **The only send path.** Requires approved suggestion or user text |
| `SearchMessages` / `SearchMemories` | Retrieval |
| `ExportData` / `ImportData` | Portability |
| `PurgeContact` / `PurgeChat` / `DeleteAccountData` | Erasure |
| `CreateBackup` / `RestoreBackup` | Backup lifecycle |
| `ApplyRetentionPolicies` | Scheduled retention |
| `RunMigrations` | Startup and manual migration |
| `CheckProviders` | Capability discovery and verification |

---

# 15. Error Handling

Every port declares its failure modes. No implementation exposes provider-specific exceptions. The complete hierarchy, retry policies and timeout policies are in `ERROR_HANDLING.md`.

Summary of the top-level families:

| Family | Examples | Typical handling |
|---|---|---|
| `DomainError` | `InvariantViolationError`, `InvalidStateTransitionError` | Bug — log, surface, never retry |
| `PersistenceError` | `ConstraintViolationError`, `RecordNotFoundError`, `DatabaseUnavailableError` | Depends; constraint violations are usually bugs |
| `TelegramError` | `AuthorizationRequiredError`, `RateLimitedError`, `ChatNotFoundError`, `NetworkUnavailableError` | Retry with backoff where marked retryable |
| `AIProviderError` | `ProviderUnavailableError`, `ProviderRateLimitedError`, `ContextTooLongError`, `SchemaViolationError`, `ContentFilteredError` | Retry, fall back, or degrade |
| `ConfigurationError` | `MissingRequiredSettingError`, `InvalidConfigurationValueError`, `UnknownConfigurationKeyError` | Fatal at startup |
| `PluginError` | `IncompatibleApiVersionError`, `PluginCrashedError` | Isolate and disable |

---

# 16. Versioning

Ports are versioned with the application. Breaking changes require: a version bump, migration notes in `CHANGELOG.md`, updates to every implementation and fake, and an ADR when the change is architectural.

The **plugin API** carries its own semantic version, independent of the application version, because third parties depend on it (ADR-025 §6).

---

# 17. Testing Requirements

1. Every port has a **fake** implementation in `tests/fakes/`, behaviourally correct and usable without I/O.
2. Every port has a **contract test suite** run against all implementations — real and fake — so fakes cannot drift.
3. Adapters have integration tests against real dependencies, marked `integration` and excluded from the default run.
4. `LLMProvider` adapters run a **conformance suite**: the same prompts and schemas against every provider, verifying capability negotiation, schema validation, repair and error normalization.
5. Use cases are tested entirely against fakes: no database, no network, no model.
6. Architectural tests assert that no domain module imports outside the domain, that `ReplyGenerator` and `BehaviorRuleEngine` have no gateway dependency, and that `AuditRepository` exposes no mutation method.

---

# 18. API Design Rules

Interfaces define behavior, not implementation.

Business logic must never depend on external SDKs.

Infrastructure implements interfaces. Application coordinates interfaces. Domain defines contracts.

This separation ensures that Telegram, AI providers, storage engines, and plugins can all be replaced independently without changing the application's core behavior.
