# MASTER_ARCHITECTURE.md

# Telegram AI Conversation Assistant

Master Architecture Diagrams

Version: 1.0

Status: Active

Last Updated: 2026-07-28

---

# Purpose

A single visual reference for the system's structure and behaviour. Each diagram is normative — where a diagram and prose disagree, that is a defect to be fixed, not a matter of interpretation.

| # | Diagram | Answers |
|---|---|---|
| 1 | Project dependency graph | What third-party code do we depend on, and where? |
| 2 | Module dependency graph | Which internal modules may import which? |
| 3 | Request flow | What happens when the user asks for a suggestion? |
| 4 | AI pipeline | How does a message become a validated suggestion? |
| 5 | Startup sequence | What happens between launch and a usable application? |
| 6 | Synchronisation sequence | How is history backfilled resumably? |
| 7 | Plugin lifecycle | How is a plugin discovered, loaded, isolated and unloaded? |
| 8 | Memory lifecycle | How does an observation become a durable, reversible fact? |
| 9 | Conversation processing lifecycle | How is a chat segmented, analysed and summarised? |

---

# 1. Project Dependency Graph

External dependencies by layer. Note that the domain layer has **no** external dependencies — that is the property the whole architecture is built to preserve.

```mermaid
flowchart TB
    subgraph P["Presentation"]
        PS["PySide6 (LGPL)"]
        QA["qasync"]
        TY["Typer"]
    end

    subgraph A["Application"]
        NONE1["stdlib only"]
    end

    subgraph D["Domain"]
        NONE2["stdlib only — no third-party imports"]
    end

    subgraph I["Infrastructure"]
        subgraph TGX["Telegram"]
            TD["TDLib / tdjson (native)"]
            TH["Telethon (fallback adapter)"]
        end
        subgraph DBX["Persistence"]
            SA["SQLAlchemy Core"]
            AL["Alembic"]
            SQ["sqlite3 (stdlib)"]
        end
        subgraph AIX["AI — optional extras"]
            AN["anthropic"]
            OA["openai"]
            HX["httpx (Ollama)"]
        end
        subgraph EMX["Embeddings"]
            FE["fastembed / onnxruntime"]
            NP["numpy"]
        end
        subgraph CRX["Cross-cutting"]
            PD["pydantic-settings"]
            YM["PyYAML"]
            SL["structlog"]
            KR["keyring"]
            AP["APScheduler"]
            PL["pluggy"]
        end
    end

    subgraph T["Dev & Test — not shipped"]
        PT["pytest, pytest-asyncio, pytest-qt"]
        HY["hypothesis"]
        RF["ruff"]
        MY["mypy"]
        IL["import-linter"]
        UV["uv"]
        PI["PyInstaller"]
    end

    P --> A
    A --> D
    I --> D

    style D fill:#e8f5e9,stroke:#2e7d32
    style A fill:#e3f2fd,stroke:#1565c0
```

**Dependency policy.** AI provider SDKs are optional extras (`pip install tgassist[anthropic]`), so a user who enables no cloud provider installs no cloud SDK. The only native dependency is the Telegram library, whose acquisition and verification is resolved in Milestone 0 (ADR-012).

---

# 2. Module Dependency Graph

The contracts enforced by `import-linter` in CI. A violation fails the build.

