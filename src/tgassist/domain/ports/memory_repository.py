"""Memory repository port.

Scoped to one Account at construction (ADR-039).

**No ``update``.** A memory is immutable: correcting one means deleting it and
accepting a new proposal, because an edit in place would keep the provenance
while changing the fact, and the provenance is what makes a stored claim
checkable (ADR-059).

:meth:`delete` is the one exception, and it is not a general update. It is soft
-- a timestamp, not a removal -- and it is a *named* operation with one meaning,
so no caller can reach it while intending something else. A hard delete belongs
to retention (Milestone 10) and will be a bulk operation rather than a method
here.

Seven operations, each traceable to a caller that exists:

* :meth:`add` -- ``AcceptMemoryProposal``.
* :meth:`get` -- ``tgassist memory show``.
* :meth:`list_active` -- ``tgassist memory list``.
* :meth:`get_by_proposal` -- ``AcceptMemoryProposal``, to report the memory an
  already-accepted proposal produced rather than a bare refusal.
* :meth:`delete` -- ``tgassist memory forget``.
* :meth:`list_for_contact` -- ``BuildMemoryContext``, the retrieval read.
* :meth:`mark_retrieved` -- ``BuildMemoryContext``, the retrieval write.

:meth:`mark_retrieved` is the second exception to "no update", and it is the
same kind as :meth:`delete`: a named operation with one meaning. What it changes
is *bookkeeping about* a memory -- how often it has been used -- not the fact
itself, which is why a memory can stay immutable while its counters move
(ADR-060).

There is still no ``search``, no similarity and no vector operation. Semantic
retrieval is a later slice, and it will need a port shaped by an embedding
index rather than by this one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from tgassist.domain.model.identifiers import (
    AccountId,
    ContactId,
    MemoryId,
    MemoryProposalId,
)
from tgassist.domain.model.memory import Memory
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest


@runtime_checkable
class MemoryRepository(Protocol):
    """Stores the approved facts of one account.

    Satisfies the repository contract in ``domain/ports/repository.py``:
    absence returns ``None`` rather than raising, the repository never commits,
    and results are domain objects rather than rows.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def add(self, memory: Memory) -> None:
        """Persist one approved fact.

        Raises:
            DomainValidationError: If the memory belongs to another account.
            ConstraintViolationError: If the identifier is taken, if the
                contact does not belong to this account, if the proposal has
                already produced a memory, or if this account already holds a
                live memory with the same contact, category and key.
        """
        ...

    async def get(self, memory_id: MemoryId) -> Memory | None:
        """Return one of this account's memories, or ``None`` if absent.

        Returns deleted memories too. "Show me what you deleted" is a question
        a person is entitled to ask of their own data, and a lookup that hid
        them would make the deletion less visible than the fact.
        """
        ...

    async def get_by_proposal(self, proposal_id: MemoryProposalId) -> Memory | None:
        """Return the memory an accepted proposal produced, if it still exists."""
        ...

    async def list_active(self, request: PageRequest) -> Page[Memory]:
        """Return one page of this account's live memories, newest first.

        Deleted memories are excluded. This is the listing a person reads to see
        what the application knows, and something it has been told to forget is
        not that.
        """
        ...

    async def list_for_contact(
        self, contact_id: ContactId | None, *, limit: int
    ) -> tuple[Memory, ...]:
        """Return the live memories about one contact, newest first.

        The retrieval read. **Never crosses contacts**: a memory about one
        person cannot reach a conversation with another, and that is the
        repository's job rather than the selector's, so there is one place to
        get it wrong (ADR-060).

        Args:
            contact_id: Who to retrieve about. ``None`` means the memories that
                are about *nobody in particular* -- the ones extracted from
                conversations with no single counterpart. It does **not** mean
                "everybody": a group chat sees only contactless facts, and a
                private chat only that person's.
            limit: The most to return. A bound on how much a context can cost to
                build, not on how much of it is used -- the selector's budget
                decides that. Reaching it is worth reporting, because a
                truncated candidate set means ranking never saw everything.

        Returns:
            Live memories only. Something the user has forgotten is not
            something the application knows.
        """
        ...

    async def mark_retrieved(self, memory_ids: Sequence[MemoryId], now: datetime) -> int:
        """Record that these memories were selected into a context.

        One statement over all of them, and the count is incremented **in the
        database** rather than read and written back -- so two contexts built at
        once cannot lose an increment.

        Args:
            memory_ids: What was selected. An empty sequence writes nothing.
            now: When the context was built.

        Returns:
            How many rows were updated. Fewer than asked for means something was
            deleted between the read and the write, which is not an error --
            the context was still built from what was true when it was read.
        """
        ...

    async def delete(self, memory_id: MemoryId, now: datetime) -> bool:
        """Forget one memory, softly.

        A timestamp rather than a removal, because retention has to ask
        "deleted before when" and a boolean cannot answer that. Deleting frees
        the memory's key, so the same fact can be accepted again -- which is the
        only route to a correction, since nothing edits a memory.

        Returns:
            Whether a live memory was deleted. ``False`` when there was none, or
            when it had already been deleted -- so a caller can tell "forgotten
            now" from "was already forgotten" without a second query, and
            deleting twice is not an error.
        """
        ...
