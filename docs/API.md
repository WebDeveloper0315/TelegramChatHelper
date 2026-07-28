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
2. Exiting without `commit()` rolls back. There is no implicit commit.
3. **Domain events are collected during the transaction and published only after a successful commit.** Publishing inside the transaction would announce facts that may be rolled back.
4. Nesting is not supported; a nested `UnitOfWork` is a design error and raises.
5. The fake implementation is fully in-memory and honours the same commit/rollback semantics, so use-case tests need no database.

---

# 7. Repository Ports — Common Contract

Every repository:

1. Is account-scoped. Methods take an `AccountId` or an entity that carries one. **There is no unscoped query path.**
2. Returns domain objects or `None`; never raises for "not found" unless the method is named `get_or_raise`.
3. Excludes soft-deleted rows unless `include_deleted=True` is passed.
4. Uses keyset pagination via a `Page` object, never numeric offsets.
5. Raises typed persistence errors (`ERROR_HANDLING.md` §5), never database driver exceptions.
6. Performs no business logic — no validation beyond schema constraints, no derived values.

```python
@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None
    has_more: bool
```

---

# 8. Core Repository Ports

## `ContactRepository`

```python
async def add(contact: Contact) -> Contact
async def update(contact: Contact) -> Contact
async def get(account_id: AccountId, contact_id: ContactId) -> Contact | None
async def get_by_telegram_id(account_id: AccountId, tg_id: TelegramUserId) -> Contact | None
async def get_by_username(account_id: AccountId, username: str) -> Contact | None
async def list(account_id: AccountId, *, cursor: str | None, limit: int,
               include_deleted: bool = False) -> Page[Contact]
async def search(account_id: AccountId, query: str, limit: int) -> list[Contact]
async def soft_delete(account_id: AccountId, contact_id: ContactId) -> None
async def purge(account_id: AccountId, contact_id: ContactId) -> PurgeReport
```

`purge()` removes every row referencing the contact across all tables in one transaction and returns counts per table — the operation behind a contact's erasure request (`PRIVACY.md` §7).

## `ChatRepository`

```python
async def add(chat: Chat) -> Chat
async def update(chat: Chat) -> Chat
async def get(account_id: AccountId, chat_id: ChatId) -> Chat | None
async def get_by_telegram_id(account_id: AccountId, tg_chat_id: TelegramChatId) -> Chat | None
async def list_by_activity(account_id: AccountId, *, cursor: str | None, limit: int) -> Page[Chat]
async def list_sync_enabled(account_id: AccountId) -> list[Chat]
async def set_ai_processing_mode(account_id: AccountId, chat_id: ChatId,
                                 mode: AIProcessingMode) -> None
async def purge(account_id: AccountId, chat_id: ChatId) -> PurgeReport
```

## `ConversationRepository`

```python
async def add(conversation: Conversation) -> Conversation
async def update(conversation: Conversation) -> Conversation
async def get(account_id: AccountId, conversation_id: ConversationId) -> Conversation | None
async def get_open(account_id: AccountId, chat_id: ChatId) -> Conversation | None
async def list_by_chat(account_id: AccountId, chat_id: ChatId, *,
                       cursor: str | None, limit: int) -> Page[Conversation]
async def close(account_id: AccountId, conversation_id: ConversationId, ended_at: datetime) -> None
```

## `MessageRepository`

