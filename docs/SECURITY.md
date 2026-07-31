# SECURITY.md

# Telegram AI Conversation Assistant

Security Specification

Version: 2.0

Status: Active

Last Updated: 2026-07-28

---

# 1. Purpose

This document defines the project's security requirements: what is protected, from whom, and by what mechanism.

Privacy principles, data lifecycle and third-party data rights are specified separately in `PRIVACY.md`. This document covers the technical controls; that one covers the commitments they enforce.

Security is a design requirement, not a feature.

---

# 2. Security Principles

- Least privilege
- Defense in depth
- Secure by default
- Privacy by design
- Fail securely — a failed control denies rather than permits
- Explicit consent for anything that leaves the device

---

# 3. Threat Model

Stating what the application defends against, and what it does not, so the controls below can be judged.

## In scope

| Threat | Asset | Control |
|---|---|---|
| Casual local access to files (shared or portable machine) | Conversation database, attachments | Owner-only ACLs; optional full-database encryption in Phase 2 (§6) |
| Theft of the Telegram session | Account takeover | Session store always encrypted; key in OS credential store (§7) |
| Secret leakage via logs, crash reports or backups | API keys, session keys | Central redaction; secrets never in database, logs or unencrypted backups (§8, §11) |
| Excessive data disclosure to AI providers | Conversation content | Per-chat data boundaries; minimum-context assembly (§13) |
| Prompt injection by a conversation partner | Suggestion integrity, memory integrity | Structural delimiting; schema validation; no tools in generation; memory proposals (§12) |
| Malicious or buggy plugin | Everything in-process | Honest trust model, API versioning, failure isolation, advisory permissions (§14) |
| SQL injection | Database | Parameterized queries only; enforced by SQLAlchemy Core expression API (§10) |
| Data loss from failed migration or corruption | All user data | Pre-migration backup, verified backups, integrity checks (§16) |
| Backup exfiltration | All user data | Mandatory encryption for backups outside the app data directory (§16) |
| Account restriction by Telegram | Access to the service | No automation, no synthetic typing, rate limiting (§15) |

## Explicitly out of scope

| Threat | Why |
|---|---|
| A local attacker with administrator or root privileges | They can read process memory, keylog, and access the credential store as the user. No application-level control survives this. |
| Malicious plugin code the user chose to install | In-process Python cannot be sandboxed meaningfully (ADR-025). The control is user judgement, and we say so rather than implying protection we do not provide. |
| Compromise of an AI provider's infrastructure | Outside our control. Mitigated only by the user's ability to choose local models or exclude sensitive chats. |
| Telegram platform compromise | Outside our control. |
| Physical device compromise with full-disk access and the user logged in | The OS session is the boundary; ours is inside it. |

Being explicit here matters: a threat model that claims to cover everything protects nothing, because it gives no basis for judging whether a control is adequate.

---

# 4. Protected Assets

Ranked by consequence of compromise.

| Rank | Asset | Consequence | Protection |
|---|---|---|---|
| 1 | Telegram session / auth key | **Full account takeover** | Always encrypted; key in OS credential store; never backed up; never logged |
| 2 | AI provider API keys | Financial loss, impersonation | OS credential store; never in database, logs or backups |
| 3 | Conversation history | Serious privacy harm to the user **and to third parties** | Local-first; owner-only ACLs; per-chat AI boundaries; optional encryption (Phase 2) |
| 4 | Long-term memory database | Concentrated personal profiles of third parties | As above, plus user-visible review and deletion |
| 5 | Backups | Same as 3 and 4, in a more portable form | Encrypted by default outside the app directory; verified; session data excluded |
| 6 | Audit log | Forensic value | Append-only; immune to retention deletion |
| 7 | Configuration and settings | Low direct value | Standard file permissions |

---

# 5. Authentication

