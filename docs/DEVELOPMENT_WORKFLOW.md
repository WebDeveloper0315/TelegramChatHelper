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
---

# 26. Obtaining `tdjson`

Telegram connectivity needs TDLib's `tdjson` shared library. It is **not** a
Python dependency and `uv sync` does not provide it. Everything except live
Telegram tests runs without it, so this is only needed when working on the
Telegram layer.

`tdjson` is loaded into the application's own process and sees the session key,
every message and the network. It is therefore checksum-verified against a
pinned manifest before it is loaded, and nothing is trusted by default
(ADR-047).

## 26.1 Supported platforms

| Platform | Manifest key | File |
|---|---|---|
| Windows x86-64 | `windows-amd64` | `tdjson.dll` |
| Linux x86-64 | `linux-amd64` | `libtdjson.so` |
| Linux ARM64 | `linux-arm64` | `libtdjson.so` |
| macOS Apple Silicon | `darwin-arm64` | `libtdjson.dylib` |
| macOS Intel | `darwin-amd64` | `libtdjson.dylib` |

Any platform Python runs on and TDLib builds for will work; those are the ones
the project intends to support. The application refuses a library whose reported
TDLib version is below `telegram.minimum_version` (default `1.8.0`), because the
client API this project uses stabilised there.

## 26.2 Obtaining a library

**Build it** — the highest-provenance option, and the only one where the chain
from source to loaded library is yours. On Windows this is one command:

```
scripts\build-tdjson.bat [build-root] [tdlib-commit]
```

That script *is* the procedure. It is committed rather than described because
an earlier prose version of this section was written without being run and was
wrong in three places — the corrections are noted below, and a script that
demonstrably produced the recorded binary is worth more than instructions that
might not.

It clones vcpkg, builds OpenSSL and zlib **statically** from source, fetches
TDLib at a pinned commit, and builds the `tdjson` target. Around an hour on a
first run, most of it OpenSSL. Requires `git` and Visual Studio 2022 or newer
with the C++ workload; CMake and Ninja ship with Visual Studio.

Three things the obvious recipe gets wrong, all found by running it:

1. **`-G Ninja`, not the default generator.** The CMake bundled with Visual
   Studio 18 offers no VS18 generator, so the IDE generator is not always
   available even when the IDE is. Ninja ships alongside it and works.
2. **`-DGPERF_EXECUTABLE` must be given.** vcpkg installs host tools under
   `installed\<triplet>\tools\<port>\`, which is not on `PATH` and not
   somewhere TDLib's `find_program` looks. Configure fails with
   *"Could NOT find gperf"* otherwise.
3. **The vcpkg toolchain file and triplet must both be passed**, and the
   `install` line and the `configure` line must name the *same* triplet.
   Mismatching them installs dynamic packages and then fails to find static
   ones, in an error that looks like a toolchain problem rather than a typo.

**On Linux and macOS**, TDLib's own build instructions apply, with OpenSSL and
zlib linked statically. Those steps are **not scripted here and have not been
run on this project** — a script nobody has executed is worse than none, because
it implies a verification that did not happen.

**Or obtain a prebuilt one.** TDLib publishes no binaries itself: its GitHub
releases carry no assets. Distributions package it (`libtdjson` on several Linux
distributions, `brew install tdlib` on macOS), and third-party builds exist on
NuGet and PyPI. Any of these is acceptable *if* you can say where it came from —
that sentence is what the manifest entry is *for*. Check whether it is
dynamically linked before recording it (see the limitations below); most
prebuilt binaries are.

## 26.3 Installing it

Either place it where the application looks:

```
<data_dir>/tdlib/tdjson.dll          # or libtdjson.so / .dylib
<data_dir>/tdlib/1.8.29/tdjson.dll   # a versioned layout also works
```

or name it explicitly:

```yaml
telegram:
  tdjson_path: D:/tools/tdlib/tdjson.dll