```python
async def add(message: Message) -> Message
async def add_batch(messages: Sequence[Message]) -> BatchResult   # idempotent bulk ingest
async def update(message: Message) -> Message
async def get(account_id: AccountId, message_id: MessageId) -> Message | None
async def get_by_telegram_id(account_id: AccountId, chat_id: ChatId,
                             tg_message_id: TelegramMessageId) -> Message | None
async def list_recent(account_id: AccountId, chat_id: ChatId, *,
                      before: datetime | None, limit: int) -> Page[Message]
async def list_by_conversation(account_id: AccountId,
                               conversation_id: ConversationId) -> list[Message]
async def list_for_metrics(account_id: AccountId, chat_id: ChatId,
                           window: TimeWindow) -> list[MessageMetricRow]
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
| `AccountRepository` | `add`, `get`, `get_active`, `update`, `list`, `purge` |
| `UserProfileRepository` | `get`, `upsert`, `update_preferences` |
| `SessionRepository` | `get`, `upsert`, `set_state`, `clear` |
| `AttachmentRepository` | `add`, `list_by_message`, `mark_downloaded`, `delete`, `total_size` |
| `RelationshipRepository` | `get`, `upsert`, `list_stale` |
| `StyleProfileRepository` | `get`, `upsert`, `list_stale` |
| `SummaryRepository` | `add`, `get_current`, `history`, `supersede`, `delete` |
| `PlanRepository` | `add`, `get_latest`, `mark_stale` |
| `SuggestionRepository` | `add`, `get`, `list_by_conversation`, `set_status`, `list_for_evaluation` |
| `BehaviorRepository` | `add`, `get_latest` |
| `AnalysisRepository` | `add`, `get_cached`, `invalidate_by_prompt_version`, `invalidate_by_subject` |
| `EmbeddingRepository` | `upsert`, `get`, `delete_by_owner`, `list_by_model`, `list_stale` |
| `SyncCursorRepository` | `get`, `upsert`, `record_failure`, `reset` |
| `NotificationRepository` | `add`, `list_active`, `mark_read`, `dismiss`, `purge_old` |
| `AIProviderRepository` | `list_enabled`, `get_default_for`, `upsert`, `set_enabled`, `update_capabilities` |
| `AICallRepository` | `add`, `aggregate_cost`, `list_recent`, `purge_older_than` |
| `PluginRepository` | `list`, `get`, `upsert`, `set_enabled`, `record_error` |
| `SettingsRepository` | `get`, `get_all`, `set`, `delete` |
| `RetentionPolicyRepository` | `list`, `upsert`, `delete`, `record_applied` |
| `AuditRepository` | `append`, `list` — **no update or delete methods exist** |

`AuditRepository` deliberately omits mutation methods. Append-only is expressed in the interface, not merely in policy.

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
    async def current_revision(self) -> str | None: ...
    async def head_revision(self) -> str: ...
    async def pending(self) -> list[MigrationInfo]: ...
    async def upgrade(self, target: str = "head", *, backup_first: bool = True) -> MigrationReport: ...
    async def downgrade(self, target: str) -> MigrationReport: ...
    async def verify(self) -> IntegrityReport: ...
```

`upgrade()` backs up by default and restores automatically on failure (`DATABASE.md` §7.6).

---

# 10. `TelegramGateway`

**Responsibility.** The sole boundary to Telegram. Converts between platform structures and domain objects. Contains no business logic and no persistence.

```python
class TelegramGateway(Protocol):
    # Lifecycle
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def is_connected(self) -> bool: ...

    # Authorization (drives the Session state machine)
    async def authorization_state(self) -> AuthorizationState: ...
    async def start_authorization(self, handler: AuthorizationHandler) -> None: ...
    async def logout(self) -> None: ...

    # Reading
    async def get_me(self) -> TelegramUser: ...
    async def list_chats(self, *, limit: int) -> list[TelegramChatInfo]: ...
    async def get_chat(self, chat_id: TelegramChatId) -> TelegramChatInfo | None: ...
    async def get_contact(self, user_id: TelegramUserId) -> TelegramUser | None: ...
    async def iter_history(self, chat_id: TelegramChatId, *,
                           before_message_id: TelegramMessageId | None,
                           limit: int) -> AsyncIterator[TelegramMessage]: ...

    # Updates
    def updates(self) -> AsyncIterator[TelegramUpdate]: ...

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

**Constraints:**

1. Never writes to the database. Ingest is the application layer's job.
2. Handles rate limiting internally: `FLOOD_WAIT` produces bounded exponential backoff and a `RateLimited` domain error only if the wait exceeds the configured ceiling.
3. Reconnects automatically with backoff and emits `ConnectionStateChanged`.
4. `iter_history()` streams, never materialising a whole chat.
5. **There is no method for sending typing indicators.** Its absence is the structural expression of ADR-023 §2.

## `AuthorizationHandler`

**Responsibility.** Lets the presentation layer supply credentials during the multi-step login flow without the gateway depending on any UI.

```python
class AuthorizationHandler(Protocol):
    async def request_phone_number(self) -> str: ...
    async def request_code(self, hint: CodeHint) -> str: ...
    async def request_password(self, hint: PasswordHint) -> str: ...
    async def on_state_change(self, state: AuthorizationState) -> None: ...
    async def on_error(self, error: AuthorizationError) -> RetryDecision: ...