```mermaid
flowchart TB
    subgraph PRES["presentation/"]
        CLI["cli/"]
        DESK["desktop/"]
    end

    subgraph APP["application/"]
        UCS["use_cases/"]
        POL["policies/"]
        EVH["event_handlers/"]
        CONT["container.py<br/>(composition root)"]
    end

    subgraph DOM["domain/"]
        MODEL["model/"]
        PORTS["ports/"]
        SVCS["services/"]
        EVTS["events.py"]
        ERRS["errors.py"]
    end

    subgraph INF["infrastructure/"]
        TEL["telegram/"]
        PER["persistence/"]
        AIM["ai/"]
        EMB["embeddings/"]
        CFG["config/"]
        LOGI["logging/"]
        SEC["security/"]
        EVI["events/"]
        TSK["tasks/"]
        PLH["plugins/"]
    end

    CLI --> UCS
    DESK --> UCS
    CLI --> MODEL
    DESK --> MODEL

    UCS --> PORTS
    UCS --> MODEL
    UCS --> SVCS
    UCS --> EVTS
    UCS --> ERRS
    POL --> MODEL
    EVH --> PORTS

    SVCS --> MODEL
    PORTS --> MODEL

    TEL -.implements.-> PORTS
    PER -.implements.-> PORTS
    AIM -.implements.-> PORTS
    EMB -.implements.-> PORTS
    CFG -.implements.-> PORTS
    LOGI -.implements.-> PORTS
    SEC -.implements.-> PORTS
    EVI -.implements.-> PORTS
    TSK -.implements.-> PORTS
    PLH -.implements.-> PORTS

    CONT -.constructs.-> INF
    CONT -.injects.-> UCS

    style DOM fill:#e8f5e9,stroke:#2e7d32
    style CONT fill:#fff3e0,stroke:#e65100
```

## Enforced contracts

| Contract | Rule |
|---|---|
| Layered architecture | `presentation` → `application` → `domain`; no reverse edges |
| Domain independence | `domain` imports nothing from `application`, `infrastructure`, `presentation`, or any third-party package |
| Infrastructure isolation | `infrastructure` imports only from `domain` |
| Composition root | Only `application/container.py` imports from `infrastructure` |
| No send capability | `ReplyGenerator`, `ConversationPlanner`, `BehaviorRuleEngine` do not reference `TelegramGateway` (ADR-023) |
| Audit immutability | `AuditRepository` exposes no update or delete method |
| No cycles | No circular imports anywhere |

---

# 3. Request Flow — Generating a Suggestion

```mermaid
sequenceDiagram
    actor U as User
    participant UI as Presentation
    participant UC as GenerateSuggestion
    participant CA as ContextAssembler
    participant MR as MemoryRanker
    participant VS as VectorStore
    participant RP as Repositories
    participant AI as LLMProvider
    participant SV as SchemaValidator
    participant CC as ConfidenceCalibrator
    participant BE as BehaviorEngine
    participant BUS as EventBus

    U->>UI: Request suggestion
    UI->>UC: execute(chat_id)

    UC->>RP: load conversation, messages, goal, profiles
    UC->>VS: search(query vector, contact scope)
    VS-->>UC: candidate memories + scores
    UC->>MR: rank(candidates, context)
    MR-->>UC: ranked memories

    UC->>CA: assemble(inputs, token budget)
    CA-->>UC: ConversationContext + truncation report

    UC->>UC: check chat.ai_processing_mode vs provider.data_boundary
    Note over UC: local_only chat + external provider → refuse, degrade

    UC->>AI: generate(request, schema)
    AI-->>UC: raw response
    UC->>SV: validate(response, schema)
    alt invalid
        SV-->>UC: errors
        UC->>AI: repair (one attempt only)
        AI-->>UC: response
        UC->>SV: validate
    end
    SV-->>UC: parsed ReplySuggestion

    UC->>CC: calibrate(model confidence, verifiable signals)
    CC-->>UC: final Confidence + recommended_action

    UC->>BE: recommend(context, profiles, now)
    BE-->>UC: BehaviorRecommendation

    UC->>RP: persist suggestion + recommendation + context snapshot (one transaction)
    UC->>BUS: SuggestionGenerated (after commit)
    UC-->>UI: ReplySuggestion
    UI-->>U: Display with reasoning, confidence, alternatives, provider

    Note over U,UI: The flow ends here.<br/>Sending requires a separate explicit action.
```

**The critical property:** there is no edge from this flow to Telegram. Sending is a separate use case requiring an approved suggestion (ADR-023).

---

# 4. AI Pipeline