1. Telegram authentication is performed by the official client library; the application never handles the MTProto auth protocol itself.
2. **Authentication codes and 2FA passwords are never logged, never stored, never retained after use.** They pass from the `AuthorizationHandler` to the gateway and are discarded.
3. The authorization state machine is explicit (`DOMAIN_MODEL.md` §5.3); only the `ready` state permits sending.
4. Logout destroys local session material and writes an audit event.
5. **Session data is never restored from a backup.** A restore requires re-authentication, by design — a session restored onto a different machine is a security hazard, not a convenience.
6. Phone numbers are stored as salted hashes; the plaintext is never persisted.

---

# 6. Local Data Encryption

Phased strategy per ADR-022. The phasing follows asset value, not convenience.

## Phase 1 — MVP (Milestones 0–10)

| Asset | Encrypted | Mechanism |
|---|---|---|
| Secrets | **Always** | OS credential store (DPAPI / Keychain / Secret Service) |
| Telegram session | **Always** | TDLib `database_encryption_key`; key in OS credential store |
| Application database | No | Owner-only filesystem ACLs, applied at creation and verified at startup |
| Attachments | No | Owner-only ACLs |
| Backups (inside app directory) | Optional | AES-256-GCM, passphrase-derived key |
| Backups (outside app directory) | **Always** | AES-256-GCM, passphrase-derived key |

**This is stated plainly to users in onboarding and in `PRIVACY.md`:** in Phase 1, conversation data is readable by anyone with filesystem access to the user's account. Users on shared machines should know this before they sync.

## Phase 2 — v1.0

Optional full-database encryption via SQLite3MultipleCiphers, per profile, key in the OS credential store, with a tested bidirectional migration. All database access already flows through one `DatabaseEngine` factory, so enabling it is a contained change (ADR-022).

## Key handling rules

1. Encryption keys exist only in the OS credential store and in process memory while in use.
2. Keys are never written to disk, logs, backups or the database.
3. A key is referenced everywhere by **name**, never by value (`encryption_key_ref`, `api_key_ref`).
4. Losing the passphrase for an encrypted backup means the backup is unrecoverable — stated at creation time, with no recovery mechanism, because a recovery mechanism would be a backdoor.

---

# 6a. Native Library Trust

`tdjson` is loaded into this process with `ctypes`. It sees the session key,
every message, and the network. Whatever supplies it is therefore **as trusted
as the application itself** — it is not a dependency in the ordinary sense, it
is part of the trusted computing base.

## What is checked, and in what order

Each step runs only if the one before it passed, and each failure names a
different remedy (ADR-047).

| # | Check | Rejects |
|---|---|---|
| 1 | **Discovery** — a documented search order | Nothing found anywhere |
| 2 | **Checksum** — SHA-256 against a pinned, committed manifest | A binary nobody recorded |
| 3 | **Architecture** — read from the file's own headers | A library built for another machine |
| 4 | **Dependencies** — imports read from the file's headers | Unverified code inside the trust boundary |
| 5 | **Load** — the platform maps it | A missing transitive dependency |
| 6 | **Entry points** — the client API is exported | A real TDLib of the wrong vintage |
| 7 | **Version** — reported by the library, cross-checked against the manifest | Too old, or not what was recorded |

Steps 3 and 4 happen **before** the library is mapped into the process. An
untrusted or mismatched binary is never given a chance to execute.

## The search never falls back

The first candidate that *exists* is the one used. If it fails any check, the
search **stops** — it does not try the next location. Falling through would mean
that planting a library in a high-precedence directory earns a silent retry
elsewhere rather than a refusal, and would make "which library am I actually
running" depend on the failure mode. Only an *absent* candidate advances the
search.

There is **no configuration setting that loads an unverified library.**
Recording an entry is one command; an opt-out would become the documented path
within a week.

## Why dependencies are checked

The manifest checksums **one file**. Anything that file loads at runtime is
inside the trust boundary and is not covered by the digest.

