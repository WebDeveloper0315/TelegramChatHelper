"""Memory mapper and repository.

Stores one row per fact a person has approved.

**No update.** A memory is immutable, and the only mutation here is
:meth:`SqlMemoryRepository.delete` -- a soft delete, written as a named
operation with one meaning rather than as a general update (ADR-059).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, insert, select, update

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ContactId,
    ConversationId,
    MemoryId,
    MemoryProposalId,
)
from tgassist.domain.model.memory import (
    Confidence,
    Importance,
    Memory,
    MemoryCategory,
    MemoryKey,
    MemorySource,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_repository import MemoryRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import memories
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "created_at"


class MemoryMapper(EntityMapper[Memory]):
    """Converts between :class:`Memory` and its row."""

    def to_domain(self, row: Any) -> Memory:
        """Build a memory from a row."""
        created_at = from_stored_datetime(_as_iso(row.created_at))
        if created_at is None:  # pragma: no cover - schema forbids
            msg = "A memory row is missing its timestamp"
            raise DomainValidationError(msg, user_message="That memory record is incomplete.")

        return Memory(
            id=MemoryId(row.id),
            account_id=AccountId(row.account_id),
            contact_id=ContactId(row.contact_id) if row.contact_id is not None else None,
            category=MemoryCategory(row.category),
            key=MemoryKey(row.key),
            value=row.value,
            confidence=Confidence(float(row.confidence)),
            source=MemorySource(row.source),
            proposal_id=(
                MemoryProposalId(row.proposal_id) if row.proposal_id is not None else None
            ),
            conversation_id=(
                ConversationId(row.conversation_id) if row.conversation_id is not None else None
            ),
            ai_call_id=AiCallId(row.ai_call_id) if row.ai_call_id is not None else None,
            created_at=created_at,
            importance=Importance(float(row.importance)),
            deleted_at=from_stored_datetime(_as_iso(row.deleted_at)),
            retrieval_count=int(row.retrieval_count),
            last_retrieved_at=from_stored_datetime(_as_iso(row.last_retrieved_at)),
        )

    def to_params(self, entity: Memory) -> dict[str, Any]:
        """Build column values from a memory."""
        return {
            "id": int(entity.id),
            "account_id": int(entity.account_id),
            "contact_id": int(entity.contact_id) if entity.contact_id is not None else None,
            "category": entity.category.value,
            "key": entity.key.value,
            "value": entity.value,
            "confidence": entity.confidence.value,
            "source": entity.source.value,
            "proposal_id": int(entity.proposal_id) if entity.proposal_id is not None else None,
            "conversation_id": (
                int(entity.conversation_id) if entity.conversation_id is not None else None
            ),
            "ai_call_id": int(entity.ai_call_id) if entity.ai_call_id is not None else None,
            "importance": entity.importance.value,
            "created_at": entity.created_at,
            "deleted_at": entity.deleted_at,
            "retrieval_count": entity.retrieval_count,
            "last_retrieved_at": entity.last_retrieved_at,
        }


def _as_iso(value: Any) -> str | None:
    """Render a stored timestamp as ISO text, whichever form the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class SqlMemoryRepository(Repository[Memory], MemoryRepository):
    """Stores the approved facts of one account.

    Scoped at construction (ADR-039).
    """

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = MemoryMapper()
        self._paginator = KeysetPaginator(
            sort_column=memories.c.created_at,
            tiebreak_column=memories.c.id,
            sort_field=SORT_FIELD,
        )

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    def _scoped(self) -> Select[Any]:
        """Return the base query, already filtered to this account."""
        return select(memories).where(memories.c.account_id == int(self._account_id))

    async def add(self, memory: Memory) -> None:
        """Persist one approved fact."""
        self._require_own(memory, operation="add")
        await self.execute_write(
            insert(memories).values(self._mapper.to_params(memory)),
            operation="add_memory",
            conflict_message="That fact is already remembered.",
        )

    async def get(self, memory_id: MemoryId) -> Memory | None:
        """Return one of this account's memories, deleted or not."""
        row = await self.fetch_one(
            self._scoped().where(memories.c.id == int(memory_id)),
            operation="get_memory",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def get_by_proposal(self, proposal_id: MemoryProposalId) -> Memory | None:
        """Return the memory an accepted proposal produced, if it still exists."""
        row = await self.fetch_one(
            self._scoped().where(memories.c.proposal_id == int(proposal_id)),
            operation="get_memory_by_proposal",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def list_active(self, request: PageRequest) -> Page[Memory]:
        """Return one page of this account's live memories, newest first."""
        return await self.fetch_page(
            self._scoped().where(memories.c.deleted_at.is_(None)),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_memories",
        )

    async def list_for_contact(
        self, contact_id: ContactId | None, *, limit: int
    ) -> tuple[Memory, ...]:
        """Return the live memories about one contact, newest first.

        ``contact_id IS NULL`` rather than ``= NULL`` for the contactless case:
        the two are not the same in SQL, and the second silently matches
        nothing -- which would make a group chat's context permanently empty.
        """
        statement = (
            self._scoped()
            .where(memories.c.deleted_at.is_(None))
            .where(
                memories.c.contact_id.is_(None)
                if contact_id is None
                else memories.c.contact_id == int(contact_id)
            )
            .order_by(memories.c.created_at.desc(), memories.c.id.desc())
            .limit(limit)
        )
        rows = await self.fetch_all(statement, operation="list_memories_for_contact")
        return tuple(self._mapper.to_domain(row) for row in rows)

    async def mark_retrieved(self, memory_ids: Sequence[MemoryId], now: datetime) -> int:
        """Record that these memories were selected into a context.

        The count is incremented in SQL -- ``retrieval_count + 1`` -- rather
        than read and written back, so two contexts built at once cannot lose an
        increment.
        """
        if not memory_ids:
            return 0

        result = await self.execute_write(
            update(memories)
            .where(
                memories.c.id.in_([int(memory_id) for memory_id in memory_ids]),
                memories.c.account_id == int(self._account_id),
                memories.c.deleted_at.is_(None),
            )
            .values(
                retrieval_count=memories.c.retrieval_count + 1,
                last_retrieved_at=now,
            ),
            operation="mark_memories_retrieved",
            conflict_message="Those memories could not be marked as retrieved.",
        )
        return int(result.rowcount)

    async def delete(self, memory_id: MemoryId, now: datetime) -> bool:
        """Forget one memory, softly.

        The condition names ``deleted_at IS NULL``, so deleting twice writes
        nothing and returns ``False`` rather than moving the timestamp -- the
        moment a fact was forgotten is the first one.
        """
        result = await self.execute_write(
            update(memories)
            .where(
                memories.c.id == int(memory_id),
                memories.c.account_id == int(self._account_id),
                memories.c.deleted_at.is_(None),
            )
            .values(deleted_at=now),
            operation="delete_memory",
            conflict_message="That memory could not be deleted.",
        )
        return bool(result.rowcount)

    def _require_own(self, memory: Memory, *, operation: str) -> None:
        """Refuse a memory belonging to a different account."""
        if memory.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a memory of account {int(memory.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That memory belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "memory_account": int(memory.account_id),
                },
            )


def memory_repository(uow: UnitOfWork, account_id: AccountId) -> SqlMemoryRepository:
    """Build a memory repository scoped to one account.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlMemoryRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlMemoryRepository(uow, account_id)


__all__ = [
    "MemoryMapper",
    "SqlMemoryRepository",
    "memory_repository",
]
