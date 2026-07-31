"""Memory proposal repository port.

Scoped to one Account at construction (ADR-039).

**No ``update`` and no ``delete``.** A proposal records what a model said at a
moment, and that does not become untrue later. The same discipline ``Message``
and ``AiCall`` have (ADR-046, ADR-057).

There is exactly **one** mutation, :meth:`decide`, and it is a named method with
its restriction in its signature rather than a general update: pending to a
terminal state, once. A proposal cannot be re-opened, because no method returns
one to ``pending`` (ADR-059).

Five operations, each traceable to a caller that exists:

* :meth:`add` -- ``ExtractMemories``, once per accepted candidate.
* :meth:`get` -- ``tgassist memory show``.
* :meth:`list_recent` -- ``tgassist memory proposals``.
* :meth:`list_for_conversation` -- ``ExtractMemories``, to avoid proposing
  again what has already been proposed. Re-running extraction on a conversation
  must be free, and this is what makes it so.
* :meth:`decide` -- ``AcceptMemoryProposal`` and ``RejectMemoryProposal``.

There is no ``count_pending``, though a review queue will want one. `memory
proposals` pages rather than counting, and a count with no caller is a query
nobody has measured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tgassist.domain.model.identifiers import AccountId, ConversationId, MemoryProposalId
from tgassist.domain.model.memory import MemoryProposal, ProposalStatus
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest


@runtime_checkable
class MemoryProposalRepository(Protocol):
    """Stores the candidate facts of one account.

    Satisfies the repository contract in ``domain/ports/repository.py``:
    absence returns ``None`` rather than raising, the repository never commits,
    and results are domain objects rather than rows.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def add(self, proposal: MemoryProposal) -> None:
        """Persist one candidate fact.

        Raises:
            DomainValidationError: If the proposal belongs to another account.
            ConstraintViolationError: If the identifier is taken, if the
                conversation or AI call does not belong to this account, or if
                the same fact has already been proposed for that conversation.
        """
        ...

    async def get(self, proposal_id: MemoryProposalId) -> MemoryProposal | None:
        """Return one of this account's proposals, or ``None`` if absent."""
        ...

    async def list_recent(self, request: PageRequest) -> Page[MemoryProposal]:
        """Return one page of this account's proposals, newest first."""
        ...

    async def decide(
        self, proposal_id: MemoryProposalId, status: ProposalStatus, now: datetime
    ) -> bool:
        """Record a decision about one proposal, once.

        The write names ``pending`` in its condition, so a second decision --
        or two decisions racing -- cannot both succeed. The entity refuses the
        transition as well; this is the half of the guarantee that survives
        concurrency, and the entity's half is the one that explains itself.

        Args:
            proposal_id: What was decided.
            status: ``ACCEPTED`` or ``REJECTED``. Never ``PENDING``: a decision
                cannot be undone.
            now: When it was decided.

        Returns:
            Whether a *pending* proposal was decided. ``False`` when there was
            none, or when it had already been decided -- which is what lets a
            caller tell "decided now" from "was already decided" without a
            second query.
        """
        ...

    async def list_for_conversation(
        self, conversation_id: ConversationId
    ) -> tuple[MemoryProposal, ...]:
        """Return every proposal already made for one conversation.

        Every proposal, **not** only the pending ones. A rejected proposal is
        kept precisely so the same fact is not offered again (``DOMAIN_MODEL.md``
        section 5.10), and a duplicate check that ignored it would re-propose
        what the user has already declined.

        Unpaged, because the caller needs all of them to compare against and a
        conversation's proposals are bounded by the extraction cap.
        """
        ...
