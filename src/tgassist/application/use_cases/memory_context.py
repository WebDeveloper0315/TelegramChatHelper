"""Assembling what a model should be told about somebody.

The step between "the application knows things" and "the application uses
them". It reads one contact's approved memories, ranks them with the pure
selector, spends a token budget, and reports exactly what it chose and what it
left out.

**No model is called here.** That is the point: retrieval happens before
generation, it is deterministic, and it can be inspected on its own -- so the
first time a memory reaches a prompt, the selection that put it there has
already been read by a person (ADR-060).

Two use cases, one difference
-----------------------------

:class:`BuildMemoryContext` selects **and records the retrieval**. It is what a
prompt assembler calls, and the counters it writes are the evidence that will
eventually justify or refute this ranking.

:class:`GetMemoryContext` selects and records nothing. It is what
``tgassist memory context`` calls, because looking at what *would* be sent is
not using it, and an inspection that inflated the counters would corrupt the
measurement it exists to expose.

Both return the same thing and share their whole implementation. The difference
is one boolean and it is worth two names: a caller choosing between
``build(record=False)`` and ``build(record=True)`` has to know which is which,
whereas ``Get`` and ``Build`` say so.

Contact scope
-------------

Retrieval never crosses contacts. A private chat retrieves that person's
memories; a chat with no single counterpart retrieves the memories that are
about *nobody in particular* -- the ones extracted from group conversations --
and nobody else's. The partition is strict in both directions and it is enforced
by the repository, so there is one place to get it wrong.

The transaction
---------------

One, and it covers the read, the accounting and nothing else. Selection happens
in memory between them, because it is pure and needs no transaction; the model
call that eventually consumes the context happens well after it, because a
transaction held across a model call would stop everything else in the process
(ADR-034).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.domain.errors import RecordNotFoundError
from tgassist.domain.events import MemoriesRetrieved
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.identifiers import AccountId, ChatId, ContactId
from tgassist.domain.model.memory import Memory
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.memory_repository import MemoryRepository
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tgassist.domain.services.memory_selection import (
    MemorySelector,
    Omitted,
    Selection,
    SelectionRules,
    memory_tokens,
    ordering_key,
)

#: How many of a contact's memories to consider. A bound on the cost of building
#: a context, not on its content -- the budget decides that. Set high enough
#: that reaching it means something unusual, and reported when it does, because
#: a truncated candidate set means ranking never saw everything.
DEFAULT_MAX_CANDIDATES: Final = 500


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """What a model should be told about somebody, and how it was chosen.

    Attributes:
        chat_id: The conversation this was assembled for.
        contact_id: Who it is about, or ``None`` for a chat with no single
            counterpart.
        selection: What the selector chose, what it did not, and why.
        recorded: Whether this retrieval was counted against the memories it
            used. ``False`` for an inspection.
        truncated: Whether the candidate set hit its cap, so ranking saw only
            part of what is known. Reported rather than silent: a context that
            omitted something it never looked at is a different thing from one
            that ranked it last.
    """

    chat_id: ChatId
    contact_id: ContactId | None
    selection: Selection
    recorded: bool
    truncated: bool

    @property
    def memories(self) -> tuple[Memory, ...]:
        """Return the selected memories, in the order they should be presented."""
        return self.selection.selected

    @property
    def omitted(self) -> tuple[Omitted, ...]:
        """Return what was ranked and left out, in rank order."""
        return self.selection.omitted

    @property
    def tokens(self) -> int:
        """Return the estimated cost of the selected memories."""
        return self.selection.tokens

    @property
    def is_empty(self) -> bool:
        """Whether there is nothing to tell the model."""
        return self.selection.is_empty

    def why(self, memory: Memory) -> str:
        """Explain in one line why a memory placed where it did.

        Every ranking key, in the order they were applied. A selection nobody
        can read is one nobody can disagree with, and disagreeing with it is how
        the ranking gets better.
        """
        priority, *_ = ordering_key(memory)
        return (
            f"category {memory.category.value} (priority {priority}), "
            f"importance {memory.importance}, "
            f"confidence {memory.confidence}, "
            f"accepted {memory.created_at:%Y-%m-%d}, "
            f"{memory_tokens(memory)} tokens"
        )


class GetMemoryContext:
    """Assembles a context without recording that it was used.

    The inspection path. Reads exactly what :class:`BuildMemoryContext` would
    choose and leaves the retrieval counters untouched, so looking does not
    change what is being looked at.
    """

    __slots__ = (
        "_accounts",
        "_candidates",
        "_chats",
        "_clock",
        "_events",
        "_memories",
        "_selector",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        memories: ScopedRepositoryFactory[MemoryRepository],
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        rules: SelectionRules | None = None,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        events: EventBus | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory. One transaction per context.
            memories: Memory repository factory, scoped per account.
            chats: Chat repository factory, to resolve who the chat is with.
            accounts: Account repository factory.
            clock: Time source, for the retrieval timestamp.
            rules: The token budget and the memory cap.
            max_candidates: How many memories to consider before ranking.
            events: Where ``MemoriesRetrieved`` is published, by the recording
                subclass only. This one never publishes: an inspection is not a
                retrieval.
        """
        self._unit_of_work = unit_of_work
        self._memories = memories
        self._chats = chats
        self._accounts = accounts
        self._clock = clock
        self._selector = MemorySelector(rules)
        self._candidates = max_candidates
        self._events = events

    @property
    def records_retrieval(self) -> bool:
        """Whether this use case counts what it selects."""
        return False

    async def execute(self, chat_id: int, *, account_id: AccountId | None = None) -> MemoryContext:
        """Assemble the context for one chat.

        Args:
            chat_id: The chat a reply is being prepared for.
            account_id: Account to operate on. ``None`` selects the active one.

        Returns:
            What was chosen, what was not, and why.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such chat.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            chat = await self._require_chat(uow, resolved, chat_id)
            repository = self._memories(uow, resolved)

            candidates = await repository.list_for_contact(chat.contact_id, limit=self._candidates)
            selection = self._selector.select(candidates)
            recorded = await self._account_for(repository, selection)
            if recorded:
                await uow.commit()

            context = MemoryContext(
                chat_id=chat.id,
                contact_id=chat.contact_id,
                selection=selection,
                recorded=recorded,
                truncated=len(candidates) >= self._candidates,
            )

        # After the commit, never inside it: a subscriber observing a retrieval
        # that then rolled back would be counting something that never happened.
        await self._announce(resolved, context)
        return context

    async def _announce(
        self,
        account_id: AccountId,  # noqa: ARG002 - the publishing subclass uses it
        context: MemoryContext,  # noqa: ARG002
    ) -> None:
        """Publish the retrieval, if this use case performs one.

        A no-op here. ``GetMemoryContext`` inspects rather than retrieves, and an
        inspection that announced a retrieval would make the events disagree
        with the counters (ADR-060).
        """
        return

    async def _account_for(
        self,
        repository: MemoryRepository,  # noqa: ARG002 - the recording subclass uses it
        selection: Selection,  # noqa: ARG002
    ) -> bool:
        """Record the retrieval, or do not. This one does not."""
        return False

    async def _require_chat(self, uow: UnitOfWork, account_id: AccountId, chat_id: int) -> Chat:
        """Return the chat a context is being built for.

        Raises:
            RecordNotFoundError: If this account has no such chat.
        """
        chat = await self._chats(uow, account_id).get(ChatId(chat_id))
        if chat is None:
            msg = f"No chat {chat_id} in account {int(account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That chat was not found.",
                context={"chat_id": chat_id, "account_id": int(account_id)},
            )
        return chat


class BuildMemoryContext(GetMemoryContext):
    """Assembles a context and records that its memories were used.

    The production path, and the only thing in the application that writes
    ``retrieval_count`` and ``last_retrieved_at``. The accounting happens in the
    **same transaction as the read**, so a context and the record of it existing
    cannot disagree -- and it happens before the model call rather than after,
    because the selection is what was used whether or not the model then
    succeeded (ADR-060).
    """

    __slots__ = ()

    @property
    def records_retrieval(self) -> bool:
        """Whether this use case counts what it selects."""
        return True

    async def _account_for(self, repository: MemoryRepository, selection: Selection) -> bool:
        """Count every selected memory, in one statement."""
        if selection.is_empty:
            return False
        await repository.mark_retrieved(
            [memory.id for memory in selection.selected], self._clock.now()
        )
        return True

    async def _announce(self, account_id: AccountId, context: MemoryContext) -> None:
        """Publish what was retrieved, after the transaction that recorded it.

        Only when something was: an empty context records nothing and announces
        nothing, because "no memories were used" is not a fact worth waking a
        subscriber for.
        """
        if self._events is None or not context.recorded:
            return
        await self._events.publish(
            MemoriesRetrieved(
                account_id=int(account_id),
                chat_id=int(context.chat_id),
                contact_id=int(context.contact_id) if context.contact_id is not None else None,
                count=len(context.memories),
                candidates=context.selection.candidates,
                tokens=context.tokens,
            )
        )


__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "BuildMemoryContext",
    "GetMemoryContext",
    "MemoryContext",
]