```

Codes and passwords are passed through and never logged, stored or retained after use.

---

# 11. AI Ports

## 11.1 `LLMProvider`

```python
class Capability(Enum):
    JSON_SCHEMA = "json_schema"
    TOOL_CALLING = "tool_calling"
    STREAMING = "streaming"
    SYSTEM_PROMPT = "system_prompt"
    TOKEN_COUNTING = "token_counting"
    VISION = "vision"

class LLMProvider(Protocol):
    def provider_name(self) -> str: ...
    def model_identifier(self) -> ModelIdentifier: ...
    def capabilities(self) -> frozenset[Capability]: ...
    def context_window(self) -> int: ...
    def data_boundary(self) -> DataBoundary: ...          # local | external

    async def generate(self, request: GenerationRequest) -> GenerationResult: ...
    async def stream_generate(self, request: GenerationRequest) -> AsyncIterator[GenerationChunk]: ...
    async def count_tokens(self, text: str) -> int | None: ...   # None when unsupported
    async def health_check(self) -> HealthStatus: ...
```

```python
@dataclass(frozen=True)
class GenerationRequest:
    system: str | None
    messages: Sequence[PromptMessage]
    output_schema: JSONSchema | None
    max_output_tokens: int
    temperature: float
    timeout_seconds: float
    prompt_id: str
    prompt_version: PromptVersion

@dataclass(frozen=True)
class GenerationResult:
    text: str
    parsed: dict | None          # populated when output_schema was supplied and validated
    input_tokens: int | None
    output_tokens: int | None
    finish_reason: FinishReason
    model_identifier: ModelIdentifier
    latency_ms: int
```

Rules:

1. `capabilities()` is **verified** by `ai check`, never trusted from configuration (ADR-020 §6).
2. `count_tokens()` may return `None`. Business logic must tolerate this and fall back to conservative estimation.
3. `data_boundary()` is consulted before every call; an `external` provider is never invoked for a chat whose `ai_processing_mode` is `local_only` or `disabled` (ADR-024).
4. Provider exceptions are normalized to `ERROR_HANDLING.md` §6 types.
5. Every call is instrumented into `ai_calls`, including failures.

## 11.2 `StructuredOutputValidator`

```python
class StructuredOutputValidator(Protocol):
    def validate(self, payload: str, schema: JSONSchema) -> ValidationOutcome: ...
    def build_repair_prompt(self, payload: str, errors: Sequence[ValidationError]) -> str: ...
```

Applied to **every** model response regardless of the mechanism used to obtain it. One repair attempt; a second failure raises `SchemaViolationError` (ADR-020 §4).

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

## 11.5 `PromptRepository`

```python
class PromptRepository(Protocol):
    def get(self, prompt_id: str) -> PromptTemplate: ...
    def render(self, prompt_id: str, variables: Mapping[str, Any]) -> RenderedPrompt: ...
    def schema_for(self, prompt_id: str) -> JSONSchema: ...
    def version_of(self, prompt_id: str) -> PromptVersion: ...
    def list_all(self) -> list[PromptInfo]: ...
    def validate_registry(self) -> RegistryValidation: ...
```

Rules: `render()` **raises** on a missing declared variable rather than substituting empty text (ADR-026 §3); `validate_registry()` runs at startup and a mismatch is fatal; untrusted conversation content is inserted only through delimited slots (`SECURITY.md` §12).

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
