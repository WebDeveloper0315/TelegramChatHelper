"""Suggesting a reply: the first thing that consumes what the application knows.

Everything built since Slice 9a arrives here. A conversation is read, memories
are retrieved and ranked, the two are assembled into one prompt, a model answers
in a fixed shape, and what comes back is checked before anybody sees it.

**Nothing is sent.** This produces a draft for a person to read and decide
about. There is no code path from here to Telegram (ADR-061, ADR-062).

**Everything produced is stored.** A suggestion that existed only as a return
value could not be reviewed tomorrow, so generation persists what it makes and
publishes ``SuggestionsCreated``. That is what makes "observable before any
action" a property of the system rather than of whoever happened to be watching
the terminal (ADR-062).

The pipeline
------------

1. Resolve the chat, and through it the person.
2. Retrieve memories -- ranked and budgeted by Slice 9d, and **recorded**,
   because this is a real use rather than an inspection.
3. Read the recent messages.
4. Assemble: a fixed order, a token budget, and a trim order that never removes
   the last message (ADR-061).
5. Render the shipped prompt around that context.
6. Execute through ``StructuredAiTask``: the privacy gate, the timeout, the
   audit record and exactly one repair, none of which is reimplemented here.
7. Check the attribution the model returned against what was actually supplied.

Attribution, and why it is checked
----------------------------------

The model is asked which memories it used, and answers with their keys. Every
key is checked against the keys that were supplied. A key that was not supplied
is a **fabricated attribution** -- the model claiming to have used something it
was never given -- and it is discarded and counted rather than shown.

That check is cheap, deterministic, and it is the same idea as the evidence
grounding in extraction (ADR-058): a model cannot cite what it was not given.
What it catches is narrow but real -- a suggestion that claims grounding it does
not have is worse than one that claims none, because the first invites trust.

What it does *not* prove is that a memory reported as used actually influenced
the text. Nothing available here can prove that. What attribution buys is a
reader who can ask "why did it say that?" and be shown the exact facts that were
in front of the model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.application.use_cases.ai import StructuredAiTask
from tgassist.application.use_cases.memory_context import GetMemoryContext
from tgassist.domain.errors import RecordNotFoundError, SchemaViolationError
from tgassist.domain.events import SuggestionsCreated
from tgassist.domain.model.ai import PromptVersion
from tgassist.domain.model.identifiers import AccountId, AiCallId, ChatId, SuggestionId
from tgassist.domain.model.memory import Confidence, Memory
from tgassist.domain.model.message import Message
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.suggestion import ProposalType, Suggestion
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.message_repository import MessageRepository
from tgassist.domain.ports.prompt_registry import PromptRegistry
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.suggestion_repository import SuggestionRepository
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory
from tgassist.domain.services.context_assembly import (
    AssemblyRules,
    ContextAssembler,
    PromptContext,
)
from tgassist.domain.services.memory_selection import Selection

#: Which prompt asks for a reply.
SUGGESTION_PROMPT: Final = "chat_suggestion"

#: The prompt carrying the standing rules, including the one that says content
#: inside the delimiters is data rather than instructions.
SYSTEM_PROMPT: Final = "system"

#: What the recorded call calls itself. Read by ``tgassist ai list``.
TASK_KIND: Final = "suggest_reply"


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    """A prompt and everything that went into deciding it.

    Attributes:
        chat_id: What it is for.
        context: What the model will be told, and what the budget removed.
        retrieval: The memory selection this was built from, including what
            retrieval itself omitted. Kept separate from the assembly's own
            trimming: a memory the retriever never selected and one the
            assembler dropped were excluded by different budgets, and only a
            report that distinguishes them says which is too small.
        text: The rendered prompt. What actually goes on the wire.
        instructions: The rendered system prompt.
        version: Which prompt at which revision produced it. Recorded on the
            call, so a suggestion can be traced to the exact wording that
            produced it (ADR-057).
    """

    chat_id: ChatId
    context: PromptContext
    retrieval: Selection
    text: str
    instructions: str
    version: PromptVersion

    @property
    def memories(self) -> tuple[Memory, ...]:
        """Return the memories the model will actually see."""
        return self.context.memories


@dataclass(frozen=True, slots=True)
class GeneratedSuggestion:
    """A draft reply, and the account of how it was produced.

    Attributes:
        prompt: Everything the model was told.
        text: What it suggested. A draft, never sent by anything.
        confidence: What it reported about its own answer. Self-reported and
            poorly calibrated (``AI_MODELS.md`` section 15).
        used_memories: The supplied memories the model said it used.
        fabricated_keys: Keys it claimed to use that were never supplied. Should
            always be empty; when it is not, the suggestion is claiming
            grounding it does not have.
        ai_call_id: The recorded call that produced it.
        repaired: Whether the first answer failed validation.
        record: The stored draft, awaiting review. ``None`` only when this use
            case was built without a repository, which no production path does
            -- the option exists so a test can exercise generation alone.
    """

    prompt: AssembledPrompt
    text: str
    confidence: Confidence
    used_memories: tuple[Memory, ...]
    fabricated_keys: tuple[str, ...]
    ai_call_id: AiCallId
    repaired: bool
    record: Suggestion | None = None

    @property
    def suggestion_id(self) -> SuggestionId | None:
        """Return the identifier the stored draft was given, if it was stored."""
        return self.record.id if self.record is not None else None

    @property
    def is_grounded(self) -> bool:
        """Whether every memory the model claimed to use was really supplied."""
        return not self.fabricated_keys


class BuildPromptContext:
    """Assembles the prompt for one chat, without asking anything.

    The whole deterministic half of a suggestion, separated so it can be read on
    its own -- and so a prompt can be inspected before it is paid for.
    """

    __slots__ = (
        "_accounts",
        "_assembler",
        "_chats",
        "_memory_context",
        "_messages",
        "_prompts",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        memory_context: GetMemoryContext,
        messages: ScopedRepositoryFactory[MessageRepository],
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
        prompts: PromptRegistry,
        rules: AssemblyRules | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory, for reading the messages.
            memory_context: Retrieval. ``BuildMemoryContext`` when a suggestion
                will really be generated, ``GetMemoryContext`` when a prompt is
                only being looked at -- the difference is whether the retrieval
                is counted (ADR-060).
            messages: Message repository factory, scoped per account.
            chats: Chat repository factory.
            accounts: Account repository factory.
            prompts: Where the versioned prompt assets come from.
            rules: The assembly budget and the trim order's bounds.
        """
        self._unit_of_work = unit_of_work
        self._memory_context = memory_context
        self._messages = messages
        self._chats = chats
        self._accounts = accounts
        self._prompts = prompts
        self._assembler = ContextAssembler(rules)

    async def execute(
        self, chat_id: int, *, account_id: AccountId | None = None
    ) -> AssembledPrompt:
        """Assemble the prompt for one chat.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such chat.
        """
        retrieved = await self._memory_context.execute(chat_id, account_id=account_id)
        messages = await self._recent(retrieved.chat_id, account_id)

        context = self._assembler.assemble(retrieved.memories, messages)
        prompt = self._prompts.get(SUGGESTION_PROMPT)
        rendered = prompt.render(
            {
                "memories": context.render_memories(),
                "conversation": context.render_conversation(),
            }
        )
        return AssembledPrompt(
            chat_id=retrieved.chat_id,
            context=context,
            retrieval=retrieved.selection,
            text=rendered.text,
            instructions=self._prompts.get(SYSTEM_PROMPT).render({}).text,
            version=rendered.version,
        )

    async def _recent(self, chat_id: ChatId, account_id: AccountId | None) -> tuple[Message, ...]:
        """Read the recent messages, oldest first.

        A separate transaction from retrieval's, because retrieval owns its own
        and this application permits one at a time (ADR-034). Nothing spans a
        model call, which happens after both.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            page = await self._messages(uow, resolved).list_by_chat(
                chat_id, PageRequest(limit=self._assembler.rules.message_limit)
            )
            # The repository returns newest first; a conversation is read
            # oldest first, and the assembler trims from the old end.
            return tuple(reversed(page.items))


class GenerateConversationSuggestion:
    """Produces one draft reply for a chat.

    The first feature that consumes everything: memories, retrieval, assembly,
    the prompt registry, the AI boundary and structured validation. It produces
    text and nothing else -- no message is sent, no state about the conversation
    changes, and the only thing written is the audit record ``ExecuteAiTask``
    writes for any call.
    """

    __slots__ = (
        "_accounts",
        "_builder",
        "_clock",
        "_events",
        "_ids",
        "_prompts",
        "_suggestions",
        "_task",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        builder: BuildPromptContext,
        task: StructuredAiTask,
        prompts: PromptRegistry,
        unit_of_work: UnitOfWorkFactory | None = None,
        suggestions: ScopedRepositoryFactory[SuggestionRepository] | None = None,
        accounts: RepositoryFactory[AccountRepository] | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        events: EventBus | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            builder: The deterministic half -- retrieval and assembly.
            task: The structured AI boundary: gate, timeout, audit record and
                exactly one repair, none of it reimplemented here.
            prompts: Where the output schema comes from.
            unit_of_work: Transaction factory for storing the draft.
            suggestions: Suggestion repository factory, scoped per account.
            accounts: Account repository factory.
            clock: Time source, for the stored draft's timestamp.
            ids: Local identifier generator. The model never supplies one.
            events: Where ``SuggestionsCreated`` is published, after the commit.

        The storage collaborators are optional **only** so a test can exercise
        generation without a queue. Every production path supplies them: a
        suggestion nobody can find tomorrow is not reviewable, which is the
        whole of ADR-062.
        """
        self._builder = builder
        self._task = task
        self._prompts = prompts
        self._unit_of_work = unit_of_work
        self._suggestions = suggestions
        self._accounts = accounts
        self._clock = clock
        self._ids = ids
        self._events = events

    async def execute(
        self, chat_id: int, *, account_id: AccountId | None = None
    ) -> GeneratedSuggestion:
        """Generate one suggestion.

        Args:
            chat_id: The chat to suggest a reply for.
            account_id: Account to operate on. ``None`` selects the active one.

        Returns:
            The draft, and everything that produced it.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such chat.
            DomainValidationError: If the chat has no messages to reply to. A
                suggestion for a conversation nobody has had is a guess, and
                this refuses rather than making one.
            AiForbiddenError: If the chat's ``ai_processing_mode`` does not
                permit the configured model (ADR-024).
            SchemaViolationError: If the answer failed validation twice.
            AiError: If the provider failed.
        """
        assembled = await self._builder.execute(chat_id, account_id=account_id)
        if assembled.context.conversation.is_empty:
            msg = f"Chat {chat_id} has no messages to reply to"
            raise RecordNotFoundError(
                msg,
                user_message="There is nothing in that chat to reply to yet.",
                context={"chat_id": chat_id},
            )

        schema = self._prompts.schema_for(SUGGESTION_PROMPT)
        if schema is None:  # pragma: no cover - the registry validates this
            msg = f"Prompt {SUGGESTION_PROMPT!r} has no output schema"
            raise SchemaViolationError(msg, user_message="That prompt cannot be validated.")

        answer = await self._task.execute(
            content=assembled.text,
            instructions=assembled.instructions,
            prompt=assembled.version,
            task_kind=TASK_KIND,
            schema=schema,
            chat_id=int(assembled.chat_id),
            account_id=account_id,
        )

        used, fabricated = _attribute(answer.payload, assembled.memories)
        suggestion = GeneratedSuggestion(
            prompt=assembled,
            text=str(answer.payload["suggestion"]).strip(),
            confidence=Confidence(float(answer.payload["confidence"])),
            used_memories=used,
            fabricated_keys=fabricated,
            ai_call_id=answer.call_id,
            repaired=answer.repaired,
        )
        return replace(suggestion, record=await self._store(suggestion))

    async def _store(self, suggestion: GeneratedSuggestion) -> Suggestion | None:
        """Persist the draft so it can be reviewed later.

        One transaction, and it writes one row. The AI call was already recorded
        in its own transaction by ``ExecuteAiTask`` -- deliberately, so a
        generation that fails to store still shows what it cost (ADR-057).
        """
        if (
            self._unit_of_work is None
            or self._suggestions is None
            or self._accounts is None
            or self._clock is None
            or self._ids is None
        ):
            return None

        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), None)
            record = Suggestion.draft(
                suggestion_id=SuggestionId(self._ids.new_id()),
                account_id=resolved,
                chat_id=suggestion.prompt.chat_id,
                ai_call_id=suggestion.ai_call_id,
                proposal_type=ProposalType.REPLY_DRAFT,
                title=_title(suggestion),
                description=suggestion.text,
                payload=_payload(suggestion),
                now=self._clock.now(),
            )
            await self._suggestions(uow, resolved).add(record)
            await uow.commit()

        if self._events is not None:
            await self._events.publish(
                SuggestionsCreated(
                    account_id=int(resolved),
                    chat_id=int(record.chat_id),
                    suggestion_ids=(int(record.id),),
                    ai_call_id=int(record.ai_call_id),
                    proposal_type=record.proposal_type.value,
                )
            )
        return record


def _title(suggestion: GeneratedSuggestion) -> str:
    """Return the one-line summary a listing shows.

    The first line of the draft, shortened. Derived rather than asked of the
    model: a title is presentation, and asking for one would add a field the
    model could get wrong in a way nobody would notice.
    """
    first = suggestion.text.strip().splitlines()[0] if suggestion.text.strip() else "(empty)"
    collapsed = " ".join(first.split())
    if len(collapsed) <= _TITLE_LENGTH:
        return collapsed
    return collapsed[:_TITLE_LENGTH].rstrip() + "..."


def _payload(suggestion: GeneratedSuggestion) -> dict[str, Any]:
    """Return what a machine would need, and a person does not read.

    Everything an executor would want and nothing it would have to re-derive:
    the model's own confidence, which memories it cited, and which prompt asked.
    Read by nothing today (ADR-062).
    """
    return {
        "confidence": suggestion.confidence.value,
        "used_memory_keys": [memory.key.value for memory in suggestion.used_memories],
        "fabricated_memory_keys": list(suggestion.fabricated_keys),
        "prompt_version": str(suggestion.prompt.version),
        "repaired": suggestion.repaired,
        "context_tokens": suggestion.prompt.context.tokens,
    }


#: How long a derived title may be before it is shortened.
_TITLE_LENGTH: Final = 120


def _attribute(
    payload: Mapping[str, Any], supplied: Sequence[Memory]
) -> tuple[tuple[Memory, ...], tuple[str, ...]]:
    """Match the keys a model reported against the memories it was given.

    Safe to index without checking: the schema declared these fields required,
    and nothing reaches here without having satisfied it.

    Returns:
        The supplied memories it claimed to use, in the order they were
        supplied, and the keys it named that were never supplied.
    """
    claimed = [str(key) for key in payload["used_memory_keys"]]
    by_key = {memory.key.value: memory for memory in supplied}
    used = tuple(memory for memory in supplied if memory.key.value in claimed)
    fabricated = tuple(dict.fromkeys(key for key in claimed if key not in by_key))
    return used, fabricated


__all__ = [
    "SUGGESTION_PROMPT",
    "SYSTEM_PROMPT",
    "TASK_KIND",
    "AssembledPrompt",
    "BuildPromptContext",
    "GenerateConversationSuggestion",
    "GeneratedSuggestion",
]