The gap is invisible rather than noisy. CPython resolves a library path in full
and adds its directory to the search order, so a dynamically linked `tdjson`
with `libcrypto`, `libssl` and `zlib1` beside it **loads and works perfectly**
while three unverified files sit inside the boundary. Nothing fails. That is why
imports are read from the binary's headers and OpenSSL and zlib are rejected
outright, and why the documented build links them statically — so that the
artefact which is checksummed is the whole of what gets loaded.

Imports are parsed directly rather than by shelling out to `dumpbin` or `ldd`:
those need a toolchain present, differ per platform, and cannot be tested
without one.

## Artefact verification procedure

For any `tdjson`, from any source:

1. Establish **where it came from**. A checksum proves a file has not changed
   since somebody recorded it; it proves nothing about whether they were right
   to. This step is the one that cannot be automated.
2. `tgassist tdlib verify` — prints the digest and, if unrecognised, the exact
   manifest entry to add.
3. Confirm it is **self-contained**. `tgassist tdlib doctor` lists every runtime
   dependency and rejects OpenSSL, zlib and anything unrecognised.
   Independently: `dumpbin /dependents` on Windows, `ldd` on Linux.
4. Add the entry to `src/tgassist/infrastructure/telegram/tdjson_manifest.json`,
   with `source` describing the provenance in a sentence and `version` set so
   the cross-check in step 7 above has something to compare against.
5. `tgassist tdlib doctor` — every stage must report `ok`.

**Review a manifest change as you would any other security change.** A digest
recorded from whatever happened to be on disk makes the whole mechanism theatre.

## What the bridge may log

TDLib frames are not log material. A single frame can carry an authorization
code, a session encryption key, a phone number or message text -- the four
things `section 9` forbids most strongly.

The receive bridge therefore logs **only** a frame's `@type`, never its body,
and never at any level. This is upstream of the redaction processor rather than
relying on it: redaction is keyed on field names, and TDLib's field names are
its own, so a frame logged wholesale could carry a key the processor has never
heard of.

Counts are safe and are reported: frames received, malformed frames, queue
depth and high-water mark, pending requests. None of them is content.

## Limits of this

- **Verified is not audited.** The checksum pins an artefact; it says nothing
  about the code inside it.
- **Coverage is per format.** PE is fully parsed. ELF yields architecture but
  not imports. Mach-O is not parsed. Every gap reports *not checked* rather than
  passing — an unverified platform must never read as a verified one.
- **The manifest ships empty.** A fresh checkout trusts nothing, deliberately.

---

# 7. Session Security

1. The Telegram session store is always encrypted with a key generated on first run and stored in the OS credential store.
2. The session directory is created with owner-only permissions; `tgassist doctor` verifies them and warns if they have been widened.
3. Session paths appear in configuration; session **contents** are never read, logged or exported by the application.
4. Logout destroys the local session store and the associated key.
5. Sessions are excluded from every export and every backup format.
6. If the credential store becomes unavailable, the application **refuses to start** rather than falling back to an unencrypted session. Failing securely means denying.

**Enforced since Milestone 2.4**, and carried unenforced from Milestone 0 until then — there was no session to protect, so the gap was theoretical until a session key existed.

`Container.start()` checks the credential store *before* opening the database, so a refusal has not already done work it promised not to do. It is the entry point every command that touches user data uses. Diagnostic commands deliberately do not: `doctor` exists to *report* an unavailable credential store, and refusing to run it would remove the tool that explains the refusal.

The rule is governed by `security.require_secret_store`, which the `development` and `testing` profiles set to `false` so a developer without a credential backend is not locked out. Preparing a session refuses **regardless** of that flag: the flag governs startup, and there is nowhere else a session key may go.

## Credentials during login

The login flow touches three things Telegram treats as secret, and none of them
is retained.

