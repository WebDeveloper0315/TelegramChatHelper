# TESTING.md

# Telegram AI Conversation Assistant

Testing Strategy

Version: 2.0

Status: Active

Last Updated: 2026-07-28

---

# 0. Changes in Version 2.0

| Change | Reason |
|---|---|
| Architectural tests added as a distinct level (§4) | The corrected dependency rule (ADR-011) and the automation boundary (ADR-023) are only guarantees if a machine checks them |
| AI evaluation split into two tiers (§9) | Live model evaluation is nondeterministic and costs money; it cannot run on every commit |
| Contract tests added (§6) | Fakes drift from real implementations unless both run the same suite |
| Fakes specified as a first-class deliverable (§14) | Every port needs a behaviourally correct fake for use-case tests to be possible |
| Privacy and security test requirements enumerated (§16, §17) | v1.0 listed categories without falsifiable assertions |

---

# 1. Purpose

Detect defects early · prevent regressions · validate AI quality · ensure stability · enable confident refactoring.

Testing is required for every feature. A feature without verification is not complete (`DEVELOPMENT_WORKFLOW.md` §22).

---

# 2. Principles

Every test should be repeatable, independent, deterministic, fast, readable and maintainable.

Two rules that follow from those and matter most here:

**Time and randomness are injected, never patched.** `Clock` and `IdGenerator` are ports precisely so tests need no monkeypatching of global state.

**Nondeterministic tests are quarantined, not tolerated.** AI evaluation is inherently nondeterministic and therefore lives in its own opt-in tier — never in the suite that gates a commit.

---

# 3. Test Levels

```
        Evaluation (opt-in, live models, cost-gated)
      ───────────────────────────────────────────────
              End-to-End (via CLI)
        ───────────────────────────────────
           Integration & Contract
      ─────────────────────────────────────────
                   Unit
   ───────────────────────────────────────────────────
              Architectural (fastest, first)
```

| Level | Directory | Dependencies | Runs |
|---|---|---|---|
| Architectural | `tests/architecture/` | None | Every commit |
| Unit | `tests/unit/` | None | Every commit |
| Contract | `tests/contract/` | Fakes + real | Every commit (fakes), integration marker (real) |
| Integration | `tests/integration/` | Real DB, network | Marked, on demand and nightly |
| End-to-end | `tests/e2e/` | Local stack via CLI | Every commit (fakes), nightly (real) |
| Evaluation | `tests/evals/` | Live models | Opt-in, on prompt/model change |

Markers: `unit`, `integration`, `e2e`, `eval`, `slow`. The default run is `-m "not integration and not eval and not slow"`.

---

# 4. Architectural Tests

These run first and fastest because they encode the guarantees the whole design rests on. A violation is a build failure, not a discussion.

| Test | Assertion |
|---|---|
| Layer contracts | `import-linter`: presentation → application → domain; infrastructure → domain only |
| Domain purity | No module under `domain/` imports any third-party package |
| Composition root | Only `application/container.py` imports from `infrastructure/` |
| No cycles | No circular imports anywhere |
| **No send capability** | `ReplyGenerator`, `ConversationPlanner`, `BehaviorRuleEngine` have no reference to `TelegramGateway` (ADR-023) |
| **No typing indicator** | No method anywhere sends a typing action (ADR-023) |
| **Audit immutability** | `AuditRepository` exposes no update or delete method |
| **No trust score** | No `trust_score` or `friendship_level` field exists (§3.5 of `PROJECT_SPEC.md`) |
| Plugin send path closed | `PluginContext` exposes no path to `TelegramGateway` |
| Async discipline | No blocking I/O call in an `async def` outside the designated executor |

---

# 5. Unit Tests

Validate individual components in isolation. **No network, no Telegram, no AI provider, no real database.**

Coverage targets: domain and application **>90%** · repositories **>85%** · infrastructure **>70%**.

**Property-based tests** (`hypothesis`) are used where invariants are stronger than examples:

| Target | Property |
|---|---|
| Mappers | domain → row → domain is identity |
| `ConversationSegmenter` | Re-segmenting produces identical boundaries; conversations never overlap |
| `MemoryRanker` | Ranking is a total order; pinned always included; `USER` outranks equal-similarity AI |
| `TokenBudgetPlanner` | Allocations never exceed the budget; output reserve always preserved |
| Keyset pagination | Iterating all pages yields every row exactly once |
| `RelationshipMetricsCalculator` | Deterministic; below sample size returns `insufficient_data` |

---

# 6. Contract Tests

Every port has one test suite run against **every** implementation — real and fake. This is what prevents fakes from drifting into fiction, which is the standard failure mode of fake-based testing.

| Port | Implementations under test |
|---|---|
| Repositories | SQLAlchemy implementation, in-memory fake |
| `UnitOfWork` | SQLite implementation, in-memory fake |
| `LLMProvider` | Anthropic, OpenAI, Ollama, fake |
| `EmbeddingProvider` | fastembed, fake |
| `VectorStore` | NumPy, sqlite-vec (when added), fake |
| `SecretStore` | keyring, encrypted file, fake |
| `TelegramGateway` | TDLib adapter, fake |
| `EventBus` | in-memory implementation, fake |
| `Cache`, `Scheduler`, `FileStore` | real, fake |

Real-implementation runs carry the `integration` marker; fake runs are in the default suite.

## Provider conformance

`LLMProvider` adapters additionally run a **conformance suite**: identical prompts and schemas across every provider, verifying capability negotiation against reality, schema validation, the single repair path, error normalization, and that `count_tokens()` returning `None` is handled.

---

# 7. Integration Tests

Verify adapters against real dependencies.

| Area | Verifies |
|---|---|
| Persistence | Migrations up/down/up; constraints; cascades; FTS triggers; transaction isolation |
| Telegram | Authentication; session persistence; resumable backfill; reconnection; rate limiting |
| AI providers | Real request/response; streaming; timeouts; rate-limit handling |
| Embeddings | Model download; batch embedding; dimension consistency |
| Secret store | OS backend round-trip |
| Scheduler | Job execution, cancellation, failure thresholds |

Use isolated test databases and dedicated test accounts. **Never use a production Telegram account for automated tests.**

---

# 8. End-to-End Tests

Full workflows driven **through the CLI** (ADR-030), which gives a stable, scriptable driver that does not require automating a GUI.

Primary workflow:

```
launch → migrate → authenticate (fake gateway) → sync history
   → segment conversations → analyze → extract proposals
   → approve a proposal → retrieve memories → generate a suggestion
   → inspect the explanation → approve → send (fake gateway)
   → verify memory and relationship updates
```

Additional workflows: sync interruption and resumption · export then import round-trip · contact purge completeness · backup then restore · migration with rollback · degraded operation with no AI provider · degraded operation with no network.

---

# 9. AI Evaluation

Two tiers, because live model evaluation is nondeterministic and costs real money.

## Tier 1 — Deterministic (every commit, in CI)

All AI code paths exercised against `FakeLLMProvider`, which returns canned schema-valid responses. This validates everything except model quality: context assembly, token budgeting, schema validation, the repair path, error normalization, caching and invalidation, persistence, cost instrumentation, and data-boundary enforcement.

**No network. No cost. Fully deterministic.**

## Tier 2 — Live evaluation (opt-in, on prompt or model change)

Run against a fixed synthetic benchmark corpus.

| Metric | Measures |
|---|---|
| Relevance | Does the suggestion address the message? |
| **Groundedness** | Are factual claims supported by supplied memories or messages? (hallucination rate) |
| Memory precision | Proportion of proposals that are correct |
| Memory recall | Proportion of extractable facts that were proposed |
| **Confidence calibration** | Predicted confidence versus actual acceptance rate |
| Tone match | Does output match the configured tone or mirrored style? |
| Context retention | Are earlier conversation facts used correctly? |
| **Injection resistance** | Do adversarial cases produce schema-valid harmful output? |
| Cost per conversation | Tokens and estimated spend |

Rules:

1. AI tests compare against **expected behaviours**, never exact wording.
2. Results are recorded per run so regressions are visible over time.
3. A prompt or model change requires an evaluation run before merge.
4. The corpus is versioned alongside the prompts.

## Benchmark corpus

**Entirely synthetic. Never real user conversations** (§13).

Composition: casual friendship · professional networking · language practice · reconnection after absence · multilingual exchanges · emotionally charged exchanges · ambiguous or terse messages · conversations with contradictory facts (to test supersession) · **prompt injection attempts** · conversations where the correct answer is "ask a clarifying question".

That last category matters: a system that always produces a confident suggestion is easy to build and useless. The corpus must contain cases where declining is correct.

---

# 10. Memory System Tests

| Test | Assertion |
|---|---|
| Extraction | "My favorite food is sushi" produces a proposal with category `preference`, value `sushi`, and a supporting quotation |
| **Quotation requirement** | A proposal without a quotation is discarded before the user sees it |
| **Conflict never auto-approves** | A proposal contradicting an existing memory always requires a user decision |
| Revision required | A memory value never changes without a `MemoryRevision` |
| Supersession | A contradictory approved fact supersedes rather than duplicating |
| Rejection history | A rejected fact is not re-proposed |
| Deduplication | Two phrasings of the same fact merge rather than duplicating |
| Retrieval accuracy | Precision and recall measured against a labelled set |
| **User precedence** | A `USER` memory outranks an equal-similarity `AI_AUTO` memory |
| Pinning | Pinned memories are retrieved regardless of score |
| Deletion completeness | A deleted memory is absent from retrieval and its vector is gone |
| Decay | Decay affects ranking but never deletes |

---

# 11. Other Component Tests

**Planner:** given a goal, conversation and memories, produces a plausible next action; becomes stale on a new message; schema-valid on every provider.

**Reply generator:** reply matches context; no invented facts; tone appropriate; goal-aligned; **confidence below the low threshold forces `clarify` or `write_manually`**; the context snapshot identifies exactly what was used.

**Behavior engine:** deterministic; within configured bounds; never recommends sending during quiet hours unless urgent; correct across a DST transition.

**Relationship engine:** every metric computed without an LLM; idempotent recomputation; `insufficient_data` below the sample threshold.

**Database:** CRUD; transactions; migrations both directions; rollback; foreign keys; index usage; constraint violations raising typed errors.

**Telegram:** authentication; session persistence; **resumable backfill with no gaps or duplicates**; reconnection; rate limiting.

**Plugins:** loading; registration; event handling; shutdown; configuration; **a faulty plugin does not crash the application**; version refusal before import.

---

# 12. Performance and Load Tests

Seeded with **500,000 messages, 200 contacts, 5,000 memories**.

| Measure | Target |
|---|---|
| Cold start to usable interface | < 3 s |
| Message history page | < 100 ms |
| Full-text search | < 200 ms |
| Memory retrieval end to end | < 100 ms |
| Vector search (warm) | < 50 ms |
| Message ingest | < 50 ms |
| Sync throughput | ≥ 1,000 messages/minute |
| Steady-state memory | < 600 MB |

Benchmarks run in CI and fail on regression beyond tolerance. Targets become binding in `PERFORMANCE_BUDGETS` at Milestone 13.

Load scenarios: very large chat histories · thousands of memories · large contact lists · long-running sessions · concurrent sync and UI interaction.

---

# 13. Test Data

Stored in `tests/data/`: `contacts.json`, `chats.json`, `messages.json`, `memories.json`, `summaries.json`, `conversation_examples/`, `injection_cases/`.

**Test data must never contain real user conversations.** Synthetic data is generated by `scripts/seed_test_data.py` with a fixed seed for reproducibility.

---

# 14. Fakes and Mocking

## Fakes are a first-class deliverable

Every port has a **fake** in `tests/fakes/` — in-memory, behaviourally correct, usable without I/O. Fakes are not stubs: `FakeUnitOfWork` honours real commit and rollback semantics; `FakeVectorStore` performs exact search; `FakeEventBus` isolates handler exceptions exactly as the real one does.

