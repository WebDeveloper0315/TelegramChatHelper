# CHANGELOG.md

All notable changes to this project will be documented in this file.

This project follows the principles of **Keep a Changelog** and **Semantic Versioning (SemVer)**.

Version numbers follow:

```
MAJOR.MINOR.PATCH
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

### Milestone 2.6 -- Gateway Reads

Slice 4 of `TELEGRAM_ARCHITECTURE.md`: reading chats and history out of
Telegram. Nothing is stored -- ingestion is the synchronisation engine's job,
and a test asserts that these commands create no local rows.

**The types**

- `TelegramChatInfo`, `TelegramMessage` and `HistoryPage` join `TelegramUser` in `domain/model/telegram.py`. They describe what Telegram *said*; the entities describe what this application decided to remember (ADR-041, ADR-044). A `TelegramChatInfo` that is not private cannot name a counterpart, which keeps every private-chat rule from being applied to a group.
- **`fetch_history` replaces the specified `iter_history`.** An `AsyncIterator` cannot express *where to continue from*: a caller draining one has no cursor to store, so a backfill interrupted part-way could not resume without re-reading. `HistoryPage` carries `messages` and `oldest_message_id`, and `reached_beginning` answers the single question a backfill asks.
- **`reached_beginning` is true only for an empty page.** A short page is not proof, because Telegram returns short pages for reasons of its own -- which is the whole reason the boundary is returned rather than derived.

**The adapter**

- `list_chats` is **three TDLib calls, not one**. `loadChats` populates the client's list and answers `404` once it is complete -- an ordinary end condition, absorbed rather than raised, because treating it as a failure would break listing for every account with few enough chats. `getChats` returns identifiers in Telegram's own order, which is not re-sorted: recomputing recency locally would need data this does not fetch. `getChat` resolves each from TDLib's local database.
- A chat that disappears between listing and resolving is skipped. One chat left or deleted mid-read must not cost the user the other two hundred.
- `fetch_history` passes `offset=0`, so consecutive pages never overlap and the cursor message is never returned twice.
- Message content maps conservatively: an unknown content type becomes `OTHER` rather than being refused, because losing it would leave a hole in a conversation the user can see in Telegram. Service messages are recognised by prefix, since TDLib has dozens and adds more.
- A photo's **caption** is read into `text`. A conversation held in captions would otherwise look empty.

**The commands**

- `tgassist telegram chats` and `tgassist telegram history <chat-id>`. A `telegram` group rather than the specified top-level `chats`, because `chat` already manages what this application has *stored*, and one word is what keeps the two from being confused at the moment a user types them. Both print "Nothing was stored."

**Tests -- 128 added, suite at 1997**

- The contract suite is now **95 tests over both implementations**: listing, ordering, unread counts, empty chats, limits, paging that reaches every message exactly once, non-overlapping pages, and the beginning being reported rather than inferred.
- `FakeTelegramGateway` gained `script_chats` and `script_history`, and its paging behaves as TDLib's does -- so a backfill loop written against the fake is one that works against Telegram (`TELEGRAM_ARCHITECTURE.md` §12.2).
- The scripted TDLib renders domain objects back into TDLib JSON and the adapter maps them forward, so a mapping bug cannot pass unnoticed by agreeing with itself.

## Fixed

- `tgassist telegram history` claimed "Beginning of the chat" for any page shorter than the limit. Telegram returns short pages for reasons of its own, so the claim was not knowable; it now reports the cursor and says older messages *may* continue.

## Architecture Decisions

- **ADR-051** gains an *As Implemented* section: slice 4 was the first test of growing a protocol one slice at a time, and it held. The single-consumer constraint held too -- reads go through `client.request()`, correlated by `@extra`, so they never touch the update stream.

## Scope note

Nine source and test files were created or modified, within the twenty-file limit.

### Milestone 2.5 -- Authentication

Slice 3 of `TELEGRAM_ARCHITECTURE.md`: signing in, signing out, and a session
that survives a restart. No reading, no updates, no sending.

**The boundary**

- `TelegramGateway` and `AuthorizationHandler` are now real ports. The gateway is **declared one slice at a time** (ADR-051): this slice declares lifecycle, both state axes, authorization and `get_me`, because a protocol listing methods no caller uses cannot be verified by a contract suite and a fake would have to invent behaviour for them.
- `connection_state()` is new relative to `API.md` v1.0. ADR-049 gave Session two independent axes, and a caller recording both cannot derive the second from `is_connected()`.
- **There is still no method for sending typing indicators**, and a contract test now asserts that neither implementation has one -- ADR-023 §2 made structural rather than documented.

**Driving an update-driven protocol (ADR-051)**

- TDLib's login returns nothing that says "you are logged in": state arrives as `updateAuthorizationState`. `TdlibGateway` runs one **dispatch loop** that owns the only consumer of `TdjsonClient.receive()`, and `start_authorization` reads the resulting view, asks the handler, submits, and waits for the view to move.
- **Submissions never run inside dispatch.** One that awaited its reply there would stall every other update behind it -- including the state change it was waiting for.
- **The waiter compares the raw TDLib state, not the domain one.** Two TDLib states collapse to `UNAUTHORIZED`, and a wait that could not tell them apart would return before anything happened.
- **The single-consumer constraint is asserted by test**, before slice 4 introduces a second candidate. The queue holds one item per update, so a second consumer would not duplicate the stream -- it would split it, and each would silently miss what the other took.
- `authorizationStateClosing` and `Closed` are deliberately **not** a logout. Closing is what every ordinary disconnect does, and recording it as a logout would tell the user they had been signed out every time they quit.
- Four real Telegram flows this application does not implement -- registration, QR confirmation and the two email flows -- each get a sentence saying what happened, because "TDLib changed" and "you need a flow we do not support" need different answers.

**Correctness**

- **A login that authenticated as a different Telegram user is refused, not recorded.** The account already owns that person's chats, contacts and messages, and there is no way to unmix two histories afterwards.
- Both session axes are written from what the gateway reports, so an authorized account still catching up is recorded as exactly that.
- `LoginResult.was_already_authorized` distinguishes a restored session from a fresh sign-in rather than implying one happened.
- Logout tells Telegram, records the transition, then destroys the store and the key -- in that order, so a failure part-way leaves nothing usable rather than something half-usable.

**Security**

- **Nothing retains a credential.** Each lives in a local for one request; `ConsoleAuthorizationHandler` has two slots -- an attempt counter and its limit -- so there is nowhere one could survive, and a test asserts that shape.
- The password is read with `getpass`, so it never reaches the screen, the scrollback or the shell history. The code is not: it is short-lived, useless once submitted, and a user who cannot see what they typed will mistype it.
- A rejection reports Telegram's reason and never the value: the mapped error carries `operation`, `telegram_code` and `telegram_message` and nothing else.
- The application hash is a **name** in the credential store (`telegram.api_hash_ref`), never a value in a file.

**Configuration**

- `telegram.api_id`, `telegram.api_hash_ref` and `telegram.device_model` move from *specified* to *implemented*. They identify the installation, not the user, and are obtained by hand from my.telegram.org -- documented in `DEVELOPMENT_WORKFLOW.md` §27, and reported as `TELEGRAM_NOT_CONFIGURED` rather than as a connection failure when absent.

**Tests -- 181 added, suite at 1869**

- A **47-test contract suite** runs every obligation against the hand-written fake *and* against `TdlibGateway` driven by a TDLib that runs the real login state machine -- clean login, wrong code, retry, abort, two-factor, restored session, logout. That is what makes the fake trustworthy everywhere else it is used.
- No test needs a Telegram account, a network or a real native library.
- Two test-isolation defects found and fixed: one test wrote `TELEGRAM_API_HASH` into the developer's **real** operating-system credential manager, and the CLI tests wrote a session key per run. Both now use an in-memory store; the leaked secret was removed.

## Architecture Decisions

- **ADR-051** *(Proposed)* -- Authorization Is Driven by a Dispatch Loop over a Single Update Stream. Records the three questions ADR-048 and ADR-049 left open, and why the port grows with its consumers.

## Changed

- `TdlibRequestFailedError` now carries TDLib's own message in its context. It is a constant such as `PHONE_CODE_INVALID`, never user data, and without it a caller can only report that something was refused.
- The ADR-048 ownership test matched `.receive(` textually and so also caught consumers of the client's own async queue -- a different call with none of the same danger. It now matches `_library.receive(`, and a second test asserts the new single-consumer rule.

## Scope note

Twenty-one source and test files were created or modified. That is **at the twenty-file guideline rather than inside it**, and it is worth saying why rather than rounding: this slice crosses four layers at once, because a port only the domain declares cannot be tested and an adapter with no use case above it cannot be run. The alternative was to split the port from its only implementation, which would have left a slice that proved nothing.

### Milestone 2.4 -- Session Storage

Slice 2 of `TELEGRAM_ARCHITECTURE.md`: where an account stands with Telegram, and
where its encrypted local store lives. No authentication flow, no gateway, no
synchronisation.

**The aggregate**

- `Session` carries **two independent state axes**, not one. TDLib reports authorization and connection separately and they vary independently, so a single enum cannot express *authorized but currently reconnecting* -- the ordinary condition after any network interruption. `DOMAIN_MODEL.md` §5.3 specified one column; it was corrected rather than worked around (ADR-049).
- `account_id` is the identity and the primary key. One session per account, so a surrogate key would be a second name for one row.
- `can_send` replaces "only the `ready` state permits sending", which could not say which sense of ready it meant.

**Two judgements the ADR left open**

- **`connected` begins at `updating`, not `ready`.** TDLib's socket is up from `updating` onwards; that state means *connected and catching up*. Dating a connection from `ready` would time it from the moment its backlog finished draining, which after a week offline is a long way from when it connected.
- **`can_send` is stricter than `is_connected`**, still requiring `ready`. A session replaying its backlog may not know the conversation it is about to reply to has moved on, and suggesting a reply into a stale view of a chat is the mistake this application exists to avoid.

**Storage**

- `telegram_sessions` and migration `0007`. Every entity invariant is restated as a `CHECK` constraint, so a row written by a repair script or a future migration cannot violate it either.
- Two tests assert that **every member of each enumeration is storable**. The enums and the constraints would otherwise drift apart silently: a state the entity accepts but the table refuses would fail only when a real user reached it.
- `SqlSessionRepository` and an independently written in-memory fake, both run against one 46-test contract suite. **No `delete`**: a session goes with its account by cascade, and logging out is a transition that leaves a record saying so.
- A downgrade drops the row, not the store on disk. Deleting a user's encrypted session directory is not a schema change's business.

**Security**

- `PrepareSession` generates the session key with `secrets.token_urlsafe` and writes it to the `SecretStore`; the row holds only the **name**. Generation is deliberately *not* behind an injectable port -- a seam there would let anything substitute a predictable generator for the one protecting every message the user has sent.
- A test asserts structurally that **neither the entity nor the table has a field a key would fit**, so the rule is not a convention anyone has to remember.
- Key first, row second, commit last. The other order would allow a row naming a key that was never stored, which looks like a working session until the first login fails. The reverse leftover costs nothing: the name is derived from the account, so the next attempt overwrites it.
- Preparation is idempotent. A second key would make the store the first key encrypted permanently unreadable.

**`security.require_secret_store` is enforced at last**

- Carried unenforced since Milestone 0, because until now there was no session to protect. `Container.start()` verifies the credential store **before** opening the database -- a refusal that had already migrated would have done work it promised not to do -- and all 17 CLI commands that touch user data now go through it.
- `doctor` deliberately does not, so the one tool that explains an unavailable credential store still runs.
- `PrepareSession` refuses **whatever the flag says**. The flag governs startup; there is nowhere else a session key may go, and an unencrypted fallback does not exist.

**Tests -- 157 added, suite at 1688**

- Aggregate, validation, derived state, transitions, mapper, migration, cascade and check constraints; the repository contract over both implementations; the use case against fakes; and the startup rule against a real container with and without a credential backend.

## Architecture Decisions

- **ADR-049** is now **Accepted and implemented**, with an *As Implemented* section recording the three judgements above.

## Scope note

Fifteen source and test files were created or modified, within the twenty-file limit.

### Milestone 2.3 -- TDLib Receive Bridge

Slice 1 of `TELEGRAM_ARCHITECTURE.md`: the runtime boundary between the verified
library and the application. No authentication, no session, no synchronisation --
the client moves JSON objects and knows nothing of Telegram.

**The bridge**

- `TdjsonClient` owns a `tgassist-td` thread running `td_receive`, and hands every frame to the event loop. `td_send` is called straight from the loop, because TDLib guarantees it is thread-safe and only receipt needs a thread.
- **The single-caller constraint is asserted by test**: `td_receive` is reachable from exactly one file and, within it, one method. Two threads calling it is undefined behaviour rather than an error, so a violation would be silent.
- Requests correlate by an `@extra` the client generates, replacing any the caller supplied -- correlation is the registry's to own.
- The update queue is bounded. When full, the receive thread **blocks** before its next `td_receive`, so TDLib buffers internally rather than this process growing without bound. `health()` reports depth and high-water mark, because a queue that filled once and drained looks identical to one that never filled.

**Three things ADR-048 did not specify, settled here**

- **End of stream is an event, not a sentinel on the queue.** The obvious design places a sentinel on the update queue at shutdown, and it cannot work: the queue is bounded, and a stalled consumer is exactly what leaves it *full*, so the sentinel would be blocked by the condition it exists to report. `receive()` returns `None`, driven by an event. Anything already queued is still drained first.
- **Backpressure is a polled `run_coroutine_threadsafe`.** The receive thread cannot `await` a full queue, so it waits on a concurrent future in short slices, rechecking the stop flag. Without the polling, a client stopped while its queue was full would hang until a consumer that is never coming drained it.
- **Restart is not supported.** A closed client's TDLib identifier is dead, and reusing the object would mean tracking which generation each pending future belonged to.

**Failure is never silent**

- A dying receive thread moves the client to `FAILED`, records why on `health()`, fails every pending request and releases every waiting receiver. `FAILED` survives `close()`: "never started" and "died" need different responses.
- `close()` is deterministic and idempotent, returning only once the thread has stopped and every waiter is released. A thread that ignores the stop request raises `TdlibShutdownTimeoutError` **after** the waiters are released -- a hung thread must not also hang the application.
- Malformed frames are counted, not raised. One frame that is not a JSON object must not cost every update queued behind it.

**Security**

- Only a frame's `@type` is ever logged, never its body. This is upstream of the redaction processor rather than relying on it: redaction is keyed on field names, and TDLib's field names are its own, so a frame logged wholesale could carry a key the processor has never heard of.

**Tests -- 50 added, suite at 1528**

- Lifecycle, sending, correlation, concurrent requests, queue saturation, backpressure, high-water, cancellation, malformed frames, thread death, shutdown, and the architectural single-caller check.
- The fake TDLib **blocks** in `receive` exactly as `td_receive` does, so the client's thread behaves in tests as it will in production.
- One integration test drives the **real** library: `getOption` round-trips through `td_send`, the receive thread and the correlation registry, and the client shuts down cleanly. It skips where no verified binary is recorded, so CI needs none.

## Architecture Decisions

- **ADR-048** is now **Accepted and implemented**, with an *As Implemented* section recording the three points above.

## Scope note

Seven source and test files were created or modified, within the twenty-file limit.

### Milestone 2.2 -- TDLib Foundation Verified Against Real Native Code

Completes slice 0. The previous entry built the verification machinery against
fakes; this one produced an actual `tdjson`, proved the machinery on it, and
recorded it.

**A binary was built from source**

- `scripts/build-tdjson.bat` is committed and *is* the procedure. It discovers Visual Studio with `vswhere`, clones vcpkg, builds OpenSSL and zlib **statically from source**, fetches TDLib at a pinned commit **by SHA** (TDLib's newest tag is years older than its releases, so a commit is the only precise pin), and builds `tdjson`.
- Recorded: TDLib commit `022d602…` (1.8.66), MSVC 19.51.36248, CMake 4.3.1 + Ninja, Release, vcpkg `x64-windows-static` OpenSSL 3.6.3 and zlib 1.3.2.

**Verification extended beyond the checksum**

- **Architecture**, read from the binary's headers *before* loading. A 32-bit library under a 64-bit interpreter otherwise fails with an `OSError` naming nothing; read first, it becomes "this is x86, we are amd64".
- **Runtime dependencies**, likewise read before loading. This closes a hole in ADR-047 as originally written: the manifest checksums *one file*, so anything that file loads at runtime is unverified code inside the trust boundary. OpenSSL and zlib are rejected; anything unrecognised is rejected, because an allow-list that admits the unknown is not one; the Visual C++ runtime is accepted but reported.
- PE and ELF headers are parsed directly rather than shelling out to `dumpbin` or `ldd` — those need a toolchain, differ per platform, and cannot be tested without one. Coverage is uneven and says so: PE fully, ELF architecture only, Mach-O not at all, each gap reported as *not checked* rather than passed.

**Verified end to end**

- `dumpbin /dependents` and this project's own PE parser **agree exactly**: 19 imports, 16 system, 3 redistributable, **zero OpenSSL, zero zlib**.
- `tgassist tdlib doctor` passes every stage against the real library, and TDLib independently reported **1.8.66** — matching the version recorded in the manifest, so the cross-check is validated rather than merely implemented.
- Flipping one byte fails the checksum and the library is **never loaded**: every later stage reports *not checked*.

## Fixed

- **The static C runtime was silently not applied.** `-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded` is governed by CMake policy `CMP0091`, which only applies to projects declaring `cmake_minimum_required(VERSION 3.15)` or later; TDLib declares 3.10, so the flag was ignored and the build stayed `/MD`. Found by reading the artefact's own imports rather than by trusting the flag. Diagnosed, reported by `doctor`, and documented with the one-line fix — deliberately not applied, because it would change the digest the committed manifest describes.
- **A patch script silently did nothing.** An edit to the build script used `str.replace` without asserting it matched, leaving the dependency install on the dynamic triplet while CMake was configured for the static one. Caught by a monitor whose pattern included the package name, ~30 seconds before OpenSSL would have built the wrong thing.
- **Three errors in the prose build recipe**, all found by running it: it omitted the vcpkg toolchain file, assumed a Visual Studio CMake generator the bundled CMake does not offer, and did not name `GPERF_EXECUTABLE` (vcpkg installs host tools where TDLib's `find_program` does not look).

## Changed

- `SECURITY.md` gains §6a, *Native Library Trust*: the seven ordered checks, why the search never falls back, why dependencies are checked at all, and the artefact verification procedure.
- `DEVELOPMENT_WORKFLOW.md` §26 replaced with the recipe that was actually run, plus the three corrections and the static-CRT limitation.
- `CONFIGURATION.md` §6.3 documents the full check order.
- ADR-047's *As Implemented* section records all four added checks and the build metadata.

### Milestone 2.1 -- TDLib Foundation

Slice 0 of `TELEGRAM_ARCHITECTURE.md`: locating, verifying and loading the
native Telegram library. No authentication, no session, no sync -- the objective
was to retire the largest remaining technical risk before any Telegram
functionality is written.

**Verification**

- `TdjsonLoader` performs four steps in order -- resolve, verify, load, probe -- and each failure has a distinct remedy rather than one generic "TDLib failed".
- **A pinned checksum manifest**, shipped **empty**. Nothing is trusted until a human records a digest, having established where the file came from. `tgassist tdlib verify` prints the exact entry to add.
- **Verification precedes loading**, so an untrusted binary is never mapped into the process. Asserted by test.
- **The search never falls back.** A candidate that exists but fails verification is a refusal; only an *absent* candidate advances the search. Falling through would mean planting a library in a high-precedence directory earns a silent retry rather than a refusal.
- **No escape hatch.** No configuration setting loads an unverified library. Recording an entry is one command, and an opt-out would become the documented path within a week.
- An unverified library raises a `SecurityError`, not a configuration error: `tdjson` sees the session key, every message and the network.

**Capability checks**

- Required entry points: `td_create_client_id`, `td_send`, `td_receive`, `td_execute`. A build offering only the deprecated `td_json_client_*` interface is a real TDLib and the wrong one.
- Version comes from `td_execute`, which is synchronous and needs no client, thread or network -- so ADR-048's receive loop stays in the next slice rather than being smuggled into this one.
- **Manifest cross-check:** when an entry records a version, it is compared with what the library reports. A disagreement means a stale entry or a swapped file, and is refused.
- TDLib's own logging is silenced immediately after loading. It defaults to verbosity 5 on standard error, which would put library chatter into command output -- the concern behind ADR-040 arriving by a different route.

**CLI**

- `tdlib doctor` runs the whole sequence and reports which stage failed. A stage never reached reports `not checked` rather than a failure: not checked and failed are different things, and conflating them sends people to fix the wrong stage.
- `tdlib version` reports what the library said, not what configuration claims.
- `tdlib verify` prints the digest and, when unrecognised, a pasteable manifest entry -- after asking for provenance.

**Tests**

- 91 tests, deterministic, with no binary, no compiler and no network. Fake libraries model each real failure: not a library, an old TDLib, one that loads but answers nothing, one that raises, a platform that refuses to open a file it can see.
- Windows and Linux search paths are both exercised from whichever machine runs the suite, by injecting the platform rather than detecting it.

## Fixed

- **A seam that was not a seam.** `TdjsonLoader`'s opener defaulted to `open_with_ctypes` as a *default argument value*, bound at import time -- so replacing the module attribute did not reach it, and the injection point was decorative. It now resolves at construction. Found by a CLI test that could not make the success path work.
- **`CONFIGURATION.md` §6.3 disagreed with ADR-047** on the key's name (`telegram.tdlib_library_path` against `telegram.tdjson_path`) and still offered `telegram.adapter`, which ADR-047's amendment to ADR-012 §4 withdrew. Reconciled, with implemented and specified-but-unimplemented keys now marked separately.

## Changed

- `DEVELOPMENT_WORKFLOW.md` §26: obtaining, installing, recording and troubleshooting `tdjson`, with supported platforms and stated operational limitations.
- `CONFIGURATION.md`: the `telegram` section reconciled with what exists.

## Architecture Decisions

- **ADR-047 -- TDLib Binary Acquisition, Verification and Distribution** is now **Accepted and implemented**, with an *As Implemented* section recording three refinements: the configured path and its environment variable are one candidate rather than two (the configuration system already layers them); verification stops the search rather than falling through; and version detection uses `td_execute` rather than a client.

## Scope note

Thirteen source and test files were created or modified, within the twenty-file limit.

### Milestone 2.0 -- Telegram Architecture Review

Design only. No production code, by instruction: the objective was to minimise
future rework by deciding the Telegram layer before building it.

**Produced**

- `docs/TELEGRAM_ARCHITECTURE.md` -- component and sequence diagrams, the revised gateway port, authentication and sync flows, the session state machine, threading and async model, error taxonomy, testing strategy, a risk register, and a nine-slice implementation order.

**Seven inconsistencies found in existing documentation**, listed rather than worked around

1. **ADR-012 §3 is overdue and blocking.** Binary acquisition was required in Milestone 0 and has been carried unresolved through eight milestones. Nothing Telegram-related can start without it.
2. **The Session state machine conflates two independent axes.** `DOMAIN_MODEL.md` §5.3 gives one enum, but TDLib reports authorization and connection state separately and they vary independently -- so *authorized but reconnecting*, the ordinary state after any network blip, is unrepresentable.
3. **`ROADMAP.md` M2 requires three tables `DATABASE.md` schedules for M3** (`sync_cursors`, `conversations`, `attachments`).
4. **`API.md` §10 does not match the concurrency model**: a per-message history iterator the batch-writing engine must re-group, an unbound gateway under multi-account, and media methods with no consumer or `FileStore` behind them.
5. **`security.require_secret_store` is still unenforced.** Harmless until now; a session key makes it real.
6. **The Contact operator-identity invariant becomes enforceable** once `getMe` supplies the operator's own identifier.
7. **`ARCHITECTURE.md`'s layer diagram places Telegram below Storage**, inviting the reading that the gateway depends on the database -- which `API.md` §10 forbids.

## Architecture Decisions

- **ADR-047 -- TDLib Binary Acquisition, Verification and Distribution** (Proposed). Resolves ADR-012 §3 at last: nothing loads unverified native code into a process holding the user's session. Checksum-pinned manifest, explicit resolution order, no automatic download, staged distribution. **Also amends ADR-012 §4**, withdrawing the planned Telethon adapter -- the fake gateway already validates the port, and Telethon is retained as a replacement path rather than a second implementation to maintain.
- **ADR-048 -- The TDLib Update Loop Runs on a Dedicated Thread, Bridged to asyncio** (Proposed). `td_receive` is blocking and thread-affine; this applies the pattern `DatabaseExecutor` already established, with a bounded queue so backpressure is visible rather than absorbed.
- **ADR-049 -- Session Models Authorization and Connection as Separate Axes** (Proposed). Corrects §5.3; `can_send` states the send rule once instead of leaving "ready" ambiguous.
- **ADR-050 -- Synchronisation Cursors, Batch Boundaries and Batched Event Publication** (Proposed). Reconciles bulk ingest with ADR-031's synchronous delivery and ADR-034's single connection: the cursor advances in the same transaction as its messages, and one event is published per committed batch rather than per message.

### Milestone 1.5 -- Message Ingestion

The pipeline every future source feeds: the CLI today, Telegram synchronisation
in Milestone 3, import tools and tests thereafter. The communication graph had
structure but nothing moving through it.

**The blocking decision, and the one that turned out not to block**

The goal asked whether retention policy had to be settled first. It does not:
retention needs an age to measure (`sent_at`), an index to find old rows by, and
a per-chat override -- the first two exist because the history query needs them,
and the third is one additive column. The policy changes a background job, not a
schema.

What *did* block the slice was identity. The documented invariant made
`(account_id, chat_id, telegram_message_id)` unique, which requires every message
to carry a Telegram identifier -- and only one of the four named sources issues
one. Resolved as ADR-045.

**Domain**

- `Message` -- frozen, and the first aggregate with **no transitions at all**. There is nothing a message becomes.
- **No `updated_at`, no update path, no delete path** (ADR-046). "Immutable factual record" is a property of the code: `MessageRepository` has no such methods, and a test asserts their absence on the port and both implementations.
- `telegram_message_id` is **optional**; its unique index is **partial**. A non-partial index would reject the second message from every source except Telegram (ADR-045).
- `sent_at` and `ingested_at` are both required and neither must precede the other -- clock skew is ordinary, and rejecting it would lose a real message over a fraction of a second.
- `is_outgoing` is **derived**, not stored. It is exactly `sender_kind == operator`; version 1.0 stored it "because it is queried constantly", which is an argument for an index rather than for a second copy that can disagree.

**Persistence**

- Migration `0006`: `messages`, plus the `chats (account_id, id)` index its composite foreign key needs. Deleting a contact now reaches messages through two cascades, which is what `PRIVACY.md` §7 promises.
- Index `(account_id, chat_id, sent_at, id)` — history ordered by when a message was **sent**, not ingested, because a backfill inserts old messages after new ones.

**Application**

- `IngestMessages` takes a **batch** and one transaction. `IncomingMessage` is deliberately not a `Message`: a source knows what arrived, not what identifier it will get or when we stored it.
- **No `MessageSource` port.** There is one source; a protocol with one implementation is an interface designed against a guess. Synchronisation will build `IncomingMessage` values exactly as the CLI does.
- The batch is validated **whole before anything is written**, so a batch containing one malformed message is refused entirely — a guarantee that holds regardless of whether the store rolls back.

**CLI**

- `message ingest`, `history`, `show`. Ingesting twice with `--telegram-id` stores one message; ingesting twice without one stores two.

**Tests**

- 1345 passing, from 1163. The shared contract suite over both implementations, plus suites for append-only-ness, idempotency, ownership, ordering and cascade.

## Fixed

- **Conversation content was not redacted from logs.** `Message.text` is the most sensitive field in the application, and `is_content_key` matched `message_text` but not a bare `text`. It could not simply be added to the fragment set: `context` — a structural key on every application error — contains `text`, so redacting by fragment would have hidden the diagnostic information errors exist to carry. Whole-key matching was added alongside fragment matching.
- **`--sent-at` rejected real ISO 8601 timestamps.** Typer's `datetime` type accepts three fixed formats, none carrying a timezone offset, while the help text claimed ISO 8601. The option now parses with `fromisoformat` and reads a naive value as UTC rather than guessing a local zone.

## Changed

- `DOMAIN_MODEL.md` §5.6 corrected: optional external identity, the append-only property, `is_outgoing` as derived, and each deferred field with its reason.
- `DATABASE.md`: `messages` documented as implemented, including why three planned indexes are absent; migration plan renumbered.
- `API.md`: the implemented `MessageRepository` and the ingestion contract, and why `add_batch`, `update`, `list_by_conversation` and `list_for_metrics` are absent.
- `SECURITY.md` §9: the whole-key content rule.

## Architecture Decisions

- **ADR-045 -- Message Identity Is Local; the External Identifier Is Optional and Its Index Partial** (Proposed). What makes the pipeline source-agnostic without giving up idempotency for the source that needs it.
- **ADR-046 -- Messages Are Append-Only, and Nothing Deletes Them Yet** (Proposed). Why the update and delete paths are absent, and why the soft-versus-hard deletion question is deliberately left to the milestone that deletes.

## Scope note

Sixteen source and test files were created or modified, within the twenty-file limit.

### Milestone 1.4 -- Chat: the Communication Graph

The edge joining an Account to a Contact. Account and Contact were nodes with
nothing between them; every system planned on top of this one -- synchronisation,
ingestion, memory, goals -- attaches to a Chat or reaches a Contact through one.

**Scope, and what was deliberately left out**

`Conversation` and `Message` were the other candidates for "the communication
graph". Neither is the edge: a Conversation is a *segment* of a chat bounded by
message timestamps, so with no messages it would have no defined start; a Message
is content rather than structure. `SyncCursor` is synchronisation state and
belongs with the code that advances it. See ADR-044.

**Domain**

- `Chat` -- frozen, self-validating, with `with_sync_enabled`, `with_ai_processing_mode` and `retitled`, each returning `self` on a no-op.
- **Two constructors**: `private_with` requires a contact and refuses a title; `group_titled` requires a title and refuses a contact. The impossible combinations are unwritable rather than merely rejected.
- The invariant is stated in **both directions** -- `(chat_type = 'private') = (contact_id IS NOT NULL)` and the same for the title -- so neither a private chat with nobody in it nor a group chat claiming a single counterpart can exist.
- `AiProcessingMode` (ADR-024) with `allows_ai` and `allows_cloud_ai` as separate questions, because "stop using AI on our chats" and "don't send our messages to a cloud service" are different requests in `PRIVACY.md` §7.
- `TelegramChatId`, distinct from `TelegramUserId`, and **allowed to be negative**: Telegram numbers groups and channels below zero. A `> 0` check -- correct for a user identifier, and the obvious thing to copy -- would have rejected every group chat.

**Persistence**

- Migration `0005`: `chats`, plus the `contacts (account_id, id)` index its foreign key requires.
- **The foreign key to `contacts` is composite, on `(account_id, contact_id)`** (ADR-043). A simple `contact_id -> contacts.id` guarantees the contact exists but *not* that it belongs to the same account, so a chat in one account could name another account's contact and nothing would object. Requiring the pair to exist together makes that unrepresentable. This is the pattern every later table in the graph should use.
- Partial unique index giving a contact at most one private chat, and a unique `(account_id, telegram_chat_id)` -- two accounts may record the same Telegram chat, one account may not record it twice.

**Application**

- `OpenPrivateChat`, `OpenGroupChat`, `GetChat`, `ListChats`, `SetChatPolicy`.
- `OpenPrivateChat` is the **first use case to compose two scoped repositories in one transaction**. The contact ownership check costs nothing: the contact repository is scoped to the same account, so another account's contact simply is not found.

**CLI**

- `chat open`, `show` (by identifier or `--contact`), `list`, `set --sync/--no-sync --ai`.

**Tests**

- The shared contract suite over both implementations, plus a graph suite: the edge reaches exactly one contact, that contact belongs to the same account, cross-account linkage is refused by the *store*, and the graph does not outlive its nodes.
- The composite key is tested against the real schema and against a fake that models it independently -- a fake checking only existence would have accepted exactly the row the constraint was added to prevent.

## Fixed

- **`DATABASE.md` specified two things that cannot both hold.** `chats.contact_id ON DELETE SET NULL` alongside a check requiring a private chat to name its contact: purging a contact would null the column and violate the check on the line below. `SET NULL` also contradicts `PRIVACY.md` §7, which requires a contact purge to be "transactional removal across every table". Resolved as `ON DELETE CASCADE` (ADR-043).

## Changed

- `DOMAIN_MODEL.md` §5.5 corrected: the edge's role, both directions of the private-chat invariant, the composite ownership key, negative Telegram identifiers, and each deferred field with its reason.
- `DATABASE.md`: `chats` documented as implemented, with a note that later tables should reference `(account_id, chat_id)` compositely; migration plan renumbered.
- `API.md`: the implemented `ChatRepository`, and why `list_by_activity`, `list_sync_enabled`, `set_ai_processing_mode` and `purge` are absent.

## Architecture Decisions

- **ADR-043 -- Cross-Table Account Ownership Is Enforced by Composite Foreign Keys** (Proposed). The most consequential decision in this slice: it establishes the shape every later table in the graph uses, and closes a hole that would otherwise have been inherited by messages, memories, goals and profiles.
- **ADR-044 -- The Communication Graph Is Established by Chat Alone** (Proposed). Why Chat is the edge, why Conversation and Message are not, and why a Chat has no lifecycle of its own.

## Scope note

Seventeen source and test files were created or modified, within the twenty-file limit.

### Milestone 1.3 -- Contact Aggregate

The first aggregate describing somebody other than the operator. Its key appears
in six tables that do not exist yet -- memories, goals, chats, relationship and
style profiles, suggestions -- which made this the last cheap moment to choose
that key deliberately.

**Domain**

- `Contact` -- frozen, self-validating, with `archived`, `deleted`, `restored`, `renamed` and `with_username`. Each returns `self` when the change is a no-op.
- **Identity is a locally generated `ContactId`**, not the Telegram identifier (ADR-041). The same person can be known to two accounts, so `telegram_user_id` is not unique in the table; a natural key would have to be the pair, and every child table would carry both columns in its foreign key.
- **Lifecycle: `active ⇄ archived`, either state to `deleted`, and one `restored` transition back** (ADR-042). Two nullable timestamps, at most one ever set, rather than booleans -- retention has to ask "deleted before when", and a boolean cannot answer that.
- `validate_username` -- structural Telegram handle validation with normalisation: a leading `@` is stripped, 5-32 characters, letter first, no trailing underscore.

**Persistence**

- Migration `0004`: `contacts`, the first table with many rows per account, and so the first whose indexes serve queries rather than only constraints.
- **The unique index on `(account_id, telegram_user_id)` covers soft-deleted rows.** A deleted contact still holds that person's history; a second row for them would split it. Re-adding is therefore refused, and the caller is told to restore instead -- which is why `get_by_telegram_id` takes `include_deleted` and creation passes it.
- Index `(account_id, created_at, id)`, matching the listing query and its keyset tiebreaker. `account_id` leads because every query this table serves is scoped.
- Seven check constraints, including `archived_at IS NULL OR deleted_at IS NULL`, each with a test proving it rejects a bad row.

**Application**

- `CreateContact`, `GetContact`, `ListContacts`, `ChangeContactStatus`. The last is one use case parameterised by `ContactTransition` rather than three near-identical classes, because archive, restore and delete are the same transaction with a different entity method.
- `resolve_account` extracted to `use_cases/account_scope.py`: every account-scoped use case begins by resolving the active account, and the rule was already duplicated between the profile use cases.

**CLI**

- `contact add`, `show`, `list`, `archive`, `restore`, `delete`, each accepting `--account`.

**Tests**

- Contact runs the shared Milestone 1.0 contract suite for the first time as an account-scoped collection -- including its **soft-deletion branch, which no aggregate had ever executed**. An untested contract clause is a clause that is probably wrong.
- A second suite covers what only matters when rows belong to somebody: ownership, foreign-key integrity, cascade deletion, scope isolation, uniqueness within and across accounts, and the archive lifecycle.
- `tests/fakes/pagination.py` extracts the keyset paging the fakes need. It is the code most likely to be subtly wrong in a way the contract suite would then pass for the wrong reason, so it is written once and verified by every aggregate's contract run. The account fake now uses it, and its contract suite still passes.

## Fixed

- **Archiving a deleted contact reported "not found".** `ChangeContactStatus` excluded deleted contacts from its lookup for every transition except restore, so the entity's own rule -- that a deleted contact must be restored before it can be archived -- was never reached, and the caller got a message that was untrue. The lookup now includes deleted contacts for every transition and lets the entity judge legality. Found by a test, not by review.

## Changed

- `DOMAIN_MODEL.md` §5.4 corrected: identity, the three-state lifecycle, the reduced attribute set, and each deferred field with its reason.
- `DATABASE.md`: `contacts` documented as implemented; the migration plan renumbered into milestone order, with a note that numbers beyond `0004` are a plan rather than a commitment.
- `API.md`: the implemented `ContactRepository`, and why `get_by_username`, `search`, `soft_delete` and `purge` are absent.

## Architecture Decisions

- **ADR-041 -- Contact Identity Is a Local Surrogate Key** (Proposed). Weighs the natural, composite, surrogate and dual arrangements, and records why the unique index deliberately covers soft-deleted rows.
- **ADR-042 -- Contact Lifecycle** (Proposed). Three states with immediate value; `discovered` and `dormant` deferred as unrepresentable today; `is_blocked` deferred until something processes contacts.

## Scope note

Nineteen source and test files were created or modified, within the twenty-file limit.

## Fixed

### Maintenance -- CLI logging configuration (ADR-040, now Accepted)

**The CLI never configured logging, so every log record was printed to standard
output, unfiltered and unredacted.** `_open` called `Container.create` with
`configure_logging_on_start=False` to keep command output clean. Suppressing the
call did not silence logging: structlog falls back to its default `PrintLogger`,
which writes to standard output at every level. The comment described the
opposite of what the code did.

Two consequences, one cosmetic and one not:

- Records appeared in the middle of command output, so no command's output was
  deterministic.
- The whole `logging` configuration section was ignored on this path --
  including `mask_secret_values`. Records emitted by a CLI command had never
  passed through redaction. Nothing secret was being logged yet, which is why
  this is a fix rather than an incident, but Milestone 2 introduces records that
  carry Telegram identifiers and message metadata.

**The fix is one line**: `_open` no longer suppresses the call. The CLI now uses
the same logging configuration as every other entry point, with no CLI-specific
default and no second initialisation path -- `configure_logging` remains the
only place logging is set up, and it already resets handlers, so repeated
invocation in one process cannot accumulate them.

The console handler writes to **standard error**, so configured output never
reaches standard output. This is why the proposal's suggestion of a CLI-specific
"console off by default" was dropped: it would have bought nothing, while giving
`console_enabled` a second answer and ignoring the user's setting -- the same
class of defect the ADR exists to remove.

**Configuration**

- `config/default.yaml` now ships `component_levels` for `asyncio`, `alembic`
  and `sqlalchemy.engine` at `WARNING`. Routing every record through one chain
  is deliberate, since redaction must cover more than our own call sites, but at
  `DEBUG` it also meant the event loop announcing its selector. The
  application's own records are untouched, and a developer who wants the detail
  raises the level.

**Tests**

- Twelve tests in `TestCliLoggingStartup`: structlog is configured by a command;
  records do not reach standard output; standard output is byte-identical across
  runs; records do reach standard error; the configured level is honoured; debug
  appears only when configured; `console_enabled: false` silences it; redaction
  is installed on this path; the file sink receives command records; repeated
  invocation duplicates neither handlers nor records; third-party libraries stay
  quiet.
- `restore_logging` now also calls `structlog.reset_defaults()`. structlog's
  configuration is process-wide, so a test invoking a command would otherwise
  leave every later test running against whatever that command installed. This
  immediately exposed an order-dependent test that had been asserting on
  configuration leaked from an earlier one; it now configures logging itself.
- The Milestone 1.2 test that compared only the profile fields a command printed
  compares whole output again, the workaround for this defect being gone.

## Added

### Milestone 1.2 -- UserProfile Vertical Slice

The first account-owned aggregate. Everything after this one is account-scoped
too, so the point of the slice was to settle how ownership, cascade and scoping
work while the aggregate is small enough to get them right.

**Domain**

- `UserProfile` -- frozen, self-validating, with `with_language`, `with_tone`, `with_message_length`, `with_emoji_usage` and `with_quiet_hours`. Each returns `self` when the value is unchanged, so a redundant edit does not move `updated_at`.
- **Identity is the account.** `account_id` is the primary key; there is no surrogate key, because an account has exactly one profile and a second name for one row is how a query eventually reads by one and writes by the other (ADR-038).
- `TimeRange` -- quiet hours as minutes past midnight, so a period crossing midnight compares correctly. A pair of naive times makes `22:00-08:00` look empty. Equal bounds are refused as ambiguous between an empty range and the whole day.
- `TonePreference`, `MessageLength`, `EmojiUsage` as `StrEnum`s, stored as their values rather than ordinals.
- `validate_language` -- structural BCP-47 validation with normalisation (`EN-gb` becomes `en-GB`). Structural rather than registry-based: an unregistered tag simply matches nothing, whereas a malformed one breaks parsing everywhere it is used.

**Persistence**

- Migration `0003`: `user_profiles`, the first table with a foreign key. `account_id` is simultaneously primary key and foreign key with `ON DELETE CASCADE`, so one profile per account and deletion of orphans are both structural. No extra index on `account_id`: the primary key already is one.
- Seven check constraints restating the entity's invariants, each with a test proving it rejects a bad row rather than merely that the table exists.
- Every column `NOT NULL` with a server default -- no nullable column standing in for "not decided yet".
- `UserProfileMapper` with round-trip and column-coverage tests.
- A test asserts `PRAGMA foreign_keys` is actually on, because SQLite ignores foreign keys without it and the cascade would otherwise be decorative.

**Application**

- `GetUserProfile` creates a default profile on first access, so adding an account does not require deciding preferences before the application is usable.
- `UpdateUserProfile` with `ProfileChanges`, where `None` means "leave alone" -- a partial update without inventing a null-means-unset convention in the entity.

**CLI**

- `profile show` and `profile set --language --tone --length --emoji --quiet-hours --account`.

**Tests**

- A contract suite running both implementations against ownership, foreign-key integrity, cascade deletion and scope isolation. The in-memory fake deliberately shares one store across scoped instances, so isolation is genuinely tested rather than passing because the fake had nothing to leak.
- One test inspects the signatures and asserts no repository method accepts an account, so a future method reintroducing the parameter fails a test rather than a review.

## Changed

- `ScopedRepositoryFactory` added to `domain/ports/repository.py`: account-owned repositories are scoped at construction and **no method takes an account identifier** (ADR-039). The conventional `get(account_id)` form places correctness at every call site forever, and its failure mode is silently returning another account's data.
- `DOMAIN_MODEL.md` §5.2 corrected: identity, the reduced attribute set, and each deferred field with its reason.
- `DATABASE.md`: `user_profiles` documented as implemented; the migration plan renumbered to one aggregate per migration.
- `API.md`: new §7.2 on account-scoped construction, and the `UserProfileRepository` interface with the reasoning for having no `delete` and no `list`.

## Fixed

- Nothing in this milestone's code. One pre-existing defect was found here and fixed in the maintenance entry above -- see ADR-040.

## Architecture Decisions

- **ADR-038 -- UserProfile Identity Is the Account** (Proposed). Drops the surrogate key, and defers `display_name`, `timezone`, `available_hours`, `auto_approve_memory_categories` and `confidence_thresholds` -- the first two because Account already owns them, the rest because the vocabulary each draws on does not exist yet and a column that accepts anything is worse than an absent one.
- **ADR-039 -- Account Scope Is a Constructor Parameter** (Proposed). Establishes scoped repositories for every account-owned aggregate to come.
- **ADR-040 -- The CLI Does Not Configure Logging** (**Accepted**; found here, fixed in the maintenance entry above). Found by comparing the output of two identical `profile show` runs. `_open` suppressed `configure_logging` to keep command output clean; the effect was the opposite, because unconfigured structlog defaults to a `PrintLogger` on standard output with no level filtering. The whole `logging` configuration section -- including secret redaction -- was therefore ignored for every CLI command. Left unapplied in this milestone as it changed behaviour outside its scope.

## Scope note

Eighteen source and test files were created or modified, within the twenty-file limit.

### Milestone 1.1 -- Account Aggregate

The first real business aggregate, exercising the whole architecture end to end:
domain, repository, mapper, migration, unit of work, dependency injection, CLI,
tests and documentation -- with no Telegram or AI involved.

**Domain**

- `Account` -- frozen, self-validating, with `activated`, `deactivated` and `renamed` returning new instances. Each returns `self` when the change is a no-op, so a redundant call does not move `updated_at` and make nothing look like something.
- `AccountId` and `TelegramUserId` as `NewType` aliases: statically non-interchangeable under `mypy --strict`, with no wrapping noise at call sites.
- `validate_timezone` -- IANA identifiers only, resolved through `zoneinfo`. A fixed offset is refused because it cannot express daylight saving, and reply-timing advice is computed against local hours.
- `DomainValidationError`, inheriting both `DomainError` and `ValueError`, so idiomatic `except ValueError` still works while the error carries a code and a user-facing message.
- `ConflictError` for a conflict detected before writing, so the message names the conflict rather than a column.
- `AccountCreated` and `AccountActivated` events.

**Persistence**

- Migration `0002`: the `accounts` table with four check constraints and two unique indexes.
- A **partial unique index** on `is_active` makes the single-active invariant structural: a second activation fails at the database rather than depending on every caller remembering to deactivate first. Declared for both SQLite and PostgreSQL.
- `AccountMapper` with a round-trip test and a column-coverage test that fails the moment a migration adds a column the mapper does not write.
- `SqlAccountRepository` -- six operations, each traceable to a caller that exists.

**Application**

- `CreateAccount`, `GetAccount`, `ListAccounts`, `SetActiveAccount`. Separate classes rather than one service, because a class states its dependencies in its constructor and a shared service would demand everything for everyone.
- The first account created becomes active automatically, so a fresh installation is not left in a state where nothing works for no visible reason.

**CLI**

- `account create`, `account show`, `account list`, `account activate`.

**Tests**

- 579 passing. Both repository implementations run the Milestone 1.0 contract suite plus account-specific obligations, and a further suite asserts they agree with each other.
- Migration tests assert the check constraints and the partial index actually reject bad rows, rather than merely that the table exists.

**Dependencies**

- Added `tzdata`, required for IANA timezone resolution on Windows, which has no system timezone database.

## Fixed

- **Domain validation escaped as an unhandled traceback.** Invariant failures raised bare `ValueError`, which is not in the taxonomy the CLI catches, so an invalid timezone showed a stack trace instead of a message. Found by exercising the CLI rather than by a test, which is the argument for the CLI existing.

## Changed

- `DOMAIN_MODEL.md` §5.1: Account's lifecycle corrected, invariants stated in full, deferred fields named with reasons, and the identifier implementation note added.
- `DATABASE.md`: the `accounts` table documented as implemented; business migrations renumbered from `0002` to `0003` onward.
- `API.md` §9: the implemented `AccountRepository` interface, and why it has no `delete`.

## Architecture Decisions

- **ADR-037 -- Account Lifecycle Separated from Session Lifecycle** (Proposed). The documented lifecycle named six states while providing one boolean, and three of the six duplicated Session's. Two entities would have owned "is this account authenticated" and would eventually have disagreed. Account now owns only whether the user selected it.

## Scope note

Sixteen source files were modified or created, within the twenty-file limit.

### Milestone 1.0 -- Repository Contracts

**Domain**

- `SortDirection`, `SortOrder`, `PageRequest`, `TimeWindow` -- query intent expressed as domain value objects. `SortOrder.field` is a domain field name, never a column, so a schema rename cannot reach the application layer.
- `RepositoryFactory` -- a callable taking a unit of work. Use cases declare the repositories they need as constructor parameters, which keeps their real dependencies visible in their signature.
- `domain/ports/repository.py` -- the repository contract, stated as obligations rather than as a base class, with the reasoning for that choice.

**Infrastructure**

- `KeysetPaginator` -- ordering, cursor position and lookahead in one place. **Requires a unique tiebreaker column**, because ordering by a non-unique column alone silently skips rows.
- `Cursor` extracted to its own module, with lossless datetime encoding.
- `EntityMapper` -- the mapping framework, with the four properties a mapper must satisfy stated as a contract and verified by test.
- `Repository.fetch_page` rewritten to take a `PageRequest` and a paginator, so the tiebreaker requirement is the framework's responsibility rather than each repository's to remember.

**Testing**

- `tests/support/repository_contract.py` -- a reusable contract suite every future repository inherits. Covers identity, round-trip equality, snapshot semantics, pagination completeness and stability, malformed cursors, sort inversion, transaction participation, and optional soft-delete and count capabilities.
- `tests/support/sample_aggregate.py` -- a toy aggregate that exists only to exercise the framework, with a deliberately non-unique sort column so the tiebreaker is tested where it actually matters. Test scaffolding: its table is created by a fixture and never reaches a user's database.
- Two independent implementations -- SQLAlchemy and in-memory -- run the identical suite, plus a further suite asserting they agree with each other.
- 443 tests passing.

**Composition root**

- `Container.repository(factory, uow)` for wiring. No business registrations.

## Fixed

- **Cursors silently skipped rows.** Encoding stringified a `datetime` through a generic `str()` fallback; decoding produced text, and binding text against a `DateTime` column compared the wrong things -- returning wrong rows without any error. Datetimes now encode as ISO-8601 and decode back to the column's own Python type. Found by the contract suite on its first run against both implementations, which is precisely the failure that suite exists to catch.

## Changed

- `API.md` §7: recorded that there is no generic repository interface and why; documented `SortOrder`, `PageRequest`, the mandatory pagination tiebreaker, and repository construction via factories.
- `DATABASE.md` §14: added the mapping contract, identity and loading policy, and the absence of optimistic locking.

## Architecture Decisions

- **ADR-035 -- No Generic Repository Base** (Proposed). A generic CRUD interface is wrong for four of the five aggregates examined and would break the guarantee that an audit trail cannot be rewritten. `ReadRepository`/`WriteRepository`, `Specification` and a factory registry are omitted for stated reasons rather than by oversight.
- **ADR-036 -- No Optimistic Locking** (Proposed). The database-level lost update is structurally impossible under serialized transactions. The remaining think-time race is narrow and better served by memory revisions, which merge rather than reject. Records the three conditions that would require revisiting.

### Milestone 0.2 -- Persistence Foundation

**Ports (domain layer)**

- `Database` -- connection lifecycle and health, with `HealthReport` and `PragmaState`.
- `UnitOfWork` -- transaction boundary, savepoints, and event release gated on commit.
- `UnitOfWorkFactory` -- use cases open their own transaction rather than receiving one.
- `MigrationRunner` -- schema status, upgrade, downgrade, and a pre-upgrade hook.
- `Page` -- keyset pagination. Every collection query uses a cursor, never an offset, because `OFFSET 50000` makes the database walk and discard fifty thousand rows to return twenty.

**Implementations (infrastructure)**

- `DatabaseExecutor` -- the single dedicated worker thread all database work runs on (ADR-013).
- `SqliteDatabase` -- SQLAlchemy Core engine, pragma application and read-back verification, health checks reporting integrity, foreign keys, page counts and schema revision.
- `SqlAlchemyUnitOfWork` -- transactions, savepoints, error normalisation, and events withheld until commit.
- `Repository` base -- transaction-aware execution, keyset pagination, and constraint-violation messages phrased in the domain rather than the schema. No business repositories: those arrive with Milestone 1.
- `Cursor` -- opaque pagination tokens; a malformed one is treated as absent, because a stale bookmark should start from the beginning rather than raise.
- Mapping utilities for datetimes, booleans and JSON, with naive datetimes refused at the storage boundary and stable JSON key ordering so content fingerprints do not shift.
- `AlembicMigrationRunner` -- drives Alembic against the application's own connection, so migrations run on the database thread with the application's pragmas in force.

**Migration framework**

- `alembic.ini`, `migrations/env.py` and a script template. Batch mode is enabled, because SQLite cannot alter a column in place.
- Migration `0001` creates `schema_metadata`. A baseline that creates nothing cannot be tested in either direction, so the machinery would go unverified until the first business table.
- Constraint naming conventions declared once. Without them SQLite invents names, and an unnamed constraint cannot be dropped by a later migration.

**Composition root**

- `Database`, `UnitOfWorkFactory` and `MigrationRunner` registered and overridable by constructor injection.
- `start_database()` opens the database, checks the schema position and migrates when configured, refusing outright if the database was written by a newer version.
- Async lifecycle (`aclose`, `async with`) so the worker thread is released deterministically.

**Configuration**

- `database` section: path, journal mode, synchronous mode, busy timeout, auto-migrate, archive directory.

**Command line**

- `db status`, `db migrate`, `db downgrade`, `db check`. `doctor` now reports the schema position, and diagnoses rather than repairs -- silently migrating would make a read-only command modify user data.

**Tests**

- 391 passing, 93% coverage. New shared contract suite for the unit of work, run against both the SQLAlchemy implementation and an in-memory fake.
- Covers pragma verification, migration up/down/up round trips, rollback, savepoint isolation, event release timing, concurrent reads and writes, cursor pagination walking every row exactly once, and startup refusing a newer database.

**Dependencies**

- Added `sqlalchemy` and `alembic`.

## Fixed

Three defects found by the Milestone 0.2 tests before any of this shipped:

- **A pool deadlock.** `QueuePool(pool_size=1)` held its one connection for the process lifetime, so any second checkout blocked forever rather than failing. Replaced with `StaticPool`, which expresses the actual design -- one connection, reused -- instead of a pool that happens to hold one.
- **SQLAlchemy autobegin blocked transactions.** Reading pragmas or running a health check on the long-lived connection silently opened a transaction, so the next explicit `begin()` was refused. Every read outside a unit of work now releases its implicit transaction.
- **Concurrent units of work collided.** One connection holds one transaction, so a second concurrent use case failed with an error unrelated to what the caller did. Transactions now serialize on a bounded lock: the second waits, and a genuine overlap surfaces as a named error rather than a hang.

## Changed

- `DATABASE.md`: added the connection and transaction model, pragma read-back, `schema_metadata`, constraint naming conventions, and renumbered business migrations to begin at `0002`.
- `API.md`: expanded the `UnitOfWork` contract with savepoints, event-release timing and transaction serialization; replaced the `MigrationRunner` sketch with the implemented interface; added the `Database` port.

## Architecture Decisions

- **ADR-034 -- Single Connection and Serialized Transactions** (Proposed). Records what ADR-013's threading decision implies for connections and transactions, and states explicitly that reads serialize behind writes. The remedy -- a reader pool exploiting WAL -- is deliberately deferred, with the measurements required before adopting it written down rather than left to judgement.

### Milestone 0.1 — Core Domain Ports

**Ports (domain layer)**

- `Clock` — UTC-only, timezone-aware wall time, plus a separate monotonic source for durations. Nothing else in the application calls `datetime.now()`.
- `IdGenerator` — time-ordered integer, UUID and correlation identifiers.
- `EventBus` — synchronous publish and subscribe with isolated handler failures.
- `SecretStore` — the only component permitted to hold credential material.
- `DomainEvent` base class. The concrete event catalogue still arrives with the milestones that raise those events.
- `SecretValue` value object: masked in `repr`, `str` and `format`, refuses to pickle, constant-time equality, explicit `reveal()`.

**Implementations (infrastructure)**

- `SystemClock`.
- `UuidV7IdGenerator` — RFC 9562 UUID version 7 with a monotonic counter, strictly increasing even under a frozen or backwards-moving clock, thread-safe, driven by the injected clock.
- `InProcessEventBus` — registration-order delivery, subclass delivery after exact-type handlers, per-handler failure isolation, automatic disabling after repeated failures, bounded publish depth.
- `KeyringSecretStore` (operating system credential store), `EnvironmentSecretStore` (read-only override) and `ChainedSecretStore`, composing the resolution order ADR-021 specifies. Blocking credential-store calls run in a worker thread.

**Composition root**

- All four ports constructed in `Container` and overridable through its constructor, so tests inject doubles rather than patching module globals. The identifier generator is driven by the container's clock, so fixing the clock fixes the identifiers.
- `Container.verify_secret_store()` fails closed when `security.require_secret_store` is set.
- `tgassist doctor` now reports credential-backend availability instead of listing it as unimplemented.

**Error taxonomy**

- Added `SecurityError` (with `SecretStoreUnavailableError`, `ReadOnlySecretStoreError`) and `EventDispatchError` — the branches that now have live consumers.

**Tests**

- New shared contract suite (`tests/contract/`) parametrized over every implementation of each port, production and fake alike. 288 tests, 93% statement coverage.
- Fakes: `FixedClock`, `AdvanceableClock`, `SequentialIdGenerator`, `RecordingEventBus`, `InMemorySecretStore`, `UnavailableSecretStore`. `RecordingEventBus` is written as an independent implementation rather than a subclass, so the contract suite genuinely tests it.
- Edge cases covered: naive-datetime rejection, clock correction versus monotonic time, counter exhaustion, backwards clock, thread safety, event cycles, failure-count reset, pickling refusal, chained-store precedence and deletion.

**Dependencies**

- Added `keyring` (mandated by ADR-021) and `pytest-asyncio` (development only).

## Fixed

- **The event bus swallowed its own refusal.** `EventDispatchError` raised by a nested `publish` was caught by the handler-isolation boundary, so an event cycle stopped silently instead of surfacing. The bus now re-raises its own control error while still isolating handler failures — a handler failing is contained, a cycle is a defect that must reach the publisher.
- **A log field named `event` collided with structlog's first positional parameter**, raising `TypeError` from inside the failure-isolation path — the one place an exception is least welcome. The field is now `event_type`.

## Changed

- `API.md` §5.1–§5.3 and §5.6: recorded where each implementation lives, added `new_uuid()` and the time-ordering guarantee, replaced fire-and-forget delivery with synchronous delivery, and changed `SecretStore.get()` to return `SecretValue`.
- `ARCHITECTURE.md` §8: event delivery is synchronous.
- `ERROR_HANDLING.md` §3: added `EventDispatchError` and refreshed the implementation-status note.

## Architecture Decisions

Three Proposed ADRs arising from implementation:

- **ADR-031 — Synchronous Event Delivery.** Supersedes the fire-and-forget semantics in `API.md` §5.3. Raised rather than changed silently, because it alters a documented contract.
- **ADR-032 — Secrets as a Domain Value Object.** A bare `str` cannot satisfy the documented rule that a secret must not appear in a `repr`.
- **ADR-033 — Identifier Generation Strategy.** UUID version 7, chosen for database index locality.

### Milestone 0 — Foundation & Tooling

**Project structure**

- Clean Architecture package tree under `src/tgassist/` with `domain`, `application`, `infrastructure` and `presentation` layers. Every package states its responsibility in a docstring, verified by test.
- Supporting trees outside the source package: `config/`, `prompts/`, `migrations/`, `resources/`, `scripts/`, `plugins/`, `tests/`.

**Build and tooling**

- `pyproject.toml` with `uv`, hatchling build backend, Python 3.12 floor, and a development environment pinned to 3.13.
- Ruff for linting *and* formatting (Black is not used — Ruff's formatter replaces it, per Step 2's condition).
- mypy in strict mode over source *and* tests, with the pydantic plugin.
- pytest with marker taxonomy (`integration`, `e2e`, `eval`, `slow`), coverage configuration, and `filterwarnings = ["error"]`.
- pre-commit hooks: Ruff, file hygiene, private-key detection, gitleaks, mypy, import-linter.
- `poethepoet` task runner: `poe lint | format | typecheck | arch | test | cov | check`.
- GitHub Actions CI across Windows and Linux on Python 3.12 and 3.13, plus a security job running `pip-audit` and `gitleaks`.
- `.editorconfig`, and a `.gitignore` covering databases, sessions, backups, logs, exports and models.

**Architectural enforcement**

- `.importlinter` contracts encoding the corrected dependency rule (ADR-011): layered architecture, domain independence, domain free of third-party imports, infrastructure isolation, and presentation never importing infrastructure directly.
- `tests/architecture/` asserting the same rules by AST inspection, plus package-structure and circular-import checks. Verified to fail on a deliberately introduced violation.

**Configuration system**

- Typed, validated, immutable configuration (`pydantic-settings`) with complete built-in defaults — the application starts with no configuration files present.
- Six-layer resolution: defaults → `default.yaml` → profile → `local.yaml` → environment → explicit overrides.
- Environment profiles: `development`, `testing`, `production`.
- Unknown keys are a startup error, not a silent ignore.
- Per-key origin tracking, surfaced by `config show`.
- `logging.diagnostic_mode` is rejected by validation in the production profile, because it writes third-party conversation content to disk.

**Logging**

- structlog routed through the standard library, so records from third-party packages pass through the same processor chain.
- Console and size-rotating file sinks; JSON or human-readable rendering; per-logger level overrides; age-based retention cleanup.
- Central redaction processor applying a domain-owned sensitivity policy.

**Composition root**

- Hand-written `Container` supplying configuration, logging, directory creation and a permission report. No DI framework: the object graph is fixed and single-process.

**Command line adapter**

- `tgassist version`, `config show`, `config validate`, `config path`, `doctor`.
- `doctor` reports unimplemented subsystems explicitly rather than returning green while silently omitting them.

**Tests**

- 123 tests: 26 architectural, 97 unit. 92% statement coverage.

## Fixed

Three defects found by the Milestone 0 test suite before any of this shipped:

- **Configuration origin tracking attributed whole sections to one file.** A nested mapping absent from the merge target was assigned wholesale, so a later layer overriding one key left the rest credited to the wrong source.
- **Secrets embedded in a larger string were not redacted.** The value-shape patterns were anchored, so a bare key was caught but a formatted message from a third-party library carried the credential through to the log file. Patterns are now unanchored and the matched span is substituted, keeping the message readable.
- **Keys that name a secret were masked as though they held one.** `require_secret_store` displayed as `********`. Configuration stores secret *names*, not values (ADR-021), so masking them hid information the user needs while protecting nothing.

## Changed

- Exception classes carry the PEP 8 `Error` suffix (`ruff` rule `N818`). `ERROR_HANDLING.md` §3 and the `API.md` error table were updated to match.
- `CONFIGURATION.md`: documented environment profiles and the six-layer resolution order; removed the separate `logging.yaml`, whose keys the main configuration already owns; added an implementation-status note recording that sections arrive with their subsystems.
- `ERROR_HANDLING.md`: added an implementation-status note distinguishing the specified taxonomy from the branches that exist in code.

## Documentation

Architecture stabilization. All project documentation reviewed for internal consistency and corrected.

### Corrected

- **`ARCHITECTURE.md` §9 — dependency direction.** Version 1.0 documented the Clean Architecture dependency rule inverted (`Domain → Infrastructure` allowed, `Infrastructure → Domain` forbidden), which contradicted both ADR-003 and `ARCHITECTURE.md` §2. Implemented literally it would have placed database, Telegram and AI provider imports inside the domain layer. Corrected per ADR-011 and now enforced in CI by `import-linter` contracts.
- **`ARCHITECTURE.md` §2 — high-level diagram.** Version 1.0 showed replies flowing automatically from the Reply Generator through to the Telegram network, contradicting ADR-010. The gateway is now shown as an inbound adapter with a separate, explicitly user-initiated outbound path.
- **`ARCHITECTURE.md` §10 — folder structure.** `tests/` and `docs/` were nested inside `src/`. Corrected.
- **Milestone numbering.** `PROJECT_SPEC.md` §10 defined milestones 1–6 that conflicted with `ROADMAP.md`'s 0–12 (for example, SPEC M5 "Desktop UI" versus ROADMAP M5 "Relationship Intelligence"). `ROADMAP.md` is now the single authority; the conflicting list was removed.
- **Milestone ordering.** `ROADMAP.md` v1.0 placed Conversation Processing (M3) and Memory Engine (M4) before AI Services (M7), although both require a language model. AI abstraction moved to M3; persistence moved before Telegram connectivity.
- **`CHANGELOG.md` 0.1.0 entry.** The previous entry claimed shipped work — initial SQLite schema, repository abstraction, provider abstraction, conversation planner, reply generator interfaces — none of which existed. Corrected to a documentation-only entry below.
- **`PROJECT_SPEC.md` automation clause.** The phrase "unless the user enables automation" implied an automation mode with no defined boundary. Removed; the boundary is now a product constraint (ADR-023).
- **Relationship metrics.** `trust_score` and `friendship_level` were specified with no definitions, ranges or update rules, making them unfalsifiable and untestable. Replaced with deterministic metrics that each have a published formula and a minimum sample size.

### Added

- `DOMAIN_MODEL.md` — 29 entities, value objects, invariants, aggregates, domain services, event catalogue and a metric glossary. Now the authority from which the schema is derived.
- `PRIVACY.md` — privacy principles, data categories, lifecycle, user and contact rights, known limitations.
- `ERROR_HANDLING.md` — exception taxonomy, retry and timeout policy, degradation strategy, anti-patterns.
- `CONFIGURATION.md` — three-store ownership rule, resolution order, complete key reference.
- `PLUGIN_SYSTEM.md` — plugin capabilities, lifecycle, fault isolation and an explicit trust model.
- `VECTOR_SEARCH.md` — embedding pipeline, hybrid retrieval scoring, store implementations, re-indexing.
- `MASTER_ARCHITECTURE.md` — nine Mermaid diagrams covering dependencies, flows, lifecycles and sequences.
- `README.md` — project overview, status, structure and documentation index.
- Twenty new Architecture Decision Records (ADR-011 through ADR-030), all **Proposed**.

### Changed

- `PROJECT_SPEC.md` — thirteen previously unspecified requirement areas added: synchronisation scope, offline behaviour, startup workflow, onboarding, timezone handling, localization, backup policy, data retention, import/export, recovery strategy, multi-account readiness, accessibility, configuration management. Non-functional requirements now paired with measurable targets.
- `API.md` — added `UnitOfWork`, `SecretStore`, `PromptRepository`, `VectorStore`, `MigrationRunner`, `Scheduler`, `Cache`, `Clock`, `IdGenerator`, `FileStore`, `NotificationPort`, `MessageSearchPort`, `AuthorizationHandler`, `StructuredOutputValidator`, `PluginContext`. Separated `VectorStore` from `EmbeddingProvider`. Defined event bus delivery semantics. Added capability negotiation to `LLMProvider`. All I/O methods are now async.
- `DATABASE.md` — added `accounts`, `user_profiles`, `telegram_sessions`, `conversations`, `attachments`, `memory_proposals`, `memory_revisions`, `style_profiles`, `conversation_plans`, `reply_suggestions`, `behavior_recommendations`, `embedding_models`, `sync_cursors`, `notifications`, `ai_providers`, `ai_calls`, `retention_policies`, `audit_log`. Added 18 unique constraints, 4 partial unique constraints, 31 foreign keys and 24 check constraints. Added migration, archive, backup, soft-deletion and multi-account policies, and a complete Mermaid ER diagram.
- `AI_MODELS.md` — added capability matrix, latency and cost expectations, prompt injection mitigation, memory poisoning prevention, confidence calibration, structured output validation, retry and fallback policy, embedding lifecycle, context window management and semantic retrieval strategy. Documented the memory proposal workflow.
- `SECURITY.md` — added an explicit threat model with out-of-scope threats, phased encryption strategy, session encryption, secret management, plugin trust model, audit logging, prompt injection defenses, AI provider data boundaries, compliance considerations and backup encryption.
- `ROADMAP.md` — resequenced, expanded to 15 milestones with falsifiable acceptance criteria, complexity estimates and an explicit scope-risk section.
- `DECISIONS.md` — restructured with an index; ADR-001 through ADR-010 preserved and cross-referenced to the new decisions.

### Database

- Removed the `logs` table. Application logs move to rotating JSONL files; durable security events move to a new append-only `audit_log` table (ADR-027). The original design caused write contention with the single-writer model and made logs unreadable when the database was the failing component.
- Renamed and generalised `ai_analysis` to `analyses`, which was message-scoped only and left conversation-level analyses homeless.

### AI

- Established that AI-derived memories are **proposals** requiring user approval or an explicit auto-approval rule (ADR-019).
- Established capability negotiation and mandatory schema validation with a single repair attempt (ADR-020).
- Established composite execution behind separate ports to bound per-message cost (ADR-029).
- Specified that relationship metrics, confidence calibration and timing recommendations use **no language model**.

### Security

- Established the automation boundary as a structural constraint: no auto-send, no synthetic typing indicators, enforced through the dependency graph rather than configuration (ADR-023).
- Established local-first defaults with per-chat AI data boundaries (ADR-024).
- Established OS credential store as the primary secret backend, with no plaintext fallback (ADR-021).
- Documented prompt injection defenses and their honest limitations.

---

# [0.1.0] - 2026-07-28

Initial documentation release. **No source code exists at this version.**

## Added

### Documentation

- CLAUDE.md
- CLAUDE_WORKFLOW.md
- DEVELOPMENT_WORKFLOW.md
- PROJECT_SPEC.md
- ARCHITECTURE.md
- AI_MODELS.md
- PROMPTS.md
- DATABASE.md
- API.md
- ROADMAP.md
- DECISIONS.md
- SECURITY.md
- TESTING.md
- CONTRIBUTING.md
- CHANGELOG.md

### Project

- Repository initialized.
- Clean Architecture adopted as the target architecture (ADR-003).
- Domain-driven folder structure designed.

### Decisions

- ADR-001 through ADR-010 accepted.

---

# Release Template

```
# [Version] - YYYY-MM-DD

## Added

## Changed

## Deprecated

## Removed

## Fixed

## Security

## Performance

## Documentation

## AI

## Database

## Breaking Changes
```

---

# Versioning Policy

## MAJOR

- Breaking API changes
- Major architectural redesign
- Incompatible database schema
- Significant plugin API changes

## MINOR

- New features
- New AI capabilities
- New plugins
- New database tables
- New UI functionality

## PATCH

- Bug fixes
- Documentation improvements
- Small performance improvements
- Prompt refinements that do not significantly change external behavior
- Internal refactoring without breaking compatibility

---

# AI Change Policy

Whenever AI behavior changes, document: prompt updates, model changes, embedding model changes, memory retrieval changes, context-building strategy, confidence estimation adjustments, evaluation benchmark results.

Prompt and model changes must record the version identifiers, because every persisted AI artifact references them and cache invalidation depends on them.

---

# Database Change Policy

Document new tables, schema changes, index additions, migration updates and data model redesigns.

Every schema change requires a reversible migration and a corresponding update to `DOMAIN_MODEL.md` first.

---

# Documentation Policy

Whenever project documentation changes, record new documents, major revisions, removed documentation and architectural updates.

---

# Security Policy

Security entries should include vulnerability fixes, dependency updates addressing security issues, authentication improvements, encryption enhancements and privacy-related changes.

---

# Breaking Changes Policy

Always describe what changed, why it changed, how developers should migrate, and whether compatibility layers exist.

---

# Release Checklist

- All planned roadmap tasks completed
- Tests passing, including architectural, security and privacy tests
- Documentation synchronized with implementation
- Database migrations verified up and down
- Security review completed
- AI evaluation completed
- Version number updated
- CHANGELOG.md updated

---

# Maintenance Guidelines

- Add entries as work is completed rather than waiting until release day.
- Keep entries concise but informative.
- Avoid duplicating commit messages.
- Group related changes together.
- Preserve historical entries; never rewrite published release notes.
- **Never record work that has not been done.** A changelog that overstates state is worse than none.

---

# Philosophy

The changelog is the historical record of the project. It should answer: what changed, why, whether it affects users or developers, whether it requires migration, and whether it improves security, performance or AI quality.