```mermaid
flowchart TD
    IN[Message ingested] --> TRIG{SuggestionTriggerPolicy}
    TRIG -->|not triggered| STOP([Stored only — no model call])
    TRIG -->|triggered| BOUND{Data boundary check}

    BOUND -->|local_only + external provider| DEG([Degrade: no suggestion, explain why])
    BOUND -->|allowed| CACHE{Analysis cached?<br/>fingerprint + version match}

    CACHE -->|hit| REUSE[Reuse cached analysis]
    CACHE -->|miss| COMP[Composite analysis call]

    COMP --> VAL1{Schema valid?}
    VAL1 -->|no, attempt 1| REP1[Repair once]
    REP1 --> VAL1
    VAL1 -->|no, attempt 2| ERR([SchemaViolationError])
    VAL1 -->|yes| SPLIT[Partial extraction per section]

    SPLIT --> ANA[Topic, intent, stage, questions]
    SPLIT --> EMO[Emotion + evidence]
    SPLIT --> PROP[Memory proposals + quotations]

    REUSE --> ANA

    PROP --> GRD{Supporting quotation present?}
    GRD -->|no| DROP([Discarded])
    GRD -->|yes| CONF{Conflicts with existing memory?}
    CONF -->|yes| QUEUE[Queue for user decision — never auto-approve]
    CONF -->|no| AUTO{Auto-approve rule satisfied?}
    AUTO -->|yes| MEM[(Memory)]
    AUTO -->|no| QUEUE
    QUEUE --> USERD{User decision}
    USERD -->|approve| MEM
    USERD -->|reject| REJ[(Rejection recorded)]

    ANA --> PLAN{Planner enabled?}
    PLAN -->|yes| PL[ConversationPlan]
    PLAN -->|no| GEN
    PL --> GEN[Reply Generator]
    EMO --> GEN
    MEM -.retrieval.-> GEN

    GEN --> VAL2[Schema validation]
    VAL2 --> CAL[Confidence calibration<br/>model self-report + verifiable signals]
    CAL --> ACT{Confidence band}
    ACT -->|low| MAN[recommended_action:<br/>clarify or write_manually]
    ACT -->|medium/high| SUG[recommended_action:<br/>review or send]
    MAN --> PERSIST
    SUG --> PERSIST[Persist suggestion + context snapshot]
    PERSIST --> INSTR[(ai_calls: tokens, latency, cost, outcome)]
    PERSIST --> SHOW([Shown to user])

    style MEM fill:#e8f5e9,stroke:#2e7d32
    style QUEUE fill:#fff3e0,stroke:#e65100
    style SHOW fill:#e3f2fd,stroke:#1565c0
```

---

# 5. Startup Sequence

```mermaid
sequenceDiagram
    participant M as __main__
    participant CFG as Configuration
    participant LOG as Logging
    participant SEC as SecretStore
    participant FS as FileStore
    participant DB as Database
    participant MIG as MigrationRunner
    participant PR as PromptRepository
    participant C as Container
    participant PLG as PluginHost
    participant SCH as Scheduler
    participant UI as Presentation
    participant TG as TelegramGateway

    M->>CFG: load (defaults → yaml → env → flags)
    CFG->>CFG: validate types, ranges, unknown keys, store exclusivity
    alt invalid
        CFG-->>M: ConfigurationError
        M-->>M: exit with actionable message
    end

    M->>LOG: initialise with redaction processor
    M->>SEC: is_available()
    alt unavailable and required
        SEC-->>M: SecretStoreUnavailable
        M-->>M: refuse to start (fail securely)
    end

    M->>FS: ensure directories and owner-only permissions
    M->>DB: open, apply PRAGMAs, start writer thread
    M->>MIG: current_revision() vs head_revision()
    alt database newer than application
        MIG-->>M: refuse to start
    else pending migrations
        MIG->>DB: backup, then upgrade (auto-restore on failure)
    end
    alt unclean previous shutdown
        M->>DB: PRAGMA integrity_check
    end

    M->>PR: validate_registry()
    alt registry mismatch
        PR-->>M: PromptRegistryInvalid (fatal)
    end

    M->>C: build composition root
    C->>C: construct adapters, inject into use cases

    M->>PLG: discover and load (bounded, isolated per plugin)
    Note over PLG: A failing plugin is disabled, never fatal

    M->>SCH: start background jobs
    M->>UI: start (application is now usable)

    par Non-blocking
        M->>TG: connect and authorise
        TG-->>UI: AuthorizationStateChanged
    end

    Note over UI,TG: The UI is usable before Telegram connects.<br/>Startup never blocks on the network.
```