```

`TGASSIST_TELEGRAM__TDJSON_PATH` sets the same value from the environment.
**Naming a file does not trust it** — a configured path is the highest
precedence candidate, not an exemption from verification.

## 26.4 Recording it in the manifest

```
tgassist tdlib verify
```

On a library that is not yet trusted this prints its digest and the exact entry
to add. Add it to
`src/tgassist/infrastructure/telegram/tdjson_manifest.json`, filling in `source`
with where the file actually came from and `recorded` with today's date:

```json
{
  "platform": "windows-amd64",
  "sha256": "…",
  "version": "1.8.29",
  "source": "built from tdlib/td tag v1.8.29 with MSVC 2026",
  "recorded": "2026-07-28"
}
```

`version` is optional. When present it is cross-checked against what the library
reports, which catches a stale entry pointing at a file that has been swapped.

**Review a manifest change as you would any other security change.** A digest
recorded from whatever happened to be on disk makes the whole mechanism
theatre.

Then:

```
tgassist tdlib doctor
```

## 26.5 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No tdjson library was found` | Nothing in any searched location | `doctor` lists every path it tried; put the file in one of them |
| `not a binary this application trusts` | Digest not in the manifest | Establish provenance, then `tdlib verify` and add the printed entry |
| Verification stops working after an upgrade | The bytes changed, so the digest changed | Record the new build; do **not** edit the old entry |
| `the platform refused to load it` on Linux | Missing OpenSSL or zlib | `ldd libtdjson.so` and install what is listed as missing |
| `the platform refused to load it` on Windows | Missing Visual C++ runtime, or a 32-bit library under 64-bit Python | Install the redistributable; check both are x64 |
| `does not export the TDLib client API` | A pre-1.8 build with only `td_json_client_*` | Build or obtain 1.8 or newer |
| `older than the minimum supported version` | TDLib below `telegram.minimum_version` | Upgrade, or lower the minimum if you have a reason |
| `loaded but did not answer a version query` | The file exports the right names but is not TDLib | Check what you actually downloaded |
| `records this digest as TDLib X, but the library reports Y` | A stale manifest entry, or a swapped file | Re-check provenance before touching the entry |

`tgassist tdlib doctor` performs the whole sequence and reports which stage
failed. A stage that was never reached reports `not checked` rather than a
failure — not checked and failed are different things.

## 26.6 Operational limitations

- **The recorded Windows binary needs the Visual C++ redistributable.**
  `scripts/build-tdjson.bat` passes `-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded`
  to request the static C runtime, and **CMake ignores it**: that variable is
  governed by policy `CMP0091`, which only takes effect for projects declaring
  `cmake_minimum_required(VERSION 3.15)` or later. TDLib declares 3.10, so the
  policy defaults to `OLD` and the runtime library stays `/MD`. The artefact
  therefore imports `msvcp140.dll` and `vcruntime140*.dll`.

  This is reported, not hidden: `tgassist tdlib doctor` lists them as
  *redistributable* and says the target machine needs them. It is not a security
  problem — they are Microsoft-signed components — but it is a deployment fact.

  Adding `-DCMAKE_POLICY_DEFAULT_CMP0091=NEW` fixes it. The script deliberately
  does **not** carry that flag yet, because changing it changes the artefact and
  therefore its digest, and the committed manifest entry describes the binary
  the script produces today. Reproducibility comes first; apply the flag and
  re-record deliberately.
- **The manifest ships with one entry**, for `windows-amd64`, built from source.
  Every other platform trusts nothing and must record its own binary. This is
  deliberate.
- **The manifest is per-repository, not per-machine.** Two developers on
  different platforms each add an entry; both entries are committed.
- **A TDLib upgrade requires a manifest change**, and a stale manifest blocks
  startup. That is the correct failure direction, but it will be inconvenient
  at least once.
- **There is no escape hatch.** No configuration setting loads an unverified
  library. Recording an entry is one command, and an opt-out would become the
  documented path within a week.
- **Verified is not audited.** A checksum proves the file has not changed since
  someone recorded it. It proves nothing about whether they were right to.
- **The manifest verifies one file.** If that file loads further libraries at
  runtime, those are inside the trust boundary and are *not* checked. This is
  why the documented build links OpenSSL and zlib **statically**: it makes the
  artefact that is checksummed the whole of what gets loaded.

  The gap is easy to miss precisely because nothing breaks. CPython resolves a
  library path in full and adds that directory to the search order
  (`LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR`), so a dynamically linked `tdjson` with
  `libcrypto`, `libssl` and `zlib1` beside it loads and works perfectly — while
  three unverified files sit inside the trust boundary.

  A `tdjson` obtained elsewhere is likely to be dynamically linked. Check before
  recording it: `dumpbin /dependents tdjson.dll` on Windows, `ldd libtdjson.so`
  on Linux. If it names OpenSSL or zlib, the checksum covers less than it
  appears to, and the manifest `source` field should say so.
