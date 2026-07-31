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

### Milestone 10b -- Suggestion Review Queue

Slice 10b: every generated suggestion is stored and waits for a decision.
Generate -> review -> accept or dismiss. **Accepting sends nothing**, because
there is nothing in this application that could send.

**Nothing acts automatically, and it is structural rather than a rule**

- `AcceptSuggestion` is given no gateway, no scheduler and no executor; a test
  asserts on its constructor signature, so wiring one in is a deliberate change
  somebody has to defend.
- The repository has `add`, `get`, `list_pending`, `list_by_chat` and `decide`,
  and no `execute`, `send` or `schedule`. The absence of an operation is a
  stronger guarantee than a rule about not calling one (ADR-062).

**One decision, enforced twice**

- The entity refuses a second decision and names the one already made.
- The repository's single mutation is conditional -- `UPDATE ... WHERE status =
  'pending'` -- so two decisions arriving at once cannot both land.
- No undo, no reopen, no edit. **An edited draft is not what the model
  suggested**, and the record exists to say what it suggested when somebody
  agreed with it.

**Three fields for three audiences**

- `title` for a listing, `description` for the person deciding -- for a reply
  draft, the draft itself -- and `payload` as JSON for a machine that does not
  exist yet.
- The split is what keeps review uniform as the kinds of suggestion multiply: a
  second type needs new payload handling and **no new review**, and no reviewer
  ever has to read JSON to decide.

**Dismissals are kept**

- A record of only what was agreed with cannot show what the generator is
  getting wrong. `list_pending` is the queue -- what has not been decided --
  while `list_by_chat` shows a conversation's whole history, dismissals
  included.

### Database

- **Migration `0014`** -- `suggestions`. Composite foreign keys to chats,
  conversations and AI calls (ADR-043), **all `CASCADE`**: unlike a memory,
  whose provenance is `SET NULL` because approved knowledge outlives the
  exchange it came from, a suggestion is a draft *about* a conversation and
  means nothing once that conversation is gone.
- Ten check constraints, including `(status = 'pending') = (decided_at IS
  NULL)`. **No `updated_at`**, because nothing edits a suggestion.
- A **partial** index for the queue (`WHERE status = 'pending'`) -- an index
  carrying every decided row would grow without bound while serving nothing --
  and a non-partial one for a chat's history.

### Changed

- Generation now writes its draft alongside the `AiCall` in one transaction and
  publishes `SuggestionsCreated`. `chat suggest` names the row it saved.
- Slice 9e's `Suggestion` DTO is renamed `GeneratedSuggestion`: the name now
  belongs to the stored aggregate, and one of the two had to give it up.

### Fixed

- Nothing. No defect was found in existing behaviour while implementing this
  slice.

### Milestone 9e -- Context Assembly & First Real Prompt

Slice 9e: memories reach a model. Conversation + retrieved memories -> one
assembled prompt -> structured answer -> a draft on the screen. **Nothing is
sent, and nothing is stored.**

**A fixed context order, and each position argued**

- System prompt, memories, conversation, task and output format (ADR-061 §1).
- **Memories before the conversation** because they are the *frame*: a
  constraint like "do not mention the old job" changes how every message below
  should be read, and a model that met the messages first has already formed a
  reading by the time it learns the rule.
- **The conversation between them**, delimited and untrusted, so an injection
  attempt is bracketed by trusted rules above and the actual instruction below.
- **The task last**, because a model's final instruction is the one it follows
  most reliably -- and the one that must survive an injection that got past
  everything above.
- **Rejected:** an order chosen for readability, and letting the model decide
  what to read first. Both make the prompt unreproducible, and an
  unreproducible prompt makes every later comparison meaningless.

**A trim order with two floors**

- **Never removed:** the system prompt, the task, the output format, and the
  **most recent message** -- without it there is nothing to respond to.
- **First to go: the oldest messages.** In a chronological record, recency is
  relevance.
- **Then the lowest-ranked memories**, and the justification matters because the
  instinct is the opposite: memories arriving here have *already survived
  retrieval's budget*, so each was deliberately chosen, while message history is
  bulk kept because it is cheap.
- **Nothing is ever shortened to fit.** A truncated fact is a fact that was
  never stated.

**Attribution is checked**

- The prompt supplies each memory with its key; the model reports which it used;
  every key is **verified against what was actually supplied**. A key that was
  not is a fabricated citation -- discarded, counted, and flagged on the output.
- What that catches is narrow but real: a suggestion claiming grounding it does
  not have is worse than one claiming none, because the first invites trust.

**The assembler writes no prose**

- It decides *what* is included and *in what order*; the prompt file decides
  *what it means and what to do about it*. Not one imperative sentence in
  `context_assembly.py` reaches a model, and the prompt version recorded against
  a call therefore covers all the wording that was sent.

**Commands**

- `tgassist chat suggest <chat>` -- memories supplied and omitted, what the
  budget trimmed, the transcript, the prompt version, the token estimate, the AI
  call and the draft. `--show-prompt` prints the exact text that was sent.

**Tests -- 86 added, suite at 3243**

- 37 over the assembler: ordering, each trim rule, the floors, determinism,
  empty memories, empty conversation, attribution keys, and that a memory cannot
  forge a delimiter.
- 53 behaviour tests: prompt generation, malformed output, a repair that works,
  a repair that does not, a provider timeout, the privacy gate, fabricated
  attribution, eight against a **real SQLite database** including the two
  rollback cases, and four end-to-end command-line tests.

## Fixed

- **An approved memory could forge a prompt delimiter.** Memories are trusted
  content -- a person accepted them -- so they are not wrapped in the "this is
  data, not instructions" markers. But a memory's *text* came from a model
  reading a conversation, so it can contain anything that conversation did: a
  value carrying `<<<END_CONVERSATION_CONTENT>>>` would sit outside the
  delimited block and close it early for everything below. Found when retrieval
  was first connected to a prompt. Memory text and keys are now neutralised
  without being delimited: **trusting a fact is not the same as trusting its
  punctuation** (ADR-061).

## Changed

- **The one-repair loop moved out of `ExtractMemories` into
  `StructuredAiTask`.** ADR-058 put it inside the only structured task there
  was; there are now two, and "exactly one repair" must not be able to become
  "one here and two there". Everything about *what* is asked stays with each
  caller.

### Milestone 9d -- Memory Retrieval

Slice 9d: approved memories become usable. Conversation -> contact -> ranked
memories -> token budget -> context. **No embeddings, no similarity, no model.**

**Deterministic before semantic, and the reason is measurement**

- A vector index shipped first would be compared against nothing, and "semantic
  retrieval improved the suggestions" would be a claim with no denominator. This
  is the baseline that makes the comparison possible (ADR-060).
- It also builds the parts a semantic version still needs and cannot borrow from
  a model: the token budget, the order a context degrades in, and the record of
  what was left out.

**Five ranking keys, lexicographic, no weights**

- **Category priority** -- ordered by what it costs to get wrong. A `constraint`
  first (ignoring one is worse than saying nothing), then `open_question`, then
  time-sensitive facts, then identity, then durable context, then preference,
  then interests. `other` last.
- **Importance**, then **confidence**. Importance first deliberately: a person's
  judgement of what is worth knowing outranks a machine's estimate of what is
  true, and self-reported confidence is poorly calibrated.
- **Recency** by when the fact was *accepted*, then the **identifier** as a
  total tie-break, so ordering never depends on what the database returned.
- **Rejected: a weighted score.** It reads as principled and is not -- the
  weights would be invented, no test could show one set beats another, and
  changing one silently reorders everything.
- **`last_retrieved_at` is not a ranking key.** Ranking by it would make a
  retrieved memory rank higher and so be retrieved again -- a feedback loop, not
  a relevance signal. It is recorded so the *absence* of retrieval stays
  visible.

**The budget is spent after ranking and never changes it**

- What does not fit is **skipped**, and the walk continues, so one long fact near
  the top does not empty the context beneath it. Order among the selected is
  untouched and every omission is reported with its reason.
- **Nothing is ever shortened to fit.** A truncated fact is a different fact.

**Retrieval never crosses contacts**

- A private chat retrieves that person's memories; a chat with no single
  counterpart retrieves the facts about nobody in particular -- and a private
  chat does not see those either. Strict in both directions, enforced by the
  repository.

**Accounting, and what it means for immutability**

- `BuildMemoryContext` records the retrieval; `GetMemoryContext` does not.
  Looking at what would be sent is not using it, and an inspection that inflated
  the counters would corrupt the measurement it exposes.
- The count is incremented **in SQL** over the whole selection in one statement,
  in the same transaction as the read, so concurrent contexts cannot lose a
  count and a context of twenty memories costs one write.
- **`Memory` is still immutable.** What retrieval writes is bookkeeping *about*
  a fact, not the fact -- a refinement of ADR-059 rather than an exception to
  it.

**Commands**

- `tgassist memory context <chat>` -- the selection, the ranking order, why each
  memory placed where it did, the token usage and every omission. `--record` to
  count it as a real retrieval would.
- `tgassist memory accept --importance low|normal|high|critical`.

**Tests -- 124 added, suite at 3157**

- 44 over the selector: every ranking key in isolation, every key's precedence
  over the next, stability under input reordering, the budget, the cap, and that
  every category has a distinct priority.
- 38 more contract obligations over both repository implementations: contact
  isolation, the contactless partition, forgotten memories ignored, ordering,
  the limit, and retrieval accounting including concurrency-safe increments.
- 42 behaviour tests: context generation, a chat without a contact, budget and
  cap exhaustion, candidate truncation, rollback during the read, rollback after
  the accounting, eight against a **real SQLite database**, and eleven
  end-to-end command-line tests.

**`MemoriesRetrieved`**

- Published after a recorded retrieval commits, carrying what was selected, how
  many were considered and what it cost. `GetMemoryContext` publishes nothing:
  an inspection is not a retrieval, and one that announced itself would make the
  events disagree with the counters.
- **Nothing subscribes yet.** The consumer is known rather than guessed --
  whatever eventually reports on retrieval quality reads this, and the
  alternative is a component that polls the counters.

## Changed

- `ScriptedAiProvider.token_count` now delegates to the domain's
  `estimate_tokens`, so the fake's accounting and the budget the selector
  enforces cannot drift apart.
- ADR-060 gained three further rejected alternatives -- random sampling,
  asking a model which memories to use, and retrieving inside repository
  queries -- and a stated bound on the token estimator's error.

### Milestone 9c -- Proposal Review & Memory Creation

Slice 9c: the lifecycle closes. `MemoryProposal` -> accept or reject ->
`Memory`. Only accepted proposals become memories, and nothing bypasses review.

**A memory is a different thing from a proposal**

- Different aggregate, different table, different identifier. A proposal is what
  a model said; a memory is what a person decided to believe, and every later
  feature that asks "what do we know" reads a table nothing can write to without
  a decision (ADR-059).
- A memory takes a **new** identifier at acceptance. The two have different
  lifetimes -- the proposal is a permanent record of an extraction, the memory
  can be forgotten -- and one identifier for both would confuse them everywhere.

**Identity belongs to the application**

- `MemoryKey` is a deterministic normalisation of the value: case folded,
  punctuation dropped, whitespace collapsed, truncated. "Lives in Lisbon." and
  "lives  in lisbon" are one fact.
- **The model never supplies it.** A key is an identity, and a model that could
  name one could collide with an existing memory -- silently preventing a true
  fact from being stored, or claiming the identity of one already there.
- **It deduplicates; it does not detect contradictions.** "Lives in Lisbon" and
  "Lives in Porto" are different keys, so both are stored. That is deliberate,
  it is the price of not letting a model name the subject of a fact, and it is
  the largest gap this milestone leaves.

**A decision is made once**

- Two independent enforcements: the entity refuses `decided()` on anything but a
  pending proposal, and the repository's one update names `pending` in its
  `WHERE` clause -- so a check-then-write cannot be overtaken, and two decisions
  racing cannot both win.
- **No undo and no reopen.** Reversing an acceptance would have to decide what
  becomes of a memory that has since been read and acted on; reopening a
  rejection would mean a fact a person declined could appear anyway. Both are
  already recoverable by ordinary means.
- Accepting twice is refused **with the identifier of the memory it produced** --
  somebody accepting twice has usually forgotten they did it once.

**Acceptance creates exactly one memory, in one transaction**

- The decision and its consequence are the same event. A committed acceptance
  with no memory would be a fact the user believes they kept and cannot find.
- "Exactly one" is a **unique index on `memories.proposal_id`**, not a rule the
  use case keeps.

**Memories are immutable, and forgettable**

- No edit method and no `update` on the repository. Correcting a memory means
  forgetting it and accepting a fresh proposal, because an edit in place would
  keep the provenance while changing the fact.
- Deletion is soft -- a timestamp, not a flag, because retention has to ask
  "deleted before when" -- and it **frees the key**, so the same fact can be
  accepted again.
- Provenance is `SET NULL`, not `CASCADE`: `ai_calls` and `memory_proposals`
  both cascade from `chats`, so without it deleting a chat would silently erase
  approved knowledge. What is lost is the trail, not the fact.

**Commands**

- `tgassist memory accept` / `reject` / `list` / `show` / `forget`.

**Tests -- 165 added, suite at 3033**

- 74 contract obligations over both repository implementations, weighted towards
  the three unique indexes, soft deletion and the contact purge.
- 14 more over the proposal repository's one mutation, including that a second
  decision changes nothing.
- 77 behaviour tests: accept, reject, duplicate acceptance, duplicate rejection,
  accepting a rejected proposal, rejecting an accepted one, rollback before
  commit, rollback after the memory was written, duplicate key, cascade
  behaviour, eleven against a **real SQLite database**, and thirteen end-to-end
  command-line tests.

## Fixed

- **A memory could survive a chat deletion as a row and not as an object.** The
  schema nulls a memory's provenance when the chat it came from is deleted --
  deliberately, so approved knowledge is not erased -- but the entity required
  an AI-derived memory to name its proposal and call, so the surviving row could
  not be reconstituted. Found by the cascade test. The invariant is now
  **all-or-nothing**: provenance is complete or absent, never partial, which
  permits the loss the schema intends and still refuses the half-state nothing
  can produce.

## Changed

- **`tgassist memory show` now shows a *memory*.** The command that shows a
  proposal is `tgassist memory proposal`. Slice 9b had `show` for proposals, and
  one name for two different things is a name that will be typed wrongly.
- `tgassist memory forget` is **beyond the five commands the slice specified**,
  and is included because `CLAUDE.md` and `PRIVACY.md` §6 both require that a
  user can delete what the application remembers -- and this is the first slice
  in which there is anything to delete.

### Milestone 9b -- Memory Proposal Extraction

Slice 9b: the first complete AI feature. Conversation -> prompt ->
`ExecuteAiTask` -> structured output -> `MemoryProposal` -> review queue.

**Nothing is remembered**

- Every extracted fact becomes a `MemoryProposal` with status `pending`. There
  is no code path in this slice that changes a status: the repository has no
  `update` and no `delete`, and the aggregate has no method returning a changed
  one, so *accepted* and *rejected* are terminal by being **unreachable**
  (ADR-058).
- The alternative -- writing memories and letting the user correct them -- fails
  in a way that is hard to recover from. A wrong memory is *retrieved*, put into
  later prompts and used to justify later suggestions, so an error propagates
  into work the user never connected to the extraction that caused it.

**The model supplies four fields, and only four**

- `category`, `value`, `confidence`, `evidence`. Identifier, timestamp,
  conversation, AI call, prompt version and status are assigned by the
  application, and the output schema sets `additionalProperties: false` -- so an
  answer carrying an `id` or a `status` **fails validation** rather than being
  partially trusted.

**Prompts are versioned assets, not strings**

- `system.md` and `memory/extract.md` live in `src/tgassist/prompts/`, **inside
  the package**: a prompt outside the wheel is a prompt an installed application
  does not have. Discovery is by `_registry.yaml`, never by globbing.
- The **version lives only in the prompt's front matter**. `PROMPTS.md` §3 put
  it in two places, and two places recording a version is one too many for them
  to agree.
- Everything is validated at startup: the file, its front matter, its id, and --
  in both directions -- that its declared inputs match the placeholders its body
  uses. A prompt found broken while a user waits is the same defect found at the
  worst moment.
- **Untrusted content is delimited by the prompt model, not by the template.** A
  template that had to remember the markers is one that can forget them. Any run
  of three or more angle brackets in untrusted text is collapsed to two, so no
  message can forge a boundary. Previously-stored proposal values are treated as
  untrusted too -- they are model output derived from conversation content that
  nobody has reviewed.

**Structured output is validated before anything interprets it**

- Shape only: required fields, types, enumerations, ranges, lengths. No business
  rules, so the same validator serves later features without acquiring an
  argument for each.
- **A deliberately small JSON Schema subset, and any schema using more is
  refused when it loads.** That check is what makes a hand-written validator
  safe: an unimplemented keyword is a startup failure rather than a constraint
  that silently passes. No new dependency.
- **Exactly one repair attempt.** The model gets its own answer back with the
  violations named, and is forbidden from changing the content -- a repair that
  added facts would be a second, unreviewed extraction.

**Three deterministic filters, and the first is the important one**

- **Ungrounded evidence.** The quotation must appear in the conversation the
  model was shown. The cheapest anti-hallucination check there is, and the one
  that catches a fluent, plausible fact nobody ever said.
- **Low confidence**, below `memory.min_confidence`.
- **Already proposed**, against stored proposals *including rejected ones*, and
  against the batch itself.
- Every discard is **counted**. A run that returned nothing and a run that
  discarded eight invented claims are very different events.

**The model call sits inside no transaction**

- Read in one, call the model in none, write in another. Holding the single
  application-wide transaction across a call that takes seconds would stop
  everything else in the process (ADR-034). The unique index on
  `(account_id, conversation_id, category, value)` is the backstop, so the worst
  case is a proposal that is not stored -- never one that is wrong.

**Commands**

- `tgassist memory extract <conversation>`, `memory proposals`, `memory show`.
  No approve and no reject: this slice has nothing to approve *into*.

**Tests -- 240 added, suite at 2868**

- 71 over prompts and structured output, including the shipped prompt files:
  that untrusted content cannot forge a delimiter, that a declaration and a
  template cannot drift apart, and that every registry failure is reported at
  load.
- 84 contract obligations over both repository implementations, weighted towards
  the two composite foreign keys and the one-fact-per-conversation index.
- 85 behaviour tests: valid proposal, malformed JSON, repair succeeding, repair
  failing, missing field, duplicate, low confidence, empty extraction, AI
  refusal, provider timeout, rollback before commit, rollback after proposals
  were written, deterministic replay, seven against a **real SQLite database**,
  and seven end-to-end command-line tests.

## Changed

- `ScriptedAiProvider` gains `script_json()`, which serialises with sorted keys
  so the same payload always produces the same text. Malformed answers are still
  scripted as exact text: a helper that could only produce valid JSON could not
  describe the failures worth testing.
- `Container.start()` now loads and validates the prompt registry, **before**
  opening the database. It is the cheapest check and the one whose failure is
  entirely our own fault.

### Milestone 9a -- AI Provider Boundary

Slice 9a: the complete AI integration boundary. Not memory extraction -- this is
the layer that makes every later AI capability deterministic, testable and
provider-independent.

**One port, one use case**

- `AiProvider` has two members: `model` and `generate`. A capability is a
  property of the *model*, not a class of client -- one endpoint answers a
  completion, a classification and a structured extraction -- so there is no
  `CompletionProvider`, `ChatProvider` or `EmbeddingProvider` (ADR-057 §1).
- `ExecuteAiTask` is the only place a model is invoked. It resolves the account,
  reads the chat's `ai_processing_mode`, applies the timeout, calls the provider
  and records what happened. It **interprets nothing** -- no parsing, no schema,
  no repair -- because every task's schema would otherwise land inside the one
  component every task shares.
- `AiModel` is a value object, not a stored `ai_providers` row. Its
  `data_boundary` comes from the **vendor**, never from configuration: a
  boundary a user could edit would put the privacy guarantee in a file.

**The privacy gate, including its fourth row**

| chat mode | local model | external model |
| --- | --- | --- |
| `disabled` | refused | refused |
| `local_only` | allowed | refused |
| `cloud_allowed` | allowed | allowed |
| *no chat named* | allowed | **refused** |

- Content that names no chat carries no permission, and in a local-first
  application the absence of a permission is not a permission.
- A refusal is **recorded** before the error is raised, and the error names the
  record. An audit containing only the calls that were allowed cannot show that
  a call was blocked.

**`ai_calls` -- append-only, and a digest rather than an answer**

- No `update` and no `delete` in the repository. The absence of both is the
  guarantee, the same discipline `messages` has (ADR-046).
- The prompt is never stored, under any setting. The response is stored as a
  truncated SHA-256 digest -- which is what deterministic replay actually needs,
  *did these two runs produce the same answer* -- and as text only when
  `ai.store_responses` is on, which the production profile refuses. The same
  arrangement `logging.diagnostic_mode` already has (ADR-057 §6).
- Cost is `Decimal` in the domain and **TEXT** in the column. Money in fractions
  of a cent summed over many rows is exactly where binary floating point drifts.
- `PromptVersion` is required on every call from the first one. Without it the
  first time an output changes there is no way to tell whether the model changed
  or the prompt did, and that question is asked after the change.

**Two implementations, neither of which opens a socket in tests**

- `AnthropicProvider` over an injected `HttpTransport` (stdlib `urllib`; **no
  new dependency**), so the real adapter's request body, headers, parsing,
  stop-reason mapping and usage reading are all exercised without a network.
- `ScriptedAiProvider` is **shipped source**, not a test fixture, and is the
  default vendor. A fresh installation has a working AI boundary that costs
  nothing, and the provider the tests exercise is the provider that ships. It is
  deterministic by construction: queued answers and failures, latency as a
  parameter, `len(text) // 4` tokens. Nothing in it samples randomness.