---

# 6. Synchronisation Sequence

```mermaid
sequenceDiagram
    participant U as User/Scheduler
    participant SC as SyncChatHistory
    participant CUR as SyncCursorRepository
    participant GW as TelegramGateway
    participant SEG as ConversationSegmenter
    participant UOW as UnitOfWork
    participant BUS as EventBus

    U->>SC: sync(chat_id)
    SC->>CUR: get(chat_id)
    CUR-->>SC: cursor {oldest, newest, backfill_complete}

    loop Until backfill complete or horizon reached
        SC->>GW: iter_history(before=cursor.oldest, limit=batch)
        alt FLOOD_WAIT
            GW->>GW: bounded exponential backoff
        end
        GW-->>SC: batch of messages

        SC->>SEG: segment(batch, existing conversations)
        SEG-->>SC: conversation assignments

        SC->>UOW: begin
        SC->>UOW: add_batch(messages)  [idempotent on unique key]
        SC->>UOW: upsert conversations
        SC->>UOW: advance cursor.oldest
        UOW-->>SC: commit
        SC->>BUS: SyncProgressed

        Note over SC,UOW: One transaction per batch.<br/>Interruption here leaves a consistent, resumable state.

        SC->>SC: throttle (backfill_delay_ms)
        alt horizon or cap reached
            SC->>CUR: backfill_complete = true
        end
    end

    SC->>BUS: SyncCompleted

    par Live updates
        GW-->>SC: NewMessage / MessageEdited / MessageDeleted
        SC->>UOW: ingest (idempotent), advance newest
        SC->>BUS: MessageIngested
    end

    alt Persistent failure
        SC->>CUR: record_failure (consecutive_failures++)
        alt threshold exceeded
            SC->>SC: disable sync for chat
            SC->>BUS: NotificationRaised(sync_failed)
        end
    end
```

---

# 7. Plugin Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Discovered: entry point or plugins/ directory

    Discovered --> Refused: api_version incompatible
    Discovered --> Registered: manifest valid

    Registered --> Disabled: user disables
    Disabled --> Registered: user enables
    Registered --> Loading: enabled at startup

    Loading --> Failed: import or initialize() error
    Loading --> Active: initialize() succeeded, hooks registered

    Active --> Degraded: hook failure (below threshold)
    Degraded --> Active: successful invocation
    Degraded --> Failed: failure threshold exceeded

    Active --> Unloading: user disables or app shutdown
    Unloading --> Registered: shutdown() complete or timed out

    Failed --> Registered: user re-enables
    Refused --> [*]

    note right of Refused
        Version checked BEFORE import.
        Incompatible code never executes.
    end note

    note right of Failed
        Exception logged with plugin name,
        never propagated to core.
        action_required notification raised.
    end note

    note right of Unloading
        Bounded timeout.
        A hanging plugin never blocks app exit.
    end note
