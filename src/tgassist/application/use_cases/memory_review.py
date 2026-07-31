"""Deciding about proposals, and what a decision creates.

The half of the lifecycle a person performs. Extraction
(``use_cases/memory.py``) produces candidates; this decides about them, and it
is the only route into long-term memory.

A decision is made once
-----------------------

Accepting and rejecting are terminal, and nothing here reopens either. Two
independent things enforce that, and both are needed:

* the **entity** refuses ``decided()`` on anything but a pending proposal,
  which is the check that can explain itself;
* the **repository** names ``pending`` in the ``WHERE`` clause of its one
  update, which is the check that survives two decisions racing. A
  check-then-write could be overtaken between the two steps; this cannot.

There is no undo. Reversing an acceptance would have to decide what becomes of a
Memory that has since been read, quoted and acted on; reopening a rejection
would mean a fact a person declined could appear anyway. Both are recoverable by
ordinary means -- delete the memory, or accept the fact again next time it is
proposed -- and neither needs a mechanism that can rewrite a decision (ADR-059).

Acceptance creates exactly one Memory, in one transaction
----------------------------------------------------------

The decision and its consequence are the same event, so they are the same
transaction: a committed acceptance with no memory would be a fact the user
believes they kept and cannot find, and a memory with a pending proposal would
be knowledge nobody approved.

"Exactly one" is a **unique index on** ``memories.proposal_id``, not a rule this
module keeps. Rules can be forgotten by the next caller; indexes cannot.

What acceptance does not do
---------------------------

It does not re-ask the model, re-check the evidence, or reconsider the
confidence. Those were the extractor's job and they were done before the
proposal was shown to anybody. Acceptance is a person saying *keep this*, and
the only thing this module adds to what they approved is the identity, the
timestamp, the contact the fact is about, and the derived key.
"""

from __future__ import annotations

from dataclasses import dataclass

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.domain.errors import (
    InvalidStateTransitionError,
    RecordNotFoundError,
)
from tgassist.domain.events import (
    MemoryCreated,
    MemoryProposalAccepted,
    MemoryProposalRejected,
)
from tgassist.domain.model.identifiers import (
    AccountId,
    ContactId,
    MemoryId,
    MemoryProposalId,
)
from tgassist.domain.model.memory import (
    Importance,
    Memory,
    MemoryProposal,
    ProposalStatus,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.conversation_repository import ConversationRepository
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.memory_proposal_repository import MemoryProposalRepository
from tgassist.domain.ports.memory_repository import MemoryRepository
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    """What accepting one proposal produced.

    Attributes:
        proposal: The proposal, now accepted.
        memory: The memory it became. Exactly one, always.
    """

    proposal: MemoryProposal
    memory: Memory


class AcceptMemoryProposal:
    """Turns one candidate fact into knowledge, once."""

    __slots__ = (
        "_accounts",
        "_chats",
        "_clock",
        "_conversations",
        "_events",
        "_ids",
        "_memories",
        "_proposals",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        proposals: ScopedRepositoryFactory[MemoryProposalRepository],
        memories: ScopedRepositoryFactory[MemoryRepository],
        conversations: ScopedRepositoryFactory[ConversationRepository],
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
        events: EventBus | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory. One transaction: the decision and
                the memory it creates are the same event.
            proposals: Memory proposal repository factory, scoped per account.
            memories: Memory repository factory, scoped per account.
            conversations: Conversation repository factory. Read to reach the
                chat, and through it the person the fact is about.
            chats: Chat repository factory, for the same reason.
            accounts: Account repository factory.
            clock: Time source, for the decision and the memory.
            ids: Local identifier generator. A memory gets a new one rather than
                the proposal's: they are different things with different
                lifetimes (ADR-059).
            events: Where ``MemoryProposalAccepted`` and ``MemoryCreated`` are
                published, after the transaction commits.
        """
        self._unit_of_work = unit_of_work
        self._proposals = proposals
        self._memories = memories
        self._conversations = conversations
        self._chats = chats
        self._accounts = accounts
        self._clock = clock
        self._ids = ids
        self._events = events

    async def execute(
        self,
        proposal_id: int,
        *,
        account_id: AccountId | None = None,
        importance: Importance | None = None,
    ) -> AcceptanceResult:
        """Accept one proposal and create the memory it becomes.

        Args:
            proposal_id: The proposal to accept.
            account_id: Account to operate on. ``None`` selects the active one.
            importance: How much the person accepting says this matters.
                Defaults to normal, which is what accepting without saying
                means. Ranked above the model's confidence when a context is
                assembled (ADR-060), and set here because this is the moment
                somebody is looking at the fact and can judge.

        Returns:
            The decided proposal and the memory it produced.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such proposal.
            InvalidStateTransitionError: If the proposal has already been
                decided. The message says which way, and an already-accepted
                proposal names the memory it produced -- "you already kept
                this, here it is" is a more useful answer than a refusal.
            ConstraintViolationError: If this account already remembers the same
                fact about the same person.
        """
        now = self._clock.now()
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            proposals = self._proposals(uow, resolved)
            memories = self._memories(uow, resolved)

            proposal = await _require_proposal(proposals, resolved, proposal_id)
            await self._require_pending(memories, proposal)

            contact_id = await self._subject_of(uow, resolved, proposal)
            memory = Memory.approved(
                memory_id=MemoryId(self._ids.new_id()),
                proposal=proposal,
                contact_id=contact_id,
                now=now,
                importance=importance,
            )

            # The entity's refusal first: it is the one that explains itself,
            # and reaching the repository with an already-decided proposal would
            # produce a bare "0 rows" instead.
            decided = proposal.decided(ProposalStatus.ACCEPTED, now)
            if not await proposals.decide(proposal.id, ProposalStatus.ACCEPTED, now):
                # Nothing between the read and here can normally cause this, so
                # it means another decision arrived first. Raising rather than
                # proceeding is the point of the conditional write.
                raise _already_decided(proposal)
            await memories.add(memory)
            await uow.commit()

        await self._announce(resolved, decided, memory)
        return AcceptanceResult(proposal=decided, memory=memory)

    async def _require_pending(self, memories: MemoryRepository, proposal: MemoryProposal) -> None:
        """Refuse a proposal that has already been decided, helpfully.

        An already-accepted proposal names the memory it produced. A user
        accepting the same row twice has usually forgotten they did it, and
        being told what happened last time is more useful than being told no.
        """
        if proposal.is_pending:
            return
        if proposal.status is ProposalStatus.ACCEPTED:
            existing = await memories.get_by_proposal(proposal.id)
            if existing is not None:
                msg = (
                    f"Memory proposal {int(proposal.id)} was already accepted, "
                    f"as memory {int(existing.id)}"
                )
                raise InvalidStateTransitionError(
                    msg,
                    user_message=(f"That was already accepted; it is memory {int(existing.id)}."),
                    context={
                        "proposal_id": int(proposal.id),
                        "memory_id": int(existing.id),
                    },
                )
        raise _already_decided(proposal)

    async def _subject_of(
        self, uow: UnitOfWork, account_id: AccountId, proposal: MemoryProposal
    ) -> ContactId | None:
        """Return the person a proposal's fact is about.

        Resolved here rather than stored on the proposal, because a proposal is
        about a *conversation* and a conversation knows its chat. ``None`` for a
        chat with no single counterpart -- a group -- which is the only reason a
        memory's contact is nullable.
        """
        conversation = await self._conversations(uow, account_id).get(proposal.conversation_id)
        if conversation is None:
            # The composite foreign key makes this unreachable while the
            # proposal exists, and it is checked rather than assumed because the
            # alternative is a memory about nobody.
            return None
        chat = await self._chats(uow, account_id).get(conversation.chat_id)
        return chat.contact_id if chat is not None else None

    async def _announce(
        self, account_id: AccountId, proposal: MemoryProposal, memory: Memory
    ) -> None:
        """Publish the decision and its consequence, after the commit.

        Two events for one transaction, because they are about different
        things: one is that a person decided, the other is that something is now
        known. Nothing subscribes to either yet, and neither is shaped for a
        guessed consumer -- the shape is what the decision itself contains.
        """
        if self._events is None:
            return
        await self._events.publish(
            MemoryProposalAccepted(
                account_id=int(account_id),
                proposal_id=int(proposal.id),
                memory_id=int(memory.id),
                contact_id=int(memory.contact_id) if memory.contact_id is not None else None,
                category=memory.category.value,
            )
        )
        await self._events.publish(
            MemoryCreated(
                account_id=int(account_id),
                memory_id=int(memory.id),
                contact_id=int(memory.contact_id) if memory.contact_id is not None else None,
                category=memory.category.value,
                source=memory.source.value,
            )
        )


class RejectMemoryProposal:
    """Declines one candidate fact, permanently."""

    __slots__ = ("_accounts", "_clock", "_events", "_proposals", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        proposals: ScopedRepositoryFactory[MemoryProposalRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        events: EventBus | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._proposals = proposals
        self._accounts = accounts
        self._clock = clock
        self._events = events

    async def execute(
        self, proposal_id: int, *, account_id: AccountId | None = None
    ) -> MemoryProposal:
        """Reject one proposal.

        Creates nothing. The row is kept rather than deleted, so the extractor
        does not offer the same fact again (``DOMAIN_MODEL.md`` section 5.10) --
        a rejection is a decision worth storing, not a row worth removing.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such proposal.
            InvalidStateTransitionError: If the proposal has already been
                decided.
        """
        now = self._clock.now()
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            proposals = self._proposals(uow, resolved)

            proposal = await _require_proposal(proposals, resolved, proposal_id)
            decided = proposal.decided(ProposalStatus.REJECTED, now)
            if not await proposals.decide(proposal.id, ProposalStatus.REJECTED, now):
                raise _already_decided(proposal)
            await uow.commit()

        if self._events is not None:
            await self._events.publish(
                MemoryProposalRejected(
                    account_id=int(resolved),
                    proposal_id=int(proposal.id),
                    category=proposal.category.value,
                )
            )
        return decided


class DeleteMemory:
    """Forgets one memory, softly."""

    __slots__ = ("_accounts", "_clock", "_memories", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        memories: ScopedRepositoryFactory[MemoryRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._memories = memories
        self._accounts = accounts
        self._clock = clock

    async def execute(self, memory_id: int, *, account_id: AccountId | None = None) -> bool:
        """Forget one memory.

        Soft: the row stays, with a timestamp. That is what lets retention ask
        "deleted before when", and what frees the memory's key so the same fact
        can be accepted again -- the only route to a correction, since nothing
        edits a memory.

        Returns:
            Whether a live memory was forgotten. ``False`` when it had already
            been, which is not an error: forgetting twice leaves the same state.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such memory.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            memories = self._memories(uow, resolved)

            found = await memories.get(MemoryId(memory_id))
            if found is None:
                msg = f"No memory {memory_id} in account {int(resolved)}"
                raise RecordNotFoundError(
                    msg,
                    user_message="That memory was not found.",
                    context={"memory_id": memory_id},
                )

            forgotten = await memories.delete(found.id, self._clock.now())
            if forgotten:
                await uow.commit()
            return forgotten


class GetMemory:
    """Looks one memory up."""

    __slots__ = ("_accounts", "_memories", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        memories: ScopedRepositoryFactory[MemoryRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._memories = memories
        self._accounts = accounts

    async def execute(
        self, memory_id: int, *, account_id: AccountId | None = None
    ) -> Memory | None:
        """Return one memory, deleted or not, or ``None`` if absent."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._memories(uow, resolved).get(MemoryId(memory_id))


class ListMemories:
    """Returns a page of what this account remembers."""

    __slots__ = ("_accounts", "_memories", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        memories: ScopedRepositoryFactory[MemoryRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._memories = memories
        self._accounts = accounts

    async def execute(
        self, request: PageRequest | None = None, *, account_id: AccountId | None = None
    ) -> Page[Memory]:
        """Return one page of live memories, newest first."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._memories(uow, resolved).list_active(request or PageRequest())


# -- Shared -----------------------------------------------------------------


async def _require_proposal(
    proposals: MemoryProposalRepository, account_id: AccountId, proposal_id: int
) -> MemoryProposal:
    """Return the proposal being decided, raising if this account has none."""
    found = await proposals.get(MemoryProposalId(proposal_id))
    if found is None:
        msg = f"No memory proposal {proposal_id} in account {int(account_id)}"
        raise RecordNotFoundError(
            msg,
            user_message="That proposal was not found.",
            context={"proposal_id": proposal_id},
        )
    return found


def _already_decided(proposal: MemoryProposal) -> InvalidStateTransitionError:
    """Build the refusal for a proposal somebody has already decided about."""
    msg = (
        f"Memory proposal {int(proposal.id)} was already {proposal.status.value}; "
        f"a decision is made once"
    )
    return InvalidStateTransitionError(
        msg,
        user_message=f"That proposal was already {proposal.status.value}.",
        context={"proposal_id": int(proposal.id), "status": proposal.status.value},
    )


__all__ = [
    "AcceptMemoryProposal",
    "AcceptanceResult",
    "DeleteMemory",
    "GetMemory",
    "ListMemories",
    "RejectMemoryProposal",
]
