# Telegram AI Conversation Assistant

A privacy-first desktop application that helps you communicate more thoughtfully on Telegram.

It reads your conversations, remembers what matters about the people you talk to, and suggests replies you review before sending. It is a copilot, not an autopilot.

> **Status: pre-implementation.** The architecture and specification are complete; no source code exists yet. Development begins at Milestone 0. See [ROADMAP.md](docs/ROADMAP.md).

---

## What it does

- **Connects to Telegram** through the client API and synchronises a bounded, user-selected set of chats.
- **Remembers** what you learn about people — interests, plans, important dates, open questions — with every fact traceable to the message it came from.
- **Analyses conversations** for topic, intent, emotion and unanswered questions.
- **Suggests replies** with reasoning, alternatives and a calibrated confidence score, and tells you when it is not confident enough to be useful.
- **Recommends timing** — when to reply, how long, whether to split — based on deterministic rules, not guesswork.
- **Tracks relationships** with explainable metrics computed from observable data.
- **Keeps goals** per contact, so the assistant knows whether you are maintaining a friendship or practising a language.

## What it deliberately does not do

- **It never sends a message you have not approved.** There is no auto-reply mode, and there will not be one.
- **It never simulates typing** or otherwise makes automated activity look human.
- **It never stores an AI-inferred fact without your decision** — extracted memories are proposals you approve or reject.
- **It sends nothing to a cloud AI provider by default.** Cloud processing is opt-in, per chat.
- **It collects no telemetry.** No analytics, no crash reporting, no usage tracking.

These are architectural constraints, not settings. The components that generate replies have no capability to send, by construction.

---

## Privacy

Every conversation involves someone who did not install this software. The design takes that seriously:

- **Local-first.** All data stays on your device unless you explicitly enable a cloud AI provider, per chat.
- **Bounded collection.** You choose which chats sync and how far back. Nothing syncs by default.
- **Minimum transmission.** When a cloud provider is enabled, it receives retrieved memories and recent messages — never full history.
- **Full control.** Every stored fact is visible, editable and deletable.
- **Contact rights.** One action exports everything about a single person; one action erases it — so you can answer them if they ask.

Known limitations, including database encryption phasing, are documented honestly in [PRIVACY.md](docs/PRIVACY.md) §10.

---

## Technology

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| Telegram | TDLib (client API, not the Bot API) |
| Concurrency | asyncio, with SQLite on a dedicated writer thread |
| Desktop UI | PySide6 (LGPL) |
| Database | SQLite via SQLAlchemy Core + Alembic |
| AI | Provider-agnostic — Anthropic, OpenAI, Ollama, or none |
| Embeddings | Local by default (fastembed), exact vector search |
| Architecture | Clean Architecture, enforced in CI |

No AI provider is required. Browsing, search, memory management, goals, relationship metrics and timing advice all work with no model and no network.

---

## Project structure

```
├── config/         Configuration files
├── docs/           Specifications (see index below)
├── migrations/     Alembic database migrations
├── plugins/        Third-party plugins
├── prompts/        Versioned prompt templates and schemas
├── scripts/        Development utilities
├── src/tgassist/
│   ├── domain/           Entities, ports, pure services — no external dependencies
│   ├── application/      Use cases, policies, composition root
│   ├── infrastructure/   Telegram, persistence, AI, embeddings, config, logging
│   └── presentation/     Desktop UI and CLI
└── tests/          unit · integration · e2e · evals · architecture
```

Dependencies point inward: the domain layer imports nothing, and CI fails the build if that stops being true.

---

## Documentation

Start with [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) for what the system does, then [ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it is structured.

### Specification

| Document | Contents |
|---|---|
| [PROJECT_SPEC.md](docs/PROJECT_SPEC.md) | Requirements, functional and non-functional |
| [ROADMAP.md](docs/ROADMAP.md) | 15 milestones with acceptance criteria |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, components, dependency rules |
| [MASTER_ARCHITECTURE.md](docs/MASTER_ARCHITECTURE.md) | Nine diagrams: dependencies, flows, lifecycles |
| [DOMAIN_MODEL.md](docs/DOMAIN_MODEL.md) | Entities, invariants, ubiquitous language |
| [DATABASE.md](docs/DATABASE.md) | Schema, constraints, migrations, backup |
| [API.md](docs/API.md) | Ports and internal contracts |

### AI

| Document | Contents |
|---|---|
| [AI_MODELS.md](docs/AI_MODELS.md) | Pipeline, providers, validation, injection defences |
| [PROMPTS.md](docs/PROMPTS.md) | Prompt engineering and versioning |
| [VECTOR_SEARCH.md](docs/VECTOR_SEARCH.md) | Embeddings and semantic retrieval |

### Operations

| Document | Contents |
|---|---|
| [SECURITY.md](docs/SECURITY.md) | Threat model and technical controls |
| [PRIVACY.md](docs/PRIVACY.md) | Privacy commitments and data lifecycle |
| [ERROR_HANDLING.md](docs/ERROR_HANDLING.md) | Error taxonomy, retries, degradation |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Configuration, settings and secrets |
| [PLUGIN_SYSTEM.md](docs/PLUGIN_SYSTEM.md) | Extension architecture and trust model |

### Process

| Document | Contents |
|---|---|
| [DECISIONS.md](docs/DECISIONS.md) | 30 architecture decision records |
| [TESTING.md](docs/TESTING.md) | Verification strategy |
| [CONTRIBUTING.md](docs/CONTRIBUTING.md) | How to work on the project |
| [DEVELOPMENT_WORKFLOW.md](docs/DEVELOPMENT_WORKFLOW.md) | Feature lifecycle |
| [CHANGELOG.md](docs/CHANGELOG.md) | What changed |

---

## Getting started

Not yet applicable — implementation begins at Milestone 0.

When it does, you will need Python 3.12+, [uv](https://github.com/astral-sh/uv), and a Telegram `api_id`/`api_hash` from [my.telegram.org](https://my.telegram.org). An AI provider is optional.

---

## A note on Telegram accounts

This is a third-party client, which Telegram permits. Behaviour that resembles automation is what puts accounts at risk — which is one reason this application never sends without approval and never simulates typing. Even so, if you are cautious, test with a secondary account first.

---

## License

Not yet chosen. This decision is pending and affects distribution options; see the open decisions in the architecture review.

---

## Contributing

See [CONTRIBUTING.md](docs/CONTRIBUTING.md). In short: read the specification first, keep the domain layer clean, write tests, and update documentation in the same commit as the code.