```

---

# 8. Memory Lifecycle

```mermaid
flowchart TD
    OBS[Observation in conversation] --> EXT[MemoryExtractor]
    EXT --> QUOTE{Supporting quotation?}
    QUOTE -->|no| DROP([Discarded before user sees it])
    QUOTE -->|yes| PROP[MemoryProposal — status: pending]

    PROP --> DUP{Already rejected before?}
    DUP -->|yes| SKIP([Skipped — rejection history consulted])
    DUP -->|no| CONFL{Conflicts with existing Memory?}

    CONFL -->|yes| REVIEW[User decision REQUIRED<br/>never auto-approves]
    CONFL -->|no| RULE{Auto-approve rule?<br/>category allowed AND<br/>confidence ≥ high AND<br/>no conflict}
    RULE -->|satisfied| APPROVE
    RULE -->|not satisfied| REVIEW

    REVIEW -->|approve| APPROVE[Approve]
    REVIEW -->|reject| REJECT[status: rejected<br/>retained to prevent re-proposal]
    REVIEW -->|no action, 90 days| EXPIRE[status: expired]

    APPROVE --> TX{{One transaction}}
    TX --> MEM[(Memory created or revised)]
    TX --> REV[(MemoryRevision recorded)]
    TX --> STAT[Proposal status: approved]

    MEM --> EMBED[Embedding job — background]
    EMBED --> VEC[(Vector indexed)]

    MEM --> RETR[Retrieval: hybrid ranking]
    RETR --> USE[Used in a suggestion]
    USE --> TRACK[last_retrieved_at, retrieval_count updated]
    TRACK --> RETR

    MEM --> EDIT{User action}
    EDIT -->|edit| TX
    EDIT -->|pin| PIN[Always retrieved regardless of score]
    EDIT -->|forget| SOFT[Soft delete — excluded from AI and retrieval]
    SOFT -->|30-day grace| HARD[Hard delete + vector removed]
    EDIT -->|purge contact| HARD

    MEM --> DECAY[Importance decay affects RANKING only]
    DECAY --> RETR

    style MEM fill:#e8f5e9,stroke:#2e7d32
    style REVIEW fill:#fff3e0,stroke:#e65100
    style DROP fill:#ffebee,stroke:#c62828
```

**Two invariants are visible here:** decay affects ranking but never deletes, and no path leads from AI output to `Memory` without passing through approval or an explicit, conflict-free auto-approval rule.

---

# 9. Conversation Processing Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Ingested: message persisted

    Ingested --> Segmenting: ConversationSegmenter (deterministic)

    state Segmenting {
        [*] --> CheckGap
        CheckGap --> ExistingConversation: gap < threshold AND count < max
        CheckGap --> NewConversation: gap ≥ threshold OR count ≥ max
        ExistingConversation --> [*]
        NewConversation --> [*]
    }

    Segmenting --> Open: assigned to an open conversation

    Open --> Open: further messages appended
    Open --> Analyzing: trigger policy fires
    Open --> Closing: inactivity gap exceeded

    state Analyzing {
        [*] --> CacheCheck
        CacheCheck --> Reuse: fingerprint + version match
        CacheCheck --> Compute: miss
        Compute --> Validate
        Validate --> Repair: schema invalid (once)
        Repair --> Validate
        Validate --> Persist: valid
        Reuse --> [*]
        Persist --> [*]
    }

    Analyzing --> Open: analysis cached, conversation continues

    Closing --> Summarizing: summarize_on_close enabled
    Closing --> Closed: summarization disabled

    state Summarizing {
        [*] --> Generate
        Generate --> Supersede: previous summary exists
        Generate --> Store: first summary
        Supersede --> Store
        Store --> Embed: background embedding job
        Embed --> [*]
    }

    Summarizing --> Closed

    Closed --> Retained: within retention period
    Retained --> Archived: older than archive threshold
    Retained --> Deleted: retention policy expiry
    Archived --> Retained: user restores from archive
    Deleted --> [*]

    note right of Segmenting
        Pure and re-runnable.
        Re-segmenting a chat from its
        messages yields identical boundaries.
    end note

    note right of Analyzing
        A prompt or model version change
        invalidates only affected entries,
        never the whole cache.
    end note
```

---

# Diagram Maintenance

These diagrams are normative. When implementation diverges:

1. If the diagram is right, the code is a defect.
2. If the code is right, update the diagram **in the same commit**.
3. A structural change (new layer, new dependency direction, new flow) requires an ADR before the diagram changes.

Diagrams are reviewed at the end of every milestone (`DEVELOPMENT_WORKFLOW.md` §12).
