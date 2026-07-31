"""Extracting candidate memories from a conversation.

The first complete AI feature, and the one every later feature is shaped by.
Its job is to run four separate things in order without letting any of them
learn about the others:

1. **A prompt** decides what is asked. It is a versioned file, not a string in
   this module (ADR-008).
2. **``ExecuteAiTask``** decides how the model is reached: the privacy gate, the
   timeout, the token accounting, the audit record (ADR-057).
3. **The validator** decides whether the answer is the right *shape*.
4. **This module** decides whether it is worth *acting on*, and turns what
   survives into proposals.

Each of those can be replaced without touching the others, and the seam that
matters most is the last one: shape and worth are different questions, they
change for different reasons, and a validator that knew about confidence
thresholds would need editing every time a feature had a different one.

Nothing is remembered
---------------------

Every fact that comes out of this lands in a review queue. Nothing is written to
memory, because nothing here is believed (ADR-019, ADR-058). The model's job is
to notice; the person's job is to decide.

What the model is not allowed to decide
---------------------------------------

The schema gives it four fields: category, value, confidence, evidence. The
identifier, the timestamp, which conversation, which AI call, which prompt
version and the status all come from here. A model that could set a status could
approve itself, and one that could name an identifier could overwrite a
proposal somebody had already read.

Three filters after validation
------------------------------

A validated answer is still not trustworthy. Three rules run before anything is
stored, and all three are *deterministic* -- given the same answer they discard
the same things:

* **Ungrounded evidence.** The quotation has to appear in the text the model was
  shown. This is the cheapest anti-hallucination check available and the one
  that catches the failure that matters: a fluent, plausible fact about somebody
  that nobody ever said. A model cannot quote what nobody said.
* **Low confidence.** Below the configured threshold, a proposal costs more
  attention than it is worth. The confidence is self-reported and poorly
  calibrated (``AI_MODELS.md`` section 15), so it is used as a coarse filter and
  nothing more is claimed of it.
* **Already proposed.** Re-running extraction over a conversation must cost
  nothing and change nothing. Duplicates are dropped against what is stored --
  *including rejected proposals*, so a fact the user has already declined is not
  offered again -- and against the batch itself, because a model asked for
  distinct facts sometimes returns two.

The transaction does not span the model call
--------------------------------------------

Reading happens in one transaction, the model call happens in none, and writing
happens in another. This application permits one transaction at a time
(ADR-034), and a model call takes seconds: holding the lock across it would stop
everything else in the process, including the live update loop.

The cost is that the world can change between the read and the write. What that
can produce is a duplicate proposal, which the unique index refuses -- so the
worst case is a proposal that is not stored, not a proposal that is wrong.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.application.use_cases.ai import StructuredAiTask
from tgassist.domain.errors import (
    ConstraintViolationError,
    RecordNotFoundError,
    SchemaViolationError,
)
from tgassist.domain.events import MemoryProposalsCreated
from tgassist.domain.model.ai import PromptVersion
from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ConversationId,
    MemoryProposalId,
)
from tgassist.domain.model.memory import (
    Confidence,
    Evidence,
    MemoryCategory,
    MemoryProposal,
)
from tgassist.domain.model.message import Message
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.conversation_repository import ConversationRepository
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.memory_proposal_repository import MemoryProposalRepository
from tgassist.domain.ports.message_repository import MessageRepository
from tgassist.domain.ports.prompt_registry import PromptRegistry
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory

#: Which prompt asks for memories. Named here because this use case is what
#: knows which task it is performing; the prompt's *text* is not.
EXTRACT_PROMPT: Final = "memory_extract"

#: The prompt that carries the standing rules -- including the one that says
#: content inside the delimiters is data rather than instructions.
SYSTEM_PROMPT: Final = "system"

#: What the recorded call calls itself. Read by ``tgassist ai list``, so it
#: says what was being attempted rather than which module attempted it.
TASK_KIND: Final = "extract_memories"

#: Shown when a conversation has no proposals yet. Never an empty string: a
#: prompt whose section silently vanishes reads as though the section did not
#: exist, and the model has no way to tell the difference.
NOTHING_PROPOSED: Final = "(nothing has been proposed for this conversation yet)"


@dataclass(frozen=True, slots=True)
class ExtractionPolicy:
    """What this application does with what a model returns.

    Policy rather than domain rule: every value here is a judgement that a user
    or a later milestone may reasonably change, and none of them makes a
    proposal invalid -- only unworthy of somebody's attention.

    Attributes:
        min_confidence: Below this, a proposal is discarded rather than queued.
        max_proposals: The most one conversation may produce in one run. A
            model that returns thirty facts about one exchange has misunderstood
            the task, and a review queue is only useful while it is short.
        message_limit: How many of a conversation's messages to show the model,
            counting from the end. A bound on the request, and therefore on its
            cost.
        max_message_chars: How much of any one message to show. Bounds the
            payload space available to an injection attempt (``SECURITY.md``
            section 12).
    """

    min_confidence: float = 0.6
    max_proposals: int = 10
    message_limit: int = 100
    max_message_chars: int = 2000


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    """What one extraction run did.

    Every discarded candidate is counted rather than silently dropped. A run
    that returns nothing and a run that discarded eight ungrounded claims are
    very different events, and a report that could not tell them apart would
    make a broken prompt look like a quiet conversation.

    Attributes:
        conversation_id: What was read.
        ai_call_id: The call that produced the answer, and the proposals'
            provenance. The *second* call when a repair was needed, because that
            is the one whose answer was used.
        proposed: What was stored.
        returned: How many candidates the model offered.
        ungrounded: Discarded because the quotation did not appear in the
            conversation.
        low_confidence: Discarded for falling below the threshold.
        duplicates: Discarded because the same fact was already proposed, or was
            proposed twice in one answer.
        over_cap: Discarded because the run had already reached its cap.
        repaired: Whether the first answer failed validation and a repair was
            needed. Worth surfacing: a prompt that needs repairing often is a
            prompt that needs rewriting.
    """

    conversation_id: ConversationId
    ai_call_id: AiCallId
    proposed: tuple[MemoryProposal, ...] = ()
    returned: int = 0
    ungrounded: int = 0
    low_confidence: int = 0
    duplicates: int = 0
    over_cap: int = 0
    repaired: bool = False

    @property
    def stored(self) -> int:
        """How many proposals were written."""
        return len(self.proposed)

    @property
    def discarded(self) -> int:
        """How many candidates the model offered that were not stored."""
        return self.ungrounded + self.low_confidence + self.duplicates + self.over_cap


@dataclass(slots=True)
class _Candidate:
    """One entry from a validated answer, before it becomes a proposal."""

    category: MemoryCategory
    value: str
    confidence: Confidence
    evidence: Evidence


@dataclass(slots=True)
class _Tally:
    """What was discarded, and why."""

    returned: int = 0
    ungrounded: int = 0
    low_confidence: int = 0
    duplicates: int = 0
    over_cap: int = 0
    kept: list[_Candidate] = field(default_factory=list)


class ExtractMemories:
    """Turns one conversation into candidate facts awaiting a decision."""

    __slots__ = (
        "_accounts",
        "_clock",
        "_conversations",
        "_events",
        "_ids",
        "_messages",
        "_policy",
        "_prompts",
        "_proposals",
        "_task",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        proposals: ScopedRepositoryFactory[MemoryProposalRepository],
        conversations: ScopedRepositoryFactory[ConversationRepository],
        messages: ScopedRepositoryFactory[MessageRepository],
        accounts: RepositoryFactory[AccountRepository],
        task: StructuredAiTask,
        prompts: PromptRegistry,
        clock: Clock,
        ids: IdGenerator,
        policy: ExtractionPolicy | None = None,
        events: EventBus | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory. Two transactions per run, with
                the model call between them and inside neither.
            proposals: Memory proposal repository factory, scoped per account.
            conversations: Conversation repository factory, scoped per account.
            messages: Message repository factory, scoped per account.
            accounts: Account repository factory.
            task: The structured AI boundary. Not a provider, and not
                ``ExecuteAiTask`` directly: everything about *how* a model is
                reached belongs there, and the one-repair rule belongs to
                ``StructuredAiTask`` -- taking either lower thing here would
                mean reimplementing something (ADR-061).
            prompts: Where the versioned prompt assets come from.
            clock: Time source, for the timestamp on each proposal.
            ids: Local identifier generator. The model never supplies one.
            policy: What to do with what the model returns.
            events: Where ``MemoryProposalsCreated`` is published, after the
                writing transaction commits.
        """
        self._unit_of_work = unit_of_work
        self._proposals = proposals
        self._conversations = conversations
        self._messages = messages
        self._accounts = accounts
        self._task = task
        self._prompts = prompts
        self._clock = clock
        self._ids = ids
        self._policy = policy if policy is not None else ExtractionPolicy()
        self._events = events

    async def execute(
        self, conversation_id: int, *, account_id: AccountId | None = None
    ) -> ExtractionReport:
        """Extract candidate memories from one conversation.

        Args:
            conversation_id: The conversation to read.
            account_id: Account to operate on. ``None`` selects the active one.

        Returns:
            What the run proposed, and what it discarded.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such conversation.
            AiForbiddenError: If the chat's ``ai_processing_mode`` does not
                permit the configured model. Raised by ``ExecuteAiTask``, which
                records the refusal before raising.
            AiError: If the provider failed. Also recorded before raising.
            SchemaViolationError: If the model's answer did not satisfy the
                schema, and the one repair attempt did not either.
        """
        resolved, conversation, messages, existing = await self._read(conversation_id, account_id)
        transcript = _transcript(messages, self._policy)

        prompt = self._prompts.get(EXTRACT_PROMPT)
        schema = self._prompts.schema_for(EXTRACT_PROMPT)
        if schema is None:  # pragma: no cover - the registry validates this
            msg = f"Prompt {EXTRACT_PROMPT!r} has no output schema"
            raise SchemaViolationError(msg, user_message="That prompt cannot be validated.")

        rendered = prompt.render(
            {
                "categories": _categories(),
                "already_proposed": _already_proposed(existing),
                "transcript": transcript,
            }
        )
        instructions = self._prompts.get(SYSTEM_PROMPT).render({}).text

        answer = await self._task.execute(
            content=rendered.text,
            instructions=instructions,
            prompt=rendered.version,
            task_kind=TASK_KIND,
            schema=schema,
            chat_id=int(conversation.chat_id),
            account_id=resolved,
        )

        tally = self._sift(answer.payload, transcript, existing)
        proposals = self._build(tally.kept, conversation, answer.call_id, rendered.version)
        stored = await self._store(resolved, proposals)

        report = ExtractionReport(
            conversation_id=conversation.id,
            ai_call_id=answer.call_id,
            proposed=stored,
            returned=tally.returned,
            ungrounded=tally.ungrounded,
            low_confidence=tally.low_confidence,
            duplicates=tally.duplicates + (len(proposals) - len(stored)),
            over_cap=tally.over_cap,
            repaired=answer.repaired,
        )
        await self._announce(resolved, report, conversation)
        return report

    # -- Reading -----------------------------------------------------------

    async def _read(
        self, conversation_id: int, account_id: AccountId | None
    ) -> tuple[AccountId, Conversation, tuple[Message, ...], tuple[MemoryProposal, ...]]:
        """Load everything the prompt needs, in one transaction."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            conversation = await self._conversations(uow, resolved).get(
                ConversationId(conversation_id)
            )
            if conversation is None:
                msg = f"No conversation {conversation_id} in account {int(resolved)}"
                raise RecordNotFoundError(
                    msg,
                    user_message="That conversation was not found.",
                    context={"conversation_id": conversation_id},
                )

            found = await self._messages(uow, resolved).list_since(
                conversation.chat_id, conversation.started_at
            )
            messages = tuple(m for m in found if conversation.contains(m.sent_at))
            existing = await self._proposals(uow, resolved).list_for_conversation(conversation.id)
            return resolved, conversation, messages, existing

    # -- Asking ------------------------------------------------------------

    # -- Sifting -----------------------------------------------------------

    def _sift(
        self,
        payload: Mapping[str, Any],
        transcript: str,
        existing: Sequence[MemoryProposal],
    ) -> _Tally:
        """Apply the three filters, counting what each discards.

        The order matters only for the counts: a candidate that is both
        ungrounded and a duplicate is counted once, as ungrounded, because that
        is the more serious of the two things to know about a prompt.
        """
        haystack = _comparable(transcript)
        seen = {(proposal.category, _comparable(proposal.value)) for proposal in existing}
        tally = _Tally()

        for entry in payload.get("proposals", ()):
            tally.returned += 1
            candidate = _candidate(entry)

            if _comparable(candidate.evidence.quote) not in haystack:
                tally.ungrounded += 1
                continue
            if not candidate.confidence.is_at_least(self._policy.min_confidence):
                tally.low_confidence += 1
                continue

            key = (candidate.category, _comparable(candidate.value))
            if key in seen:
                tally.duplicates += 1
                continue
            if len(tally.kept) >= self._policy.max_proposals:
                tally.over_cap += 1
                continue

            seen.add(key)
            tally.kept.append(candidate)
        return tally

    # -- Building and storing ----------------------------------------------

    def _build(
        self,
        candidates: Sequence[_Candidate],
        conversation: Conversation,
        call_id: AiCallId,
        prompt: PromptVersion,
    ) -> tuple[MemoryProposal, ...]:
        """Turn surviving candidates into proposals.

        Every field the model did not supply is supplied here, which is the
        whole of what "the model proposes, the application decides" means in
        code.
        """
        now = self._clock.now()
        return tuple(
            MemoryProposal.propose(
                proposal_id=MemoryProposalId(self._ids.new_id()),
                account_id=conversation.account_id,
                conversation_id=conversation.id,
                ai_call_id=call_id,
                category=candidate.category,
                value=candidate.value,
                confidence=candidate.confidence,
                evidence=candidate.evidence,
                prompt=prompt,
                now=now,
            )
            for candidate in candidates
        )

    async def _store(
        self, account_id: AccountId, proposals: Sequence[MemoryProposal]
    ) -> tuple[MemoryProposal, ...]:
        """Write the proposals, in one transaction.

        All of them or none: a partial queue would be a queue nobody could tell
        was partial. A proposal the unique index refuses is skipped rather than
        failing the run -- it means the same fact arrived twice, which is a
        duplicate rather than an error, and the run's whole purpose is to add
        what is new.
        """
        if not proposals:
            return ()

        stored: list[MemoryProposal] = []
        async with self._unit_of_work() as uow:
            repository = self._proposals(uow, account_id)
            for proposal in proposals:
                try:
                    await repository.add(proposal)
                except ConstraintViolationError:
                    continue
                stored.append(proposal)
            if stored:
                await uow.commit()
        return tuple(stored)

    async def _announce(
        self, account_id: AccountId, report: ExtractionReport, conversation: Conversation
    ) -> None:
        """Publish what was stored, after the transaction that stored it.

        After the commit, never inside it: a handler observing proposals that
        then rolled back would be acting on facts that were never offered.
        Nothing subscribes yet -- the notification that tells a user their queue
        has grown is Milestone 10 -- and the event is published anyway because
        the alternative is a handler that has to poll.
        """
        if not report.stored or self._events is None:
            return
        await self._events.publish(
            MemoryProposalsCreated(
                account_id=int(account_id),
                conversation_id=int(conversation.id),
                chat_id=int(conversation.chat_id),
                count=report.stored,
                ai_call_id=int(report.ai_call_id),
            )
        )


class GetMemoryProposal:
    """Looks one proposal up."""

    __slots__ = ("_accounts", "_proposals", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        proposals: ScopedRepositoryFactory[MemoryProposalRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._proposals = proposals
        self._accounts = accounts

    async def execute(
        self, proposal_id: int, *, account_id: AccountId | None = None
    ) -> MemoryProposal | None:
        """Return one proposal, or ``None`` if absent."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._proposals(uow, resolved).get(MemoryProposalId(proposal_id))