1. **Phone number, login code and two-step password** exist for exactly one
   submission. Each lives in a local for the duration of one request; no branch
   in the gateway or the handler puts one in an attribute, a log, an error or a
   retry cache. `ConsoleAuthorizationHandler` has two slots — an attempt counter
   and its limit — so there is nowhere for one to survive, and a test asserts
   that shape rather than trusting it.
2. **The password is read with `getpass`**, so it never reaches the screen, the
   terminal's scrollback or the shell's history. The login code is not: it is
   short-lived, useless once submitted, and a user who cannot see what they
   typed will mistype it.
3. **A rejection reports Telegram's reason, never the value.** The mapped error
   carries `operation`, `telegram_code` and `telegram_message` and nothing else
   — an error object is exactly the thing that ends up in a log or a crash
   report.

The application's own credentials follow §8: `telegram.api_id` is ordinary
configuration, and the application hash is a **name** in the credential store
(`telegram.api_hash_ref`), never a value in a file.

---

# 8. Secret Management

Per ADR-021.

1. All secrets are accessed through the `SecretStore` port; no other component holds key material.
2. Resolution order: **environment variable → OS credential store → not configured**.
3. When no OS backend is available, an encrypted file store with a passphrase-derived key is used. **A plaintext fallback does not exist.**
4. Secret *names* may appear in configuration, logs and the database; secret *values* may not.
5. Objects holding secrets override `__repr__` and `__str__` to prevent accidental disclosure through exception traces and debuggers.
6. Secret scanning (`gitleaks` or equivalent) runs in pre-commit and in CI.
7. Rotation: replacing a secret takes effect on the next call; no restart required, no cached copies.

---

# 9. Logging Policy

Per ADR-027.

## Never logged

Passwords · authentication codes · 2FA passwords · API keys · session keys or contents · message content (unless diagnostic mode is explicitly enabled) · memory values · phone numbers · prompt or response bodies.

## Always logged

Errors and warnings with typed error codes · component and event names · correlation IDs · performance metrics · security-relevant state changes · AI call **metadata** (provider, model, tokens, latency, cost, outcome).

## Enforcement

Redaction is a **central `structlog` processor**, applied before emission. It is not delegated to call sites, because a single forgetful call site would defeat it. The processor redacts by field name (a deny-list of known-sensitive keys) and by pattern (token-shaped strings).

Field names are matched two ways, and the distinction matters. Most are matched as **fragments**, so `provider_api_key` and `api_key_v2` are both caught. A few must be matched as **whole keys**: `text` is the name of `Message.text`, the most sensitive field in the application, but `context` — a structural key carried by every application error — contains it, so a fragment rule would redact the diagnostic information errors exist to convey. Whole-key matching covers those cases without that cost.

## Diagnostic mode

May log message content for troubleshooting. Requires explicit opt-in, is time-limited (default 1 hour), displays a persistent indicator while active, and writes an audit event on enable and disable.

## Log retention

Default 14 days, configurable. Rotation by size and age. Logs are excluded from backups.

---

# 10. Database Security

1. **Parameterized queries only.** String-concatenated SQL is prohibited; the SQLAlchemy Core expression API makes the safe path the default path.
2. Foreign keys enforced on every connection.
3. Database files created with owner-only permissions; verified at startup.
4. **Never stored in the database:** passwords, API keys, authentication codes, session tokens, encryption keys.
5. Phone numbers stored as salted hashes only.
6. `ai_calls` stores metadata only — **never prompt content, under any setting**. The response is stored as a truncated SHA-256 digest, which is what deterministic replay compares without the answer being readable. The text itself is written only when `ai.store_responses` is enabled, which the **production profile rejects at startup** — the same arrangement `logging.diagnostic_mode` has, and the only setting in the application that can put model output on disk (ADR-057 §6).
7. `audit_log` is append-only, enforced both by the absence of mutation methods on `AuditRepository` and by database triggers.
8. Every migration is preceded by an automatic backup and is reversible (`DATABASE.md` §7).
9. `PRAGMA integrity_check` runs after any crash and after every restore.

