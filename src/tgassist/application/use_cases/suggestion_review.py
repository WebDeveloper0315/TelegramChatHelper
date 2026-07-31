"""Deciding about suggestions.

The half a person performs. Generation (``use_cases/suggestion.py``) produces
drafts; this decides about them, and **deciding is all it does**.

Accepting executes nothing
--------------------------

There is no executor in this module, no gateway, no scheduler and no queue
worker. ``AcceptSuggestion`` changes a status, publishes a fact, and returns.
That is not an omission to be filled in quietly later: it is the guarantee this
slice exists to establish, and the shape enforces it -- these classes are given
nothing that could send a message, so they could not send one if they tried
(ADR-062).

When something eventually does act on an accepted suggestion, it will be a
component somebody added deliberately, subscribing to an event that already
says exactly what was agreed to.

A decision is made once
-----------------------

Two independent things enforce it, exactly as for memory proposals (ADR-059):

* the **entity** refuses ``decided()`` on anything but a pending suggestion,
  which is the check that explains itself;
* the **repository** names ``pending`` in the ``WHERE`` clause of its one
  update, which is the check that survives two decisions racing.

There is no undo and no reopen. A person who changes their mind about a
dismissed draft can generate another; a person who changes their mind about an
accepted one has lost nothing, because accepting did not do anything.
"""

from __future__ import annotations

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.domain.errors import InvalidStateTransitionError, RecordNotFoundError
from tgassist.domain.events import SuggestionAccepted, SuggestionDismissed
from tgassist.domain.model.identifiers import AccountId, ChatId, SuggestionId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.suggestion import Suggestion, SuggestionStatus
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.suggestion_repository import SuggestionRepository
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory


class DecideSuggestion:
    """Records one decision about one suggestion.

    Both decisions are the same operation with a different terminal state and a
    different event, so they share an implementation and differ by one value.
    ``AcceptSuggestion`` and ``DismissSuggestion`` are the names callers use,
    because a caller choosing between ``decide(status=...)`` values has to know
    which is which.
    """

    __slots__ = ("_accounts", "_clock", "_events", "_suggestions", "_unit_of_work")

    #: What this use case records. Overridden by each subclass.
    status: SuggestionStatus = SuggestionStatus.PENDING

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        suggestions: ScopedRepositoryFactory[SuggestionRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        events: EventBus | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Note what is **not** here: no Telegram gateway, no AI provider, no
        scheduler. A decision needs none of them, and a use case that held one
        could act on its own conclusion (ADR-062).

        Args:
            unit_of_work: Transaction factory. One transaction per decision.
            suggestions: Suggestion repository factory, scoped per account.
            accounts: Account repository factory.
            clock: Time source, for the decision's timestamp.
            events: Where the decision is published, after the commit.
        """
        self._unit_of_work = unit_of_work
        self._suggestions = suggestions
        self._accounts = accounts
        self._clock = clock
        self._events = events

    async def execute(
        self, suggestion_id: int, *, account_id: AccountId | None = None
    ) -> Suggestion:
        """Record the decision, in one transaction.

        Args:
            suggestion_id: What is being decided.
            account_id: Account to operate on. ``None`` selects the active one.

        Returns:
            The suggestion, decided.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such suggestion.
            InvalidStateTransitionError: If it has already been decided. The
                message says which way.
        """
        now = self._clock.now()
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            repository = self._suggestions(uow, resolved)

            found = await repository.get(SuggestionId(suggestion_id))
            if found is None:
                msg = f"No suggestion {suggestion_id} in account {int(resolved)}"
                raise RecordNotFoundError(
                    msg,
                    user_message="That suggestion was not found.",
                    context={"suggestion_id": suggestion_id},
                )

            # The entity's refusal first: it is the one that explains itself,
            # and reaching the repository with a decided suggestion would
            # produce a bare "0 rows" instead.
            decided = found.decided(self.status, now)
            if not await repository.decide(found.id, self.status, now):
                # Nothing between the read and here can normally cause this, so
                # it means another decision arrived first. Raising rather than
                # proceeding is the point of the conditional write.
                raise _already_decided(found)
            await uow.commit()

        await self._announce(resolved, decided)
        return decided

    async def _announce(self, account_id: AccountId, suggestion: Suggestion) -> None:
        """Publish the decision, after the transaction that recorded it.

        After the commit, never inside it: a subscriber observing a decision
        that then rolled back would be acting on something nobody decided.
        """
        if self._events is None:
            return
        event = (
            SuggestionAccepted(
                account_id=int(account_id),
                suggestion_id=int(suggestion.id),
                chat_id=int(suggestion.chat_id),
                proposal_type=suggestion.proposal_type.value,
            )
            if suggestion.was_accepted
            else SuggestionDismissed(
                account_id=int(account_id),
                suggestion_id=int(suggestion.id),
                chat_id=int(suggestion.chat_id),
                proposal_type=suggestion.proposal_type.value,
            )
        )
        await self._events.publish(event)


class AcceptSuggestion(DecideSuggestion):
    """Records that a person agreed with a suggestion.

    **Agreement, and nothing else.** No message is sent, no task is scheduled
    and no state but the status changes. If a person wants to act on what they
    agreed with, they do it themselves -- and when something automates that, it
    will be a component somebody switched on rather than a consequence of this
    method (ADR-062).
    """

    __slots__ = ()

    status = SuggestionStatus.ACCEPTED


class DismissSuggestion(DecideSuggestion):
    """Records that a person declined a suggestion.

    The row is kept rather than deleted. A record containing only what was
    agreed with cannot show what the generator is getting wrong, and that is the
    measurement that decides whether a prompt needs rewriting.
    """

    __slots__ = ()

    status = SuggestionStatus.DISMISSED


class GetSuggestion:
    """Looks one suggestion up."""

    __slots__ = ("_accounts", "_suggestions", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        suggestions: ScopedRepositoryFactory[SuggestionRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._suggestions = suggestions
        self._accounts = accounts

    async def execute(
        self, suggestion_id: int, *, account_id: AccountId | None = None
    ) -> Suggestion | None:
        """Return one suggestion, decided or not, or ``None`` if absent."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._suggestions(uow, resolved).get(SuggestionId(suggestion_id))


class ListSuggestions:
    """Returns a page of suggestions: the queue, or one chat's history."""

    __slots__ = ("_accounts", "_suggestions", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        suggestions: ScopedRepositoryFactory[SuggestionRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._suggestions = suggestions
        self._accounts = accounts

    async def execute(
        self,
        request: PageRequest | None = None,
        *,
        chat_id: int | None = None,
        account_id: AccountId | None = None,
    ) -> Page[Suggestion]:
        """Return one page of suggestions, newest first.

        Args:
            request: Which page.
            chat_id: A chat to filter to. When given, **decided suggestions are
                included** -- reviewing a conversation's history means seeing
                what was dismissed as well as what was kept. When absent, the
                result is the review queue: undecided only.
            account_id: Account to operate on. ``None`` selects the active one.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            repository = self._suggestions(uow, resolved)
            paging = request or PageRequest()
            if chat_id is None:
                return await repository.list_pending(paging)
            return await repository.list_by_chat(ChatId(chat_id), paging)


def _already_decided(suggestion: Suggestion) -> InvalidStateTransitionError:
    """Build the refusal for a suggestion somebody has already decided about."""
    msg = (
        f"Suggestion {int(suggestion.id)} was already {suggestion.status.value}; "
        f"a decision is made once"
    )
    return InvalidStateTransitionError(
        msg,
        user_message=f"That suggestion was already {suggestion.status.value}.",
        context={"suggestion_id": int(suggestion.id), "status": suggestion.status.value},
    )


__all__ = [
    "AcceptSuggestion",
    "DecideSuggestion",
    "DismissSuggestion",
    "GetSuggestion",
    "ListSuggestions",
]
