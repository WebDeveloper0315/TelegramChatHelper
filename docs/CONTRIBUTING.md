# CONTRIBUTING.md

# Telegram AI Conversation Assistant

Contribution Guide

Version: 1.0

Status: Active

---

# Welcome

Thank you for contributing to this project.

This document explains how development should be performed to keep the project maintainable, consistent, and well documented.

The project follows modern software engineering practices with an emphasis on modularity, testing, documentation, and incremental development.

---

# Project Philosophy

Every contribution should improve one or more of the following:

- Reliability
- Maintainability
- Readability
- Performance
- Testability
- Documentation
- User experience

Avoid unnecessary complexity.

---

# Before Writing Code

Before implementing a feature:

1. Read `PROJECT_SPEC.md` for the requirement.
2. Review `ARCHITECTURE.md` for structure and the dependency rules.
3. Review `DOMAIN_MODEL.md` for the entities and invariants involved.
4. Check `DECISIONS.md` for relevant architectural decisions.
5. Verify the current milestone in `ROADMAP.md` — the authoritative sequencing.
6. Ensure the proposed work aligns with project goals.

If a change affects architecture, create or update an ADR in `DECISIONS.md`.

**Non-negotiable constraints.** Some rules are enforced by architectural tests and cannot be worked around without an ADR superseding the decision behind them:

- Dependencies point inward; the domain layer imports nothing (ADR-011).
- Nothing except the `SendMessage` use case can send a Telegram message (ADR-023).
- No component may emit synthetic typing indicators (ADR-023).
- AI-derived memories are proposals, never direct writes (ADR-019).
- Secret values never enter the database, logs or backups (ADR-021).
- No external AI provider is called for a chat that is not `cloud_allowed` (ADR-024).

---

# Development Workflow

Every feature should follow this sequence:

1. Understand the requirement.
2. Design the solution.
3. Identify affected modules.
4. Discuss architectural impact.
5. Implement the smallest working increment.
6. Write or update tests.
7. Update documentation.
8. Perform a self-review.
9. Merge only after validation.

Large features should be divided into smaller, independently testable tasks.

---

# Branch Strategy

Recommended branch naming:

```
main

develop

feature/<feature-name>

bugfix/<issue-name>

hotfix/<issue-name>

docs/<topic>

refactor/<component>
```

Examples

```
feature/memory-engine

feature/reply-generator

bugfix/database-migration

docs/testing

refactor/telegram-gateway
```

---

# Commit Message Format

Use clear, descriptive commit messages.

Recommended format:

```
type(scope): summary
```

Examples

```
feat(memory): add semantic retrieval

fix(database): prevent duplicate contacts

docs(api): update repository interfaces

test(planner): add strategy evaluation tests

refactor(reply): simplify generation pipeline
```

Common types:

- feat
- fix
- docs
- refactor
- test
- chore
- perf
- build
- ci

---

# Coding Standards

Code should be:

- Simple
- Readable
- Modular
- Well documented
- Type annotated where practical

Avoid:

- Deep nesting
- Large functions
- Circular dependencies
- Magic numbers
- Global mutable state

---

# Naming Conventions

Classes

```
ConversationEngine

MemoryRepository

GoalManager
```

Functions

```
generate_reply()

extract_memory()

load_configuration()
```

Variables

```
conversation_context

relationship_profile

reply_confidence
```

Constants

```
MAX_HISTORY_MESSAGES

DEFAULT_TIMEOUT
```

---

# Documentation Requirements

Every significant feature should update any affected documentation, **in the same commit as the code**.

| If you change… | Update |
|---|---|
| A requirement | `PROJECT_SPEC.md` |
| Layers, components or dependency rules | `ARCHITECTURE.md`, `MASTER_ARCHITECTURE.md` |
| An entity, invariant or domain term | `DOMAIN_MODEL.md` **first**, then `DATABASE.md` |
| The schema | `DATABASE.md` + a reversible Alembic migration |
| A port or contract | `API.md` |
| AI behaviour, retrieval or providers | `AI_MODELS.md` |
| A prompt | `PROMPTS.md` + registry version + evaluation run |
| Embeddings or retrieval scoring | `VECTOR_SEARCH.md` |
| A security control | `SECURITY.md` |
| A data flow or retention default | `PRIVACY.md` |
| An error type or retry policy | `ERROR_HANDLING.md` |
| A configuration key | `CONFIGURATION.md` |
| A plugin hook | `PLUGIN_SYSTEM.md` |
| Sequencing | `ROADMAP.md` |
| An architectural decision | `DECISIONS.md` (new ADR; never overwrite an old one) |
| Anything user-visible | `CHANGELOG.md` |

The domain model is the source from which the schema is derived, not the reverse. Changing a table without first changing `DOMAIN_MODEL.md` is backwards.

Documentation should evolve with the code, never after it.

---

# Testing Requirements

Every contribution should include appropriate tests.

Possible test types:

- Unit
- Integration
- End-to-end
- AI evaluation (if applicable)

No feature is considered complete without verification.

---

# Pull Request Checklist

Before submitting:

- Code builds successfully.
- Tests pass.
- Documentation updated.
- No unnecessary dependencies added.
- No debugging code left behind.
- No secrets committed.
- Commit history is clean.

---

# Dependency Policy

Before introducing a dependency:

- Confirm active maintenance.
- Review licensing.
- Evaluate security history.
- Justify why it is needed.

Prefer existing project dependencies when possible.

---

# Error Handling

Code should:

- Fail predictably.
- Produce meaningful error messages.
- Avoid exposing internal implementation details.
- Log useful diagnostic information.

Do not silently ignore exceptions.

---

# Performance Guidelines

Consider performance when:

- Reading conversation history.
- Querying memory.
- Performing AI inference.
- Rendering the UI.

Optimize only after measuring.

---

# Security Guidelines

Never commit:

- API keys
- Session files
- Passwords
- Authentication tokens
- Personal user data

Follow `SECURITY.md` for all security-related work.

---

# AI Development Guidelines

When modifying prompts or AI behavior:

- Update `PROMPTS.md`.
- Update `AI_MODELS.md` if model usage changes.
- Run AI evaluation benchmarks.
- Record significant architectural decisions in `DECISIONS.md`.

AI behavior changes should be intentional and documented.

---

# Refactoring Policy

Refactoring should:

- Preserve behavior.
- Improve maintainability.
- Reduce complexity.
- Maintain or improve test coverage.

Avoid combining major refactoring with unrelated feature work.

---

# Code Review Checklist

Reviewers should verify:

- Correctness
- Readability
- Architecture compliance
- Documentation updates
- Test coverage
- Security implications
- Performance considerations

Feedback should focus on improving the project, not personal coding style.

---

# Issue Reporting

When reporting a bug, include:

- Description
- Expected behavior
- Actual behavior
- Steps to reproduce
- Environment
- Relevant logs (with sensitive data removed)

Feature requests should describe the problem being solved before proposing a solution.

---

# Release Process

Before a release:

- Complete roadmap milestone.
- Pass all required tests.
- Review documentation.
- Verify migrations.
- Review security checklist.
- Update CHANGELOG.md.

Tag releases using semantic versioning.

Example:

```
v1.0.0

v1.1.0

v2.0.0
```

---

# Communication Principles

Contributors should:

- Ask questions when requirements are unclear.
- Explain architectural trade-offs.
- Prefer evidence over assumptions.
- Keep discussions respectful and focused on the project.

Document important decisions rather than relying on memory.

---

# Continuous Improvement

This guide is a living document.

As the project evolves:

- Improve workflows.
- Simplify processes.
- Remove outdated guidance.
- Add new best practices.

Changes to this document should be discussed and documented when they significantly affect the development process.

---

# Final Principle

Write code that another engineer—or your future self—can understand six months later.

A successful contribution is not only one that works, but one that is easy to maintain, test, document, and extend.