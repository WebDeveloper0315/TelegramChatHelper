"""Memory proposal mapper and repository.

Stores one row per candidate fact awaiting a decision.

**One mutation, and it is a decision.** A proposal records what a model said at
a moment, and that does not become untrue later. :meth:`
SqlMemoryProposalRepository.decide` is the single write that changes a stored
proposal, and its ``WHERE`` clause names ``pending`` -- so a second decision, or
two racing, cannot both succeed. Nothing returns a proposal to pending, so a
decision cannot be undone (ADR-058, ADR-059).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, insert, select, update

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.ai import PromptVersion
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
    ProposalStatus,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_proposal_repository import MemoryProposalRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import memory_proposals
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "created_at"


class MemoryProposalMapper(EntityMapper[MemoryProposal]):
    """Converts between :class:`MemoryProposal` and its row."""

    def to_domain(self, row: Any) -> MemoryProposal:
        """Build a proposal from a row."""
        created_at = from_stored_datetime(_as_iso(row.created_at))
        if created_at is None:  # pragma: no cover - schema forbids
            msg = "A memory proposal row is missing its timestamp"
            raise DomainValidationError(msg, user_message="That proposal record is incomplete.")

        return MemoryProposal(
            id=MemoryProposalId(row.id),
            account_id=AccountId(row.account_id),
            conversation_id=ConversationId(row.conversation_id),
            ai_call_id=AiCallId(row.ai_call_id),
            category=MemoryCategory(row.category),
            value=row.value,
            confidence=Confidence(float(row.confidence)),
            evidence=Evidence(row.evidence),
            prompt=PromptVersion(prompt_id=row.prompt_id, version=row.prompt_version),
            status=ProposalStatus(row.status),
            created_at=created_at,
            decided_at=from_stored_datetime(_as_iso(row.decided_at)),
        )

    def to_params(self, entity: MemoryProposal) -> dict[str, Any]:
        """Build column values from a proposal."""
        return {
            "id": int(entity.id),
            "account_id": int(entity.account_id),
            "conversation_id": int(entity.conversation_id),
            "ai_call_id": int(entity.ai_call_id),
            "category": entity.category.value,
            "value": entity.value,
            "confidence": entity.confidence.value,
            "status": entity.status.value,
            "evidence": entity.evidence.quote,
            "prompt_id": entity.prompt.prompt_id,
            "prompt_version": entity.prompt.version,
            "created_at": entity.created_at,
            "decided_at": entity.decided_at,
        }


def _as_iso(value: Any) -> str | None:
    """Render a stored timestamp as ISO text, whichever form the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class SqlMemoryProposalRepository(Repository[MemoryProposal], MemoryProposalRepository):
    """Stores the candidate facts of one account.

    Scoped at construction (ADR-039). This class has no ``update`` and no
    ``delete``, and the absence of both is the guarantee.
    """

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = MemoryProposalMapper()
        self._paginator = KeysetPaginator(
            sort_column=memory_proposals.c.created_at,
            tiebreak_column=memory_proposals.c.id,
            sort_field=SORT_FIELD,
        )

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    def _scoped(self) -> Select[Any]:
        """Return the base query, already filtered to this account."""
        return select(memory_proposals).where(
            memory_proposals.c.account_id == int(self._account_id)
        )

    async def add(self, proposal: MemoryProposal) -> None:
        """Persist one candidate fact."""
        self._require_own(proposal, operation="add")
        await self.execute_write(
            insert(memory_proposals).values(self._mapper.to_params(proposal)),
            operation="add_memory_proposal",
            conflict_message="That fact has already been proposed for this conversation.",
        )

    async def get(self, proposal_id: MemoryProposalId) -> MemoryProposal | None:
        """Return one of this account's proposals, or ``None`` if absent."""
        row = await self.fetch_one(
            self._scoped().where(memory_proposals.c.id == int(proposal_id)),
            operation="get_memory_proposal",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def list_recent(self, request: PageRequest) -> Page[MemoryProposal]:
        """Return one page of this account's proposals, newest first."""
        return await self.fetch_page(
            self._scoped(),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_memory_proposals",
        )

    async def decide(
        self, proposal_id: MemoryProposalId, status: ProposalStatus, now: datetime
    ) -> bool:
        """Record a decision about one proposal, once.

        ``status = 'pending'`` is part of the condition rather than something
        checked first: a check-then-write could be overtaken between the two,
        and this cannot. The entity refuses the transition as well, and that is
        the half that explains itself; this is the half that survives
        concurrency.
        """
        result = await self.execute_write(
            update(memory_proposals)
            .where(
                memory_proposals.c.id == int(proposal_id),
                memory_proposals.c.account_id == int(self._account_id),
                memory_proposals.c.status == ProposalStatus.PENDING.value,
            )
            .values(status=status.value, decided_at=now),
            operation="decide_memory_proposal",
            conflict_message="That proposal could not be decided.",
        )
        return bool(result.rowcount)

    async def list_for_conversation(
        self, conversation_id: ConversationId
    ) -> tuple[MemoryProposal, ...]:
        """Return every proposal already made for one conversation.

        Ordered by identifier so the answer is stable, which is what lets a
        duplicate check produce the same result twice.
        """
        rows = await self.fetch_all(
            self._scoped()
            .where(memory_proposals.c.conversation_id == int(conversation_id))
            .order_by(memory_proposals.c.id),
            operation="list_memory_proposals_for_conversation",
        )
        return tuple(self._mapper.to_domain(row) for row in rows)

    def _require_own(self, proposal: MemoryProposal, *, operation: str) -> None:
        """Refuse a proposal belonging to a different account."""
        if proposal.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a memory proposal of account "
                f"{int(proposal.account_id)} through a repository scoped to account "
                f"{int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That proposal belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "proposal_account": int(proposal.account_id),
                },
            )


def memory_proposal_repository(
    uow: UnitOfWork, account_id: AccountId
) -> SqlMemoryProposalRepository:
    """Build a memory proposal repository scoped to one account.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = (
            f"SqlMemoryProposalRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        )
        raise TypeError(msg)
    return SqlMemoryProposalRepository(uow, account_id)


__all__ = [
    "MemoryProposalMapper",
    "SqlMemoryProposalRepository",
    "memory_proposal_repository",
]