**Commands**

- `tgassist ai run <content>`, `ai show <id>`, `ai list`. `run` is the whole
  boundary end to end; `show` prints metadata and never a prompt.

**Tests -- 188 added, suite at 2628**

- 50 contract obligations over **both** providers -- the scripted one and the
  real Anthropic adapter -- covering text, finish reasons, usage, the answering
  model, deterministic replay, normalised failures, recovery, request
  validation, and what goes on the wire.
- 54 contract obligations over both repository implementations, weighted towards
  the composite foreign key, the append-only shape and exact cost round-tripping.
- 84 behaviour tests: the value objects, success, every failure mode, timeout,
  cancellation, the four rows of the gate, deterministic replay, reading
  records, six against a **real SQLite database**, and eight end-to-end
  command-line tests.

## Fixed

- **`ai_calls` could not survive a chat deletion.** The composite foreign key
  `(account_id, chat_id) -> chats` was declared `ON DELETE SET NULL`, on the
  reasoning that what a call cost is still true after the chat is gone. SQLite
  nulls *every* column of a composite key, including the NOT NULL `account_id`,
  so deleting a chat that had AI calls failed outright with an integrity error.
  Found by the repository contract suite, which is the only place a chat is
  deleted. It cascades now: a record derived from a deleted chat is residue of
  that chat, which is why every other child of `chats` already cascades. The
  consequence is deliberate -- deleting a chat removes its calls from the spend
  history.