---

# 11. AI Provider Security and Data Boundaries

Per ADR-024.

1. **Every AI provider carries a `data_boundary`**: `local` or `external`.
2. **Every chat carries an `ai_processing_mode`**: `disabled`, `local_only` (default) or `cloud_allowed`.
3. An `external` provider is **never** invoked for a chat that is not `cloud_allowed`. This is checked at the call site, not merely at configuration time.
4. **Fallback never crosses the boundary.** If the local provider fails for a `local_only` chat, the operation degrades; it does not silently escalate to a cloud provider.
5. Before the first transmission to a given provider, the user is shown which provider will receive data and which categories are included.
6. The active provider is visible in the UI whenever a suggestion is generated.
7. Only the minimum required context is transmitted — retrieved memories, summary and recent messages, never full history (`AI_MODELS.md` §8).
8. Provider changes are recorded as audit events.
9. **No telemetry.** The application performs no analytics, usage tracking or crash reporting that leaves the device without separate, explicit, off-by-default consent.

---

# 12. Prompt Injection Defenses

Conversation content is untrusted input that enters model prompts. The full analysis is in `AI_MODELS.md` §12; the security-relevant controls are:

1. **Structural delimiting.** Untrusted content occupies clearly marked slots. The system prompt states that content within them is data, never instructions. *Implemented:* a `Prompt` declares which inputs are untrusted and `render()` wraps them — the template cannot forget to (ADR-058 §4).
2. **Delimiter neutralisation.** Content is scanned and escaped so it cannot forge a slot boundary. *Implemented as* collapsing any run of three or more angle brackets to two. Visible rather than hidden, and it can never lengthen the text. **Model output derived from conversation content — such as previously-stored proposal values — is treated as untrusted for the same reason.**
3. **Schema validation on every response.** An injection producing prose instead of the required structure fails validation and never reaches the user. *Implemented*, with exactly one repair attempt and no partial results.
3a. **Nothing a model returns becomes believed state.** An injection that survived every control above produces a `MemoryProposal`, which sits in a queue until a person reads it and its quotation. This is the control the others exist to support: no known technique makes a model reliably immune to injection, so the architectural answer is that a successful one reaches nothing valuable (ADR-019, ADR-058).
3b. **Even acceptance grants nothing but storage.** A `Memory` is a row a person approved; it cannot execute, cannot send, and cannot change itself. Its *identity* is derived by the application rather than taken from the model, so an injection cannot claim the key of an existing memory or block a true fact from being stored by colliding with it (ADR-059 §2). What is accepted can be forgotten, and forgetting frees the fact to be proposed again.
4. **No capabilities in generation.** Generation prompts have no tools, no send path, no database access. There is nothing for an injection to invoke.
5. **Memory writes are proposals.** An injection attempting to plant a false memory produces a reviewable proposal, not a stored fact.
6. **Length caps** on individual messages entering a prompt, bounding payload space.
7. **Regression coverage.** Injection attempts are part of the evaluation corpus; a prompt change that weakens resistance is caught before release.

**Stated limitation:** no known technique makes a language model reliably immune to injection. The architectural response is to ensure a successful injection reaches nothing of value — it cannot send, write memory, call tools, or escape validation.

---

# 13. Network Security

1. TLS certificate verification is always enabled; certificate validation is never disabled, including in development configurations.
2. Provider endpoints are configurable but validated as HTTPS, except explicit localhost endpoints for local model servers.
3. Network failures are handled gracefully with bounded retries (`AI_MODELS.md` §16).
4. No unnecessary metadata is transmitted; requests carry only what the task requires.
5. Outbound connections are limited to configured providers and Telegram. The application has no update-check, no analytics endpoint, and no other outbound traffic.

---

# 14. Plugin Security

Per ADR-025. The trust model is stated honestly rather than implied to be stronger than it is.