class ListMemoryProposals:
    """Returns a page of an account's proposals, newest first."""

    __slots__ = ("_accounts", "_proposals", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        proposals: ScopedRepositoryFactory[MemoryProposalRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._proposals = proposals
        self._accounts = accounts

    async def execute(
        self, request: PageRequest | None = None, *, account_id: AccountId | None = None
    ) -> Page[MemoryProposal]:
        """Return one page of proposals."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._proposals(uow, resolved).list_recent(request or PageRequest())


# -- Preparing what the model sees -----------------------------------------


def _transcript(messages: Sequence[Message], policy: ExtractionPolicy) -> str:
    """Render a conversation as text.

    The last ``message_limit`` messages, each truncated, each labelled with who
    said it and when. Truncation is marked rather than silent: a model shown a
    sentence that stops mid-word should be able to tell that it was cut.

    Attachments with no text appear as their kind. A conversation of six photos
    is a conversation about which there is nothing to extract, and the model
    should see that rather than see nothing.
    """
    recent = messages[-policy.message_limit :]
    lines = []
    for message in recent:
        who = "operator" if message.is_outgoing else "contact"
        body = message.text if message.text is not None else f"({message.message_type.value})"
        if len(body) > policy.max_message_chars:
            body = body[: policy.max_message_chars] + " [truncated]"
        lines.append(f"[{message.sent_at:%Y-%m-%d %H:%M}] {who}: {body}")
    return "\n".join(lines)


def _categories() -> str:
    """List the categories a proposal may use, for the prompt.

    Generated from the enum rather than written into the prompt file, so that a
    category added in code cannot be one the model was never told about.
    """
    return "\n".join(f"- `{category.value}`" for category in MemoryCategory)


def _already_proposed(existing: Sequence[MemoryProposal]) -> str:
    """List what has already been proposed for this conversation.

    Including the rejected ones. The model is asked not to repeat them, which is
    a courtesy rather than a guarantee -- the deterministic duplicate check
    afterwards is the guarantee.
    """
    if not existing:
        return NOTHING_PROPOSED
    return "\n".join(f"- {proposal.category.value}: {proposal.value}" for proposal in existing)


# -- Reading what the model said -------------------------------------------


def _candidate(entry: Mapping[str, Any]) -> _Candidate:
    """Build a candidate from one validated entry.

    Safe to index without checking, and that is not an assumption: the schema
    declared these fields required, and nothing reaches here without having
    satisfied it.
    """
    return _Candidate(
        category=MemoryCategory(entry["category"]),
        value=str(entry["value"]).strip(),
        confidence=Confidence(float(entry["confidence"])),
        evidence=Evidence(str(entry["evidence"]).strip()),
    )


def _comparable(text: str) -> str:
    """Normalise text for comparison.

    Whitespace collapsed and case folded, because a model quoting a message
    reproduces its words reliably and its spacing and capitalisation less so.
    Anything more forgiving -- fuzzy matching, substring scoring -- would start
    accepting quotations that were nearly right, and a nearly-right quotation is
    exactly what an invented fact produces.
    """
    return " ".join(text.split()).casefold()


__all__ = [
    "EXTRACT_PROMPT",
    "NOTHING_PROPOSED",
    "SYSTEM_PROMPT",
    "TASK_KIND",
    "ExtractMemories",
    "ExtractionPolicy",
    "ExtractionReport",
    "GetMemoryProposal",
    "ListMemoryProposals",
]
