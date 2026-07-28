# DECISIONS.md

# Telegram AI Conversation Assistant

Architecture Decision Records (ADR)

Version: 1.0

Status: Active

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

# ADR-002

## Title

Python as Primary Language

Status

Accepted

Date

2026-07-28

---

### Decision

Use Python for the application.

---

### Alternatives

Rust

Go

C#

C++

Node.js

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

Fast development

Excellent AI integration

Large ecosystem

Cons

Lower runtime performance than compiled languages

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

Layered Architecture

MVC

Monolithic Structure

Microservices

---

### Reasoning

Clean Architecture keeps business logic independent of infrastructure.

---

### Consequences

Pros

Easy testing

Replaceable infrastructure

Maintainable

Cons

More initial complexity

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

Direct SQL

ORM-only approach

Active Record

---

### Reasoning

Repositories isolate storage implementation from business logic.

---

### Consequences

Pros

Testability

Database independence

Cleaner code

Cons

Additional abstraction

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

Single provider

Provider-specific implementation

---

### Reasoning

Avoid vendor lock-in.

Support local and cloud models.

---

### Consequences

Pros

Flexibility

Future-proofing

Resilience

Cons

More abstraction

---

# ADR-006

## Title

Separate AI Services

Status

Accepted

---

### Decision

Use specialized AI services instead of one monolithic AI component.

Examples

Conversation Analyzer

Memory Extractor

Planner

Reply Generator

Emotion Analyzer

---

### Reasoning

Single-responsibility components are easier to test and replace.

---

### Consequences

Pros

Maintainability

Scalability

Independent improvements

Cons

More orchestration required

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

PostgreSQL

MySQL

MongoDB

---

### Reasoning

Simple deployment

No server dependency

Reliable

Fast enough for MVP

---

### Future Plan

Support PostgreSQL through repository abstraction.

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

Hardcoded strings

Database

JSON

---

### Reasoning

Prompts evolve frequently.

Keeping them outside the source code improves maintainability.

---

### Consequences

Pros

Easy editing

Version control

Better collaboration

Cons

Additional file management

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

Keeps the core application small.

Supports future extensions.

---

### Consequences

Pros

Scalable

Customizable

Maintainable

Cons

Requires stable plugin APIs

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

- Users retain final decision-making.
- Easier to build trust in the assistant.
- Simpler to understand and debug AI behavior.

Cons

- Requires an extra user step before sending messages.

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