1. **Plugins are trusted code.** They run in-process with full access to the process, the database file and the session. Installing a plugin is equivalent to installing an application. This is displayed at install time.
2. **Declared permissions are advisory in v1.0** — shown to the user and logged, not enforced by a sandbox. They are **not presented as a security control**.
3. Plugins access data only through `PluginContext`; direct database access is a violation and is detectable in review.
4. API version compatibility is verified before loading; incompatible plugins are refused.
5. Every hook invocation is wrapped. A raising plugin is logged, counted and disabled for the session; it never halts the application.
6. Plugin installation, enablement and disablement are audit events.
7. Plugins are loaded only from configured sources; there is no automatic discovery from arbitrary paths.

Future work (post-1.0) may add subprocess isolation with IPC, which would provide a genuine boundary. Until then, the honest position is that the control is user judgement.

---

# 15. Platform and Account Safety

1. **The application never sends a message without explicit user approval** (ADR-023). There is no unattended auto-reply mode.
2. **The application never emits synthetic typing indicators.** The `TelegramGateway` port has no method for it — the constraint is structural.
3. Rate limiting: `FLOOD_WAIT` responses are honoured with bounded exponential backoff; history backfill is throttled.
4. Synchronisation scope is bounded and user-selected, not a full account mirror.
5. Onboarding discloses the account-restriction risk inherent in any third-party client and recommends testing with a secondary account (`TESTING.md` §13).

---

# 16. Backup and Recovery Security

1. Backups **never** contain secrets, session data, encryption keys or logs.
2. Backups written outside the application data directory are **always encrypted** (AES-256-GCM, passphrase-derived key).
3. Every backup includes a manifest with SHA-256 checksums, the schema revision and the application version.
4. Every backup is verified immediately after creation: checksums recomputed, `PRAGMA integrity_check` run, row counts compared. An unverified backup is reported as failed.
5. Restore validates the manifest and schema compatibility, backs up the current database first, then replaces atomically.
6. Restore requires re-authentication with Telegram.
7. Backup creation and restoration are audit events.
8. A lost backup passphrase means unrecoverable data. There is no recovery mechanism, because one would be a backdoor.

---

# 17. Dependency Security

1. Before adding a dependency: verify active maintenance, review the licence, check known advisories, prefer widely used libraries, and justify the need (`CONTRIBUTING.md`).
2. `pip-audit` runs in CI; a known-vulnerable dependency fails the build.
3. Dependencies are pinned via `uv.lock` for reproducible builds.
4. Unused dependencies are removed promptly.
5. **Native binaries require special care.** A prebuilt `tdjson` binary has full access to the Telegram session; its provenance must be documented and its checksum verified before use (ADR-012).
6. AI provider SDKs are optional extras, so a user who never enables a provider never installs its SDK.

---

# 18. Secure Development Practices

1. Type checking (`mypy --strict` on domain and application) in CI.
2. Linting and formatting (`ruff`) in CI.
3. Architectural contract enforcement (`import-linter`) in CI.
4. Secret scanning in pre-commit and CI.
5. Dependency vulnerability scanning in CI.
6. Security implications considered in every code review (`CONTRIBUTING.md`).
7. Every bug fix includes a regression test.

---

# 19. Audit Logging

Per ADR-027 §3. Audit events are durable, queryable and append-only — distinct from application logs, which are high-volume and disposable.

**Recorded:** login · logout · session created/destroyed · provider enabled/disabled · AI processing mode changed · data exported · data deleted · contact purged · retention applied · backup created/restored · plugin installed/enabled/disabled · encryption enabled · diagnostic logging enabled/disabled.

**Rules:**

1. Append-only. `AuditRepository` exposes no update or delete method, and database triggers reject both.
2. Never contains message content or secret values.
3. **Exempt from retention deletion.** Deleting conversation data does not erase the record that a deletion occurred.
4. Included in exports so the user has their own copy of their security history.