### Milestone 3.0 -- Conversation Segmentation

Slice 8: grouping stored messages into bounded episodes, deterministically and
without a model of any kind.

**The aggregate**

- `Conversation` joins the domain model with `started_at`, `ended_at` and
  `message_count`. It is the first aggregate here that is **derived** -- every
  other one records something a person or Telegram decided -- which is why it
  has no external identifier, why it can be recomputed at any time, and why it
  is the only aggregate whose repository has a `delete`.
- **`ended_at` is not nullable and there is no `is_open`.** Version 1.0 used the
  first to express the second. A conversation derived from messages that already
  exist always has a last one; and whether it may still grow depends on how long
  ago it ended -- on *now* -- which no stored flag can keep true without a job to
  correct it. `Conversation.is_open_at(now, gap)` asks it against an instant.

**Membership is the time range**

- A message belongs to the conversation whose `[started_at, ended_at]` contains
  its `sent_at`. **Messages carry no `conversation_id` and there is no join
  table** (ADR-056). Two reasons: `Message` is append-only and its repository has
  no update path at all (ADR-046), so storing the link would reopen exactly what
  that discipline closed; and conversations within a chat do not overlap, so the
  range already *is* the membership.
- It also keeps a rebuild cheap. Fifty thousand messages produce a few hundred
  conversation rows, and only those are written.