Fakes are kept honest by the contract tests in §6.

## What to mock

Mock: AI providers, Telegram gateway, network calls, filesystem where practical.

**Do not mock:** business logic · the domain layer · anything a fake covers better.

**Do not patch time.** Inject `FixedClock` or `AdvanceableClock`.

---

# 15. Test Naming

Descriptive names stating the behaviour:

```
test_memory_extraction_creates_favorite_food_proposal()
test_conflicting_proposal_never_auto_approves()
test_goal_manager_deactivates_previous_goal_on_activation()
test_reply_generator_recommends_manual_write_on_low_confidence()
test_external_provider_not_called_for_local_only_chat()
test_backfill_resumes_without_duplicates_after_interruption()
```

Avoid `test1()`, `test_case()`, `test_it_works()`.

---

# 16. Security Tests

| Test | Assertion |
|---|---|
| Redaction | Secrets, auth codes and message content never appear in emitted log records |
| Secret storage | No secret value is written to the database or any file |
| SQL injection | Repositories resist injection attempts in every string parameter |
| **Data boundary** | An `external` provider is never called for a `local_only` or `disabled` chat |
| **Fallback boundary** | Provider fallback never crosses the data boundary |
| Session protection | Session files are created with owner-only permissions |
| Audit immutability | UPDATE and DELETE against `audit_log` are rejected |
| Backup contents | Backups contain no secrets, sessions or logs |
| Backup encryption | A backup outside the data directory is always encrypted |
| Injection resistance | Corpus injection cases produce no schema-valid harmful output |
| Plugin isolation | A raising hook does not propagate |
| Startup denial | An unavailable secret store prevents startup |
| Config masking | `config show` never emits a secret value |

---

# 17. Privacy Tests

| Test | Assertion |
|---|---|
| **Purge completeness** | After a contact purge, no row in any table references that contact |
| Purge atomicity | An interrupted purge leaves no partial state |
| Export exclusions | Exports contain no secrets, sessions or logs |
| Scoped export | A per-contact export contains that contact's data and no other's |
| Import idempotency | Importing the same file twice changes nothing the second time |
| Retention | Retention deletes what it should and never touches audit events |
| Default boundary | A newly discovered chat defaults to `local_only` |
| No telemetry | No outbound connection is made to any host other than Telegram and configured providers |
| Account isolation | No query returns data belonging to another account |
| Contact isolation | Memory retrieval never crosses contacts |

---

# 18. Regression Tests

Every bug fix includes a test that reproduces the bug and a test that verifies the fix. Regression tests are never deleted, and they name the issue they came from.

---

# 19. Continuous Integration

Before merging, on Windows and Linux:

```
ruff check  ·  ruff format --check
mypy src
lint-imports                          (architectural contracts)
pytest -m "not integration and not eval and not slow"
pip-audit
gitleaks
```

All required checks must pass. Integration tests run nightly. Evaluation runs on prompt or model change.

---

# 20. Testing Workflow

For every feature: design → write or update tests → implement → run → refactor → re-run → update documentation.

Testing is part of development, not a final step.

---

# 21. Release Checklist

- All required tests pass
- Architectural contracts pass
- Security and privacy tests pass
- Performance targets met or deviations documented
- Migration tested up and down against seeded data
- Backup and restore verified
- AI evaluation completed and recorded
- No critical regressions
- Documentation updated

---

# 22. Philosophy

Quality is built continuously, not audited in at the end.

Two convictions shape this strategy:

**Structural guarantees deserve mechanical verification.** "The reply generator must not send messages" is a comment. A test asserting it has no reference to the gateway is a guarantee that survives refactoring and new contributors.

**Nondeterminism must be quarantined, not accommodated.** The moment a flaky AI evaluation gates a commit, the team learns to re-run failing tests instead of reading them — and the suite stops meaning anything. Deterministic tests gate commits; live evaluation gates prompt changes.

The project is stable only when its behaviour is predictable, testable and repeatable.
