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

1. Read `PROJECT_SPEC.md`.
2. Review `ARCHITECTURE.md`.
3. Check `DECISIONS.md` for relevant architectural decisions.
4. Verify the current milestone in `ROADMAP.md`.
5. Ensure the proposed work aligns with project goals.

If a change affects architecture, create or update an ADR in `DECISIONS.md`.

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

Every significant feature should update any affected documentation.

Potential updates include:

- PROJECT_SPEC.md
- ARCHITECTURE.md
- DATABASE.md
- API.md
- ROADMAP.md
- DECISIONS.md
- TESTING.md

Documentation should evolve with the code.

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