- Non-overlap is a unique `(account_id, chat_id, started_at)`, which replaces
  version 1.0's partial unique index on `is_open` and enforces something
  stronger.

**The rule, and why it is replayable**

- An inactivity gap (`conversation.gap_minutes`, default 360) plus a message cap
  (`conversation.max_messages`, default 200) -- the documented rule, unchanged.
- It is a pure function in `domain/services/segmentation.py`: no clock, no
  configuration lookup, no AI. It reads `sent_at` and a count, in the total order
  `(sent_at, telegram_message_id, id)`. Every component of that key is immutable
  once stored, which is what makes a boundary reproducible -- and the tiebreak is
  deliberately *not* insertion order, because a backfill stores an older message
  later.
- Rejected: calendar day (cuts an evening exchange at midnight, and needs a
  timezone Telegram does not report), sender changes (turns a back-and-forth into
  dozens of conversations), reply chains (not stored, mostly absent, and a reply
  to a month-old message would splice two episodes).

**Identity is matched, not generated**

- A recomputed segment claims **the stored conversation owning the plurality of
  its messages**; a stored conversation may be claimed by at most one segment,
  the earliest, with ties to the lowest identifier. Both tie-breaks exist to make
  the result a function of the arguments alone.
- Four lines, and it gets the four interesting cases right: **extension** (the
  last conversation grows and keeps its identity), **a new episode** (created;
  earlier ones untouched), **merge** (the larger contributor survives), **split**
  (the earlier half keeps the identity, because a conversation can be claimed
  only once).
- Without it a rebuild would replace every conversation with an
  identical-looking new one, and Milestone 8's summaries and plans would point
  at rows that no longer exist.

**Incremental by window**

- A boundary depends only on the gap to the message before it, so a new message
  can change nothing earlier than the conversation it follows. A pass
  re-segments from that conversation's *start* -- not from the new message, which
  would see nothing before it, call it a boundary, and split an episode at
  whatever instant the caller named.

**Commands**

- `tgassist conversation rebuild <chat>`, `conversation list <chat>`,
  `conversation show <id>`. None of them reaches Telegram: `rebuild` recomputes,
  it does not fetch.

**Tests -- 136 added, suite at 2440**

- 80 covering the rule (including that the order messages arrive in does not
  matter), the entity's invariants, first pass, idempotency, live extension and
  creation, late history, merge, split, resumed backfill, membership, three
  injected failures, seven against a **real SQLite database**, and 11
  end-to-end command-line tests running the goal's scenario.
- 56 contract obligations over both repository implementations, weighted towards
  the composite foreign key and the unique start.

## Fixed

- **`IngestMessages.execute` never announced what it stored.** Slice 7 gave the
  backfill and live synchronisation an event to publish and left the third
  ingestion path -- the one the CLI and any import tool uses -- silent. Nothing
  noticed until segmentation subscribed, because until now nothing listened. The
  rule that removes the asymmetry is stated where both live: **whoever commits,
  announces.** `execute` owns its transaction and publishes; `ingest_within`
  leaves the commit to its caller and does not.

## Changed

- `MessagesIngested` gains `oldest_sent_at`. `newest_sent_at` cannot say how far
  back a batch reached -- a backfill page of a hundred messages from last year
  has a newest that is also last year -- and how far back it reached is exactly
  what decides which conversations a re-segmentation must revisit.
- `MessageRepository` gains `list_since`: one chat's messages from an instant
  onwards, **oldest first**. Served by the index the history listing already
  needed, so segmentation added none of its own. Still no `update` and no
  `delete`.

## Architecture Decisions

- **ADR-056** -- a conversation is a time range with a matched identity, and
  membership is not stored. Covers identity, the rule, incremental
  re-segmentation, boundary stability and stored-versus-derived state.
  Supersedes `DOMAIN_MODEL.md` §5.7's `is_open` and `DATABASE.md`'s partial
  unique index on it.

## Scope note

Sixteen source and test files were created or modified, plus one migration and
one configuration file -- within the twenty-file limit.

### Milestone 2.9 -- Live Update Dispatch

Slice 7 of `TELEGRAM_ARCHITECTURE.md`: consuming TDLib's update stream, so the
application stays current once its history is stored.

**The stream**

- `TelegramGateway.updates()` yields `TelegramUpdate` values in arrival order.
  It is **not** a second consumer of `TdjsonClient.receive()` -- the gateway
  already owns the only one (ADR-051), and a second would split the stream
  rather than duplicate it. The dispatch loop maps `updateNewMessage` into a
  `NewMessage` and offers it to a bounded queue that `updates()` drains.
- **One consumer at a time**, refused rather than left to be discovered as
  missing messages: the queue holds one item per update, so two iterators would
  take turns.
- **Backpressure, not loss.** A full queue stops the dispatch loop, which stops
  draining the client queue, which stops the receive thread, which leaves TDLib
  holding the backlog. Nothing is dropped anywhere along that chain.
- **Unknown update kinds are counted and dropped.** So is an `updateNewMessage`
  this version cannot map. A TDLib release that adds a kind must not be able to
  stop the loop, because a stopped loop leaves every waiter hanging on a state
  that can no longer change.

**Why the ordering cannot lose an update**

- The queue starts filling at `connect()` -- before chat synchronisation, before
  the backfill, before anything consumes it. The gap between finishing one phase
  and starting the next therefore contains nothing.
- What a queue cannot cover is a process that was not running. So a live run
  **catches up first**, paging forward from `newest_synced_message_id` until it
  meets the stored range. Backfill could never do this: it walks *downwards*
  from the oldest stored message and never looks above the top. This is the
  reader Milestone 2.8 recorded that field for (ADR-054, ADR-055).

**One ingestion path, processed serially**

- Backfill, catch-up and live updates all go through `IngestMessages`. A
  duplicate update is recognised by the partial unique index and costs nothing
  (ADR-045).
- Updates are processed **one at a time, one transaction each**. ADR-034 permits
  one transaction at a time for the whole application, so per-chat or pooled
  concurrency would contend for the same connection and buy nothing but a
  nondeterministic order to reason about.
- One update failing rolls back that update and nothing else; the drain
  continues, and the reason is kept in the report.

**Lifecycle**

- `BackgroundTaskSupervisor` (infrastructure) restarts a task that fails
  recoverably, with growing backoff, and gives up after
  `telegram.live_max_restarts`. It does **not** restart a task that returned
  normally -- a consumer that reached the end of its stream has finished -- and
  does not fight cancellation, which is how shutdown is expressed.
- `Container.run_live_sync` sequences shutdown: cancel the consumer, let its
  in-flight transaction roll back through the unit of work's own exit path, then
  let the caller close the gateway and the database. It lives at the composition
  root because joining an infrastructure supervisor to an application use case is
  what a composition root is for.

**Events**

- `MessagesIngested` is published, at last -- once per committed batch, by all
  three producers, and **after** the commit rather than inside it. It carries a
  `source` field (`backfill`, `catch_up`, `live`) that ADR-050 did not name: a
  subscriber that treated fifty thousand back-filled rows like one arriving
  message would do fifty thousand times the work it meant to.

**Commands**

- `tgassist sync live` follows Telegram until Ctrl+C.
- `tgassist sync status` reports what is stored -- per chat, its state and the
  span of its stored range. It opens no connection, so it says what the last run
  achieved rather than what is true in Telegram now.

**Tests -- 83 added, suite at 2304**

- 68 in `test_live.py`, covering ordered delivery, duplicate updates,
  interleaved chats, unknown update kinds, event publication, a handler that
  raises, failure isolation, the catch-up in seven shapes, graceful shutdown,
  supervised restart, unrecoverable failure, the supervisor's own policy, and
  **11 end-to-end command-line tests** running the goal's scenario: follow,
  interrupt, restart, and no duplicates.
- 15 contract obligations over both gateway implementations, including that the
  same updates arrive identically through the fake and through real TDLib.

## Fixed

- **A supervised task looked finished before it had started.** `create_task`
  only schedules, and `TaskStatus.running` was set inside the task -- so a
  caller polling `is_running` before the loop next ran saw `False` and concluded
  the work had ended. `tgassist sync live` consequently stopped immediately and
  reported nothing. The status is now marked running synchronously, in `start`.
- **`SyncHistory` gained an argument and the harness did not**, which was caught
  by thirty-six existing tests rather than at review. Recorded because the
  lesson is the reverse of a complaint: a constructor that takes one collaborator
  per dependency makes an omission a compile-time-shaped failure.

## Changed

- `SyncHistory` now publishes `MessagesIngested` per committed batch. Its
  translation helpers `incoming_from` and `sender_kind_of` are public, so live
  synchronisation performs the *same* translation -- a message read live and the
  same message read from history cannot disagree about whose side it is on.

## Architecture Decisions

- **ADR-055** -- live updates are dispatched by the one receive loop, buffered
  from connect, and consumed serially. Covers dispatcher ownership, start-up
  ordering, parallelism, the unknown-update policy and shutdown semantics.
- **ADR-050** is closed out. Decision 3 (`MessagesIngested`) is implemented.
  Decision 4's ingestion serialiser is **satisfied without a queue**: the two
  producers never run concurrently, so a single consumer task already serialises
  every write. The decision stands; the machinery it anticipated was not needed.

## Scope note

Fourteen source and test files were created or modified, plus one configuration
file -- within the twenty-file limit. No migration: live synchronisation writes
the same rows the backfill does.

### Milestone 2.8 -- Resumable Message Backfill

Slice 6 of `TELEGRAM_ARCHITECTURE.md`: reading a chat's history backwards and
storing it, one bounded batch at a time, resuming exactly where the last run
stopped.

**The bookmark**

- `SyncCursor` joins the domain model, keyed by its **chat** rather than by a
  surrogate: exactly one cursor per chat, so the invariant is the key (ADR-054,
  applying ADR-038's reasoning). `account_id` rides along so the foreign key can
  be composite, which is what makes a cursor for one account's chat
  unattachable to another's.
- It stores a **Telegram message identifier**, not a timestamp. Telegram pages
  history by identifier; identifiers are unique and totally ordered within a
  chat, and timestamps are neither, so a timestamp cursor either re-reads or
  skips at every page boundary.
- Both ends of the range or neither, enforced by the entity *and* a check
  constraint. A floor with no ceiling describes a range whose extent nobody can
  state.
- `backfill_complete` is read together with `backfill_horizon`: "complete" means
  complete *for that horizon*, so raising the configured depth reopens the
  cursor and continues from the same floor instead of reporting success.

**The five guarantees, and where each one lives**

- **Resumability** is the cursor. Nothing else -- no reconciliation pass, no
  repair logic.
- **Crash safety** is the transaction boundary. Messages and cursor are written
  in *one* unit of work, so a process that dies mid-batch leaves neither. The
  test that proves it throws an exception one statement before the commit and
  asserts that no message was persisted and the bookmark did not move.
- **Idempotency** is the partial unique index (ADR-045) plus the lookup
  ingestion already performed. A `--reset` re-reads all 25 messages and stores
  none of them.
- **Bounded transactions** are `telegram.backfill_batch_size`, default 100.
- **Deterministic progress** is a property rather than a hope: every batch moves
  the bookmark down or ends the run, and a batch that leaves it where it was
  stops with `no_progress`.

**What ends a run**

The first of: an empty page (the beginning of the chat), a page older than
`telegram.backfill_horizon_days` (default 365), the caller's `--max-batches`, or
an error. The per-chat cap in `PROJECT_SPEC.md` §4.1 is **not** implemented: it
needs a count of stored messages per chat, which needs a repository method and an
index that should be chosen by the query using it.

**Commands**

- `tgassist sync history [CHAT]`, with `--resume`, `--reset` and `--max-batches`.
  Omitting the chat backfills every chat with synchronisation switched on, paging
  the chat list to exhaustion rather than taking the first page -- a silent cap
  there would look exactly like an account with fewer chats.
- `--resume` is the default behaviour stated explicitly, and it is **refused
  alongside `--reset`**: the two ask for opposite things, and a flag that
  silently lost to another would be worse than no flag.

**Tests -- 111 added, suite at 2221**

- 64 covering the cursor's own rules, empty and single and multi-page histories,
  interruption by batch limit and by an exception, resume, `--reset`, duplicate
  identifiers, the horizon in both directions, sender attribution, and the
  refusals.
- 36 contract tests over both repository implementations, weighted towards the
  composite foreign key.
- 9 end-to-end command-line tests running the goal's own scenario: back-fill,
  interrupt, `--resume`, and no duplicates. Two more went to the ingestion
  pipeline, where the duplicate-within-a-batch defect belonged.
- The whole backfill runs against **real TDLib** as well as the fake, and the two
  are asserted to store the same messages, take the same batches and resume the
  same way.

## Fixed

- **`IngestMessages` raised on a batch naming the same message twice.** Nothing
  is written until the batch is built, so the repository could not answer for an
  identifier the batch itself had already claimed; the second copy met the unique
  index. An error raised over exactly the case the pipeline promises to absorb.
  Identifiers are now tracked as the batch is assembled, so a repeat *within* a
  batch is skipped like a repeat across runs.
- **`SyncHistory` read a chat before checking the gateway owned it.** A call
  wired to the wrong account learned whether that account had a given chat, from
  the error message, before being refused. The ownership check now runs
  immediately after the account resolves and before any of its data is read --
  the ordering slice 5 already asserted for chat and contact synchronisation.

## Architecture Decisions

- **ADR-054** -- the cursor's identity, value and terminating conditions: keyed
  by chat, storing a message identifier, recording the horizon it reached, and
  which of `DOMAIN_MODEL.md` §5.22's fields are deferred and why.
- **ADR-050** gains an *As Implemented* section. Its decisions 1, 2, 5 and 6
  held exactly; decision 3 (`MessagesIngested`) is unimplemented because nothing
  subscribes, and decision 4 (the ingestion serialiser) because there is still
  only one producer. Both are recorded rather than quietly skipped.

## Scope note

Fourteen source and test files were created or modified, plus one migration and
one configuration file -- within the twenty-file limit. `IngestMessages` gained
one method, `ingest_within`, so that a backfill batch and the cursor accounting
for it can share a transaction; its existing behaviour is unchanged.

### Milestone 2.7 -- Chat and Contact Synchronisation

Slice 5 of `TELEGRAM_ARCHITECTURE.md`: the first code that reads Telegram and
writes the database.

**Two use cases, not one engine**

- `SyncChats` reads the chat list; `SyncContacts` reads the address book. They
  are two because the populations are two: the chat list holds people this
  account never saved, and the address book holds people it never messaged.
  Neither set contains the other, so neither can be derived from the other.
- `SyncReport` carries counts and `SyncProblem` entries. A problem does not
  always cost an item -- a handle this application cannot store leaves the
  person recorded without one -- so problems are counted separately from
  skipped items. Nothing is dropped quietly, and no problem carries a name or
  any message content (`SECURITY.md` section 9).
- There is no `SyncEngine`, no scheduler and no retry policy. What the two use
  cases share is one contact-upsert function, and an abstraction over two cases
  would be the framework this slice was told not to build.

**What synchronisation may and may not do (ADR-053)**

- **Additive.** Nothing is ever deleted. A chat that has disappeared from
  Telegram is still the operator's history.
- **It never overwrites an operator's decision.** `sync_enabled` and
  `ai_processing_mode` are chosen when a chat is first discovered and never
  revisited; a contact the operator deleted stays deleted, and its fields are
  not refreshed. A run that silently re-enabled AI processing on a chat somebody
  had disabled it on would be a privacy defect, not a bug.
- **A repeat run over unchanged data writes nothing**, so `updated_at` still
  means "when this last changed" rather than "when we last looked".
- **One transaction per item.** For a private chat the item is the pair
  `(Contact, Chat)`, because ADR-043's composite key means a private chat cannot
  exist without the contact it names. An interrupted run leaves complete records
  and no partial ones -- verified against a real SQLite database, because the
  in-memory repositories write through and cannot show a rollback.
- **A transport failure ends the run; an item failure does not.** If Telegram or
  the database is unreachable, the next item meets the same wall. One chat
  Telegram describes badly must not cost the operator the other two hundred,
  which is the judgement the adapter already makes about a chat that vanishes
  mid-listing.

**The operator's own identity (ADR-052)**

- `DOMAIN_MODEL.md` section 5.4's invariant -- "a Contact cannot be its own
  Account's operator identity" -- has been documented and unenforced since
  Milestone 1.3. It is enforced now, by a domain service called from every write
  path that can create a contact.
- The identifier it compares against needed no work: it has been
  `Account.telegram_user_id` since Milestone 1.2. `ROADMAP.md` and
  `TELEGRAM_ARCHITECTURE.md` both said authentication would have to supply it
  through `getMe`; both were wrong, and both now say so.
- **Telegram's Saved Messages is why the rule is unavoidable rather than merely
  correct.** It arrives as a private chat whose counterpart is the operator, and
  every real account has one -- so a synchronisation that did not recognise it
  would try to create the forbidden contact on its first run against every
  account. It is stored as `ChatType.SAVED`, which the domain model already had
  and nothing had yet reached.

**The gateway grew again (ADR-051)**

- `get_contact` and `list_contacts`. `get_contact` exists because a chat carries
  its counterpart's *name* but not their handle, and `Contact.username` needs
  one. `list_contacts` is two TDLib calls, for the same reason `list_chats` is
  three: `getContacts` returns identifiers, and `getUser` resolves each.

**Configuration**

- `telegram.sync_chat_types`, default `[private]`. Every kind of chat is
  *recorded* either way, so the operator can see a group and switch
  synchronisation on; the setting decides only the initial `sync_enabled`.

**Commands**

- `tgassist sync chats` and `tgassist sync contacts`, in a `sync` group distinct
  from `telegram` -- which reads and stores nothing. Writing is the distinction
  a user most needs to see before running something against their own account.

**Tests -- 113 added, suite at 2110**

- 80 covering first sync, repeat sync, changed names and handles, Saved
  Messages, sync scope, deleted and archived contacts, two accounts knowing the
  same person, cross-account refusal, Telegram unavailable, a refused row, a
  broken database, and four against a real SQLite file for the transaction
  boundary.
- The gateway contract suite is now **124 tests over both implementations**.

## Fixed

- **`TelegramChatInfo` and `TelegramMessage` refused negative identifiers**, and
  Telegram numbers every group and channel below zero. `tgassist telegram chats`
  would therefore have failed against any real account with a single group. The
  `Chat` entity and the schema had the rule right -- `telegram_chat_id <> 0`,
  deliberately not `> 0` -- and the DTOs added in slice 4 did not; all three now
  share `require_nonzero_chat_identifier`. Slice 4's tests missed it by using
  positive identifiers for groups, which Telegram never issues.

## Changed

- `AuthenticateAccount` and both synchronisation use cases now share one
  gateway-ownership check, `require_gateway_account`. Two copies of an ownership
  rule is one copy too many.
- `resolve_account` gained a sibling, `require_account`, returning the whole
  Account for callers that need something it knows. Same query either way.

## Architecture Decisions

- **ADR-052** -- the operator's Telegram identity is the Account's, and the
  invariant is enforced in a domain service. Not in the schema: SQLite's `CHECK`
  cannot reference another table, and a trigger would be a second home for a
  rule the application already states.
- **ADR-053** -- synchronisation is additive, per-item transactional, and never
  overrules the operator.

## Scope note

Nineteen source and test files were created or modified, plus one configuration
file -- within the twenty-file limit. No migration was needed: every column
synchronisation writes already existed, which is what "prove there is a current
consumer" is meant to produce.

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