---

# 20. Error Handling and Information Disclosure

1. User-facing messages are simple, actionable, and free of internal detail.
2. Diagnostic detail goes to logs with a correlation ID; the user is shown the ID, not the stack trace.
3. Errors never include secret values, file paths outside the app directory, or message content.
4. Stack traces are never displayed in the UI.
5. Failures deny rather than permit: an unavailable credential store prevents startup rather than degrading to plaintext.

Full taxonomy in `ERROR_HANDLING.md`.

---

# 21. Compliance Considerations

The user is a **data controller** with respect to their contacts' personal data; the application is the processing tool. This is not a legal opinion, and the project makes none — the design position is to give the user the technical means to meet obligations that may apply to them.

| Obligation | Mechanism |
|---|---|
| Lawful basis / minimisation | Bounded sync scope; local-first default; minimum-context transmission |
| Transparency about processors | Explicit provider disclosure before first transmission; provider list visible in settings |
| Right of access | Full and per-contact export in JSON and Markdown |
| Right to erasure | Per-contact purge, transactional and complete |
| Right to rectification | All memories are user-editable |
| Storage limitation | Configurable retention policies per data class and per chat |
| Security of processing | §6–§10 |
| Records of processing | Append-only audit log |

Where the "purely personal or household activity" exemption applies, most formal obligations do not. The project does not rely on that: the same mechanisms are provided regardless, because they are also simply good design. Details and the user-facing position are in `PRIVACY.md`.

---

# 22. Security Checklist

Before every release:

- [ ] No secrets committed (scanner clean)
- [ ] Dependencies reviewed; `pip-audit` clean
- [ ] All tests passing, including security tests
- [ ] Static analysis and type checking clean
- [ ] Architectural contracts passing
- [ ] Database migrations verified up and down
- [ ] Backup creation, verification and restore tested
- [ ] Redaction verified against a log sample
- [ ] Provider data boundaries verified by test
- [ ] Injection regression suite passing
- [ ] File permissions verified on a fresh install
- [ ] Documentation updated

---

# 23. Incident Response

1. Identify and contain.
2. Assess impact, including impact on the user's contacts.
3. Protect user data — revoke sessions and rotate secrets if credentials may be exposed.
4. Develop and test a fix.
5. Release with a clear advisory.
6. Document the incident.
7. Update this document if new practices are warranted.

Users should be told plainly what happened, what data was affected, and what they should do.

---

# 24. Security Testing Requirements

| Test | Assertion |
|---|---|
| Redaction | Secrets, codes and message content never appear in emitted logs |
| Secret storage | No secret value is written to the database or any file |
| SQL injection | Parameterized queries resist injection in every repository |
| Data boundary | An `external` provider is never called for a `local_only` chat |
| Fallback boundary | Provider fallback never crosses the data boundary |
| Session protection | Session files are created with owner-only permissions |
| Audit immutability | Update and delete against `audit_log` are rejected |
| Purge completeness | After a contact purge, no row anywhere references that contact |
| Backup contents | Backups contain no secrets, sessions or logs |
| Injection resistance | Corpus injection cases do not produce schema-valid harmful output |
| Plugin isolation | A raising plugin hook does not propagate |
| Startup denial | Unavailable credential store prevents startup |

---

# 25. Security Philosophy

Security is continuous. Every new feature is evaluated for privacy impact, data exposure, authentication implications, dependency risk, user consent and recovery strategy.

Two commitments underpin the rest:

**Prefer structural controls over policy controls.** A rule that a component *should not* send messages is a policy. A component that has no send capability cannot send. The second survives refactoring, new contributors and feature pressure; the first does not.

**State limitations honestly.** A threat model that claims to cover everything, or a permission system that implies enforcement it does not provide, is worse than none — it substitutes false confidence for real judgement.

No feature is complete until its security implications have been reviewed.
