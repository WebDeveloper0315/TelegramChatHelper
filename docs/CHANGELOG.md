# CHANGELOG.md

All notable changes to this project will be documented in this file.

This project follows the principles of **Keep a Changelog** and **Semantic Versioning (SemVer)**.

Version numbers follow:

```
MAJOR.MINOR.PATCH
```

Examples:

```
1.0.0
1.1.0
1.1.1
2.0.0
```

---

# Change Categories

Use the following categories when applicable:

- Added
- Changed
- Deprecated
- Removed
- Fixed
- Security
- Performance
- Documentation
- AI
- Database
- Breaking Changes

Not every release requires every category.

---

# [Unreleased]

## Added

- Initial project documentation.
- Project architecture specification.
- AI model strategy.
- Prompt engineering specification.
- Database design specification.
- Internal API specification.
- Development roadmap.
- Security guidelines.
- Testing strategy.
- Contribution guidelines.

## Changed

- None.

## Deprecated

- None.

## Removed

- None.

## Fixed

- None.

## Security

- Initial security policy documented.

## Performance

- None.

## Documentation

- Added foundational engineering documentation.

## AI

- Defined AI architecture.
- Defined prompt workflow.
- Defined model abstraction strategy.

## Database

- Initial database schema designed.

## Breaking Changes

- None.

---

# [0.1.0] - 2026-07-28

## Added

### Project

- Repository initialized.
- Clean Architecture adopted.
- Domain-driven folder structure.

### Telegram

- Project planning for TDLib integration.

### Database

- Initial SQLite schema.
- Repository abstraction.

### AI

- Provider abstraction.
- Prompt management.
- Memory retrieval design.
- Conversation planner.
- Reply generator interfaces.

### Documentation

Added:

- CLAUDE.md
- PROJECT_SPEC.md
- ARCHITECTURE.md
- AI_MODELS.md
- PROMPTS.md
- DATABASE.md
- API.md
- ROADMAP.md
- SECURITY.md
- TESTING.md
- CONTRIBUTING.md
- DECISIONS.md

---

# Release Template

Use the following template for future releases.

```
# [Version] - YYYY-MM-DD

## Added

-

## Changed

-

## Deprecated

-

## Removed

-

## Fixed

-

## Security

-

## Performance

-

## Documentation

-

## AI

-

## Database

-

## Breaking Changes

-
```

---

# Versioning Policy

## MAJOR

Increment when:

- Breaking API changes.
- Major architectural redesign.
- Incompatible database schema.
- Significant plugin API changes.

Examples

```
1.0.0 → 2.0.0
```

---

## MINOR

Increment when:

- New features.
- New AI capabilities.
- New plugins.
- New database tables.
- New UI functionality.

Examples

```
1.1.0 → 1.2.0
```

---

## PATCH

Increment when:

- Bug fixes.
- Documentation improvements.
- Small performance improvements.
- Prompt refinements that do not change external behavior significantly.
- Internal refactoring without breaking compatibility.

Examples

```
1.1.0 → 1.1.1
```

---

# AI Change Policy

Whenever AI behavior changes, document:

- Prompt updates.
- Model changes.
- Embedding model changes.
- Memory retrieval changes.
- Context-building strategy.
- Confidence estimation adjustments.
- Evaluation benchmark improvements.

Example

```
## AI

- Updated reply generation prompt to improve conversational consistency.
- Switched embedding model to improve semantic retrieval.
- Refined memory ranking algorithm.
```

---

# Database Change Policy

Document:

- New tables.
- Schema changes.
- Index additions.
- Migration updates.
- Data model redesigns.

Example

```
## Database

- Added relationship_profiles table.
- Introduced message_topics table.
- Improved indexing for conversation history queries.
```

---

# Documentation Policy

Whenever project documentation changes, record:

- New documents.
- Major revisions.
- Removed documentation.
- Architectural updates.

Example

```
## Documentation

- Updated ARCHITECTURE.md with plugin lifecycle.
- Expanded SECURITY.md with backup policy.
- Revised ROADMAP.md milestone ordering.
```

---

# Security Policy

Security-related entries should include:

- Vulnerability fixes.
- Dependency updates addressing security issues.
- Authentication improvements.
- Encryption enhancements.
- Privacy-related changes.

Example

```
## Security

- Improved session file protection.
- Updated dependency to address a known vulnerability.
```

---

# Breaking Changes Policy

Always describe:

- What changed.
- Why it changed.
- How developers should migrate.
- Whether compatibility layers exist.

Example

```
## Breaking Changes

Repository interfaces now return typed domain objects instead of dictionaries.

Migration:

Update repository consumers to use the new domain models.
```

---

# Release Checklist

Before creating a release:

- All planned roadmap tasks completed.
- Tests passing.
- Documentation synchronized.
- Database migrations verified.
- Security review completed.
- AI evaluation completed.
- Version number updated.
- CHANGELOG.md updated.

---

# Maintenance Guidelines

- Add entries as work is completed rather than waiting until release day.
- Keep entries concise but informative.
- Avoid duplicate information from commit messages.
- Group related changes together.
- Preserve historical entries; never rewrite published release notes.

---

# Philosophy

The changelog is the historical record of the project.

It should answer:

- What changed?
- Why did it change?
- Does it affect users or developers?
- Does it require migration?
- Does it improve security, performance, or AI quality?

Every release should leave a clear, accurate trail of the project's evolution.