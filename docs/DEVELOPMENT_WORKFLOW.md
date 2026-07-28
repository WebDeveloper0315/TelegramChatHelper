# DEVELOPMENT_WORKFLOW.md

# Telegram AI Conversation Assistant

Development Workflow

Version: 1.0

Status: Active

---

# 1. Purpose

This document defines the development workflow for the project.

It establishes how new features are planned, designed, implemented, tested, documented, reviewed, and maintained.

Every development task should follow this workflow.

---

# 2. Core Principles

Development should prioritize:

- Simplicity
- Maintainability
- Testability
- Modularity
- Incremental progress
- Documentation
- Security
- Performance
- User privacy

Never sacrifice long-term maintainability for short-term convenience.

---

# 3. Claude's Role

Claude acts as:

- Software Architect
- Senior Software Engineer
- Code Reviewer
- Prompt Engineer
- AI Engineer
- Database Designer
- Technical Writer
- QA Engineer

Claude should recommend best practices, explain trade-offs, and identify risks before implementation.

Claude should not make major architectural changes without presenting the rationale and obtaining user approval.

---

# 4. Development Lifecycle

Every feature follows this sequence:

Requirement

↓

Analysis

↓

Architecture

↓

Design

↓

Implementation

↓

Testing

↓

Documentation

↓

Review

↓

Refactoring

↓

Approval

↓

Merge

No step should be skipped without documented justification.

---

# 5. Feature Development Process

For every feature:

1. Understand the requirement.
2. Review related documentation.
3. Identify affected modules.
4. Identify dependencies.
5. Propose an implementation plan.
6. Explain trade-offs.
7. Wait for approval if architecture changes.
8. Implement in small increments.
9. Add or update tests.
10. Update documentation.
11. Perform a self-review.

---

# 6. Documentation First

Before coding:

Review

- PROJECT_SPEC.md
- ARCHITECTURE.md
- API.md
- DATABASE.md
- ROADMAP.md
- DECISIONS.md

If documentation becomes outdated after implementation, update it before considering the task complete.

---

# 7. Implementation Rules

Features should be:

- Small
- Modular
- Independently testable
- Loosely coupled
- Well documented

Avoid implementing multiple unrelated features in one task.

---

# 8. Architecture Changes

If a task changes architecture:

Claude should:

1. Explain the reason.
2. Present alternatives.
3. Explain trade-offs.
4. Recommend an approach.
5. Wait for approval.
6. Update DECISIONS.md.
7. Update ARCHITECTURE.md if accepted.

---

# 9. Coding Standards

Every implementation should:

- Follow SOLID principles.
- Respect Clean Architecture.
- Use dependency injection where appropriate.
- Avoid global state.
- Avoid duplicated logic.
- Keep functions focused on a single responsibility.
- Prefer composition over inheritance.

---

# 10. Task Breakdown

Large features should be divided into smaller tasks.

Example

Memory Engine

↓

Memory Model

↓

Repository

↓

Extraction

↓

Ranking

↓

Retrieval

↓

Tests

↓

Documentation

Each task should leave the project in a working state.

---

# 11. Testing Workflow

Before marking a feature complete:

Run

- Unit tests
- Integration tests (if applicable)
- Static analysis
- Type checking
- AI evaluation (if applicable)

Fix failures before continuing.

---

# 12. Documentation Workflow

Whenever code changes:

Update affected documentation.

Possible documents include:

- PROJECT_SPEC.md
- ARCHITECTURE.md
- API.md
- DATABASE.md
- AI_MODELS.md
- PROMPTS.md
- ROADMAP.md
- DECISIONS.md
- CHANGELOG.md
- TESTING.md

Documentation should remain synchronized with implementation.

---

# 13. Self-Review Checklist

Before presenting work:

Verify:

- Code compiles.
- Tests pass.
- Documentation updated.
- Naming is consistent.
- No debugging code remains.
- No unnecessary dependencies.
- No secrets included.

---

# 14. Refactoring Policy

Refactor only when it:

- Simplifies the design.
- Reduces duplication.
- Improves readability.
- Improves maintainability.
- Preserves behavior.

Avoid mixing major refactoring with unrelated feature development.

---

# 15. Bug Fix Workflow

For every bug:

1. Reproduce the issue.
2. Identify the root cause.
3. Write or update a test.
4. Implement the fix.
5. Verify the fix.
6. Document significant changes.
7. Update CHANGELOG.md.

---

# 16. AI Development Workflow

When changing AI behavior:

1. Review PROMPTS.md.
2. Review AI_MODELS.md.
3. Update prompts if necessary.
4. Run AI evaluation tests.
5. Compare benchmark results.
6. Document significant improvements.

Avoid changing prompts without evaluation.

---

# 17. Dependency Management

Before adding a dependency:

Evaluate:

- Maintenance status
- Community adoption
- License
- Security history
- Long-term viability

Document significant dependency additions in DECISIONS.md.

---

# 18. Performance Workflow

Optimize only after measuring.

Typical process:

Measure

↓

Identify bottleneck

↓

Implement improvement

↓

Benchmark

↓

Compare

↓

Document results

Avoid premature optimization.

---

# 19. Release Workflow

Before a release:

- Complete roadmap milestone.
- Run all required tests.
- Update documentation.
- Review security.
- Verify database migrations.
- Update CHANGELOG.md.
- Tag the release.

---

# 20. Communication Rules

Claude should:

- Explain reasoning.
- Highlight assumptions.
- Present alternatives.
- Recommend the preferred solution.
- Identify risks.
- Ask for clarification when requirements are ambiguous.

Do not assume missing requirements.

---

# 21. Progress Reporting

After completing a task, report:

Completed

- What was implemented.

Tests

- What was verified.

Documentation

- What was updated.

Next Steps

- Recommended next task.

Blockers

- Any unresolved issues.

---

# 22. Definition of Done

A task is complete only when:

- Requirements are satisfied.
- Code builds successfully.
- Tests pass.
- Documentation is updated.
- No critical issues remain.
- Architecture remains consistent.
- Security implications have been considered.

---

# 23. Continuous Improvement

The workflow should evolve as the project grows.

Improvements may include:

- Better tooling
- Improved automation
- Additional testing
- Documentation refinements
- Workflow simplification

Record significant workflow changes in DECISIONS.md.

---

# 24. Engineering Philosophy

The project should be developed as though it will be maintained for many years.

Every decision should balance:

- Simplicity
- Flexibility
- Performance
- Reliability
- Maintainability
- User experience

---

# 25. Final Principle

Every task should leave the project in a better state than before.

When in doubt:

- Prefer clarity over cleverness.
- Prefer modularity over shortcuts.
- Prefer documented decisions over implicit assumptions.
- Prefer incremental progress over large, risky changes.