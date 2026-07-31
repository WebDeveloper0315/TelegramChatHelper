"""Suggestion repository port.

Scoped to one Account at construction (ADR-039).

**No ``update``.** A suggestion records what a model proposed at a moment, and
that does not become untrue later. Nothing edits one, because an edited draft is
not what the model suggested — and the record exists to say what it suggested
when somebody agreed with it (ADR-062).

There is exactly **one** mutation, :meth:`decide`, and it is a named method with
its restriction in its signature rather than a general update: pending to a
terminal state, once. A suggestion cannot be reopened, because no method returns
one to ``pending``.

Five operations, each traceable to a caller that exists:

* :meth:`add` -- ``GenerateConversationSuggestion``, once per draft produced.
* :meth:`get` -- ``tgassist suggestion show``, and both decisions.
* :meth:`list_pending` -- ``tgassist suggestion list``, the review queue.
* :meth:`list_by_chat` -- ``tgassist suggestion list --chat``, which shows what
  was dismissed as well as what was kept.
* :meth:`decide` -- ``AcceptSuggestion`` and ``DismissSuggestion``.

There is no ``execute``, no ``send`` and no ``schedule``. Accepting a suggestion
records agreement; nothing in this application acts on one, and the absence of a
method is how that is guaranteed rather than a rule somebody has to keep.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tgassist.domain.model.identifiers import AccountId, ChatId, SuggestionId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.suggestion import Suggestion, SuggestionStatus


@runtime_checkable
class SuggestionRepository(Protocol):
    """Stores the drafts of one account.

    Satisfies the repository contract in ``domain/ports/repository.py``:
    absence returns ``None`` rather than raising, the repository never commits,
    and results are domain objects rather than rows.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def add(self, suggestion: Suggestion) -> None:
        """Persist one draft.

        Raises:
            DomainValidationError: If the suggestion belongs to another account.
            ConstraintViolationError: If the identifier is taken, or if the
                chat, conversation or AI call does not belong to this account.
        """
        ...

    async def get(self, suggestion_id: SuggestionId) -> Suggestion | None:
        """Return one of this account's suggestions, or ``None`` if absent.

        Returns decided suggestions too. "What did I dismiss last week" is a
        question a person is entitled to ask, and it is also the measurement
        that says whether the generator is worth its cost.
        """
        ...

    async def list_pending(self, request: PageRequest) -> Page[Suggestion]:
        """Return one page of this account's undecided suggestions, newest first.

        The review queue. Decided suggestions are excluded: a queue is by
        definition what has not been decided.
        """
        ...

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Suggestion]:
        """Return one page of one chat's suggestions, newest first.

        **Decided ones included.** Reviewing a conversation's history means
        seeing what was dismissed as well as what was kept, and a listing that
        hid the dismissals would make a generator that is usually wrong look
        like one that is rarely used.
        """
        ...

    async def decide(
        self, suggestion_id: SuggestionId, status: SuggestionStatus, now: datetime
    ) -> bool:
        """Record a decision about one suggestion, once.

        The write names ``pending`` in its condition, so a second decision --
        or two decisions racing -- cannot both succeed. The entity refuses the
        transition as well; this is the half of the guarantee that survives
        concurrency, and the entity's half is the one that explains itself.

        Args:
            suggestion_id: What was decided.
            status: ``ACCEPTED`` or ``DISMISSED``. Never ``PENDING``: a decision
                cannot be undone.
            now: When it was decided.

        Returns:
            Whether a *pending* suggestion was decided. ``False`` when there was
            none, or when it had already been decided -- which is what lets a
            caller tell "decided now" from "was already decided" without a
            second query.
        """
        ...
