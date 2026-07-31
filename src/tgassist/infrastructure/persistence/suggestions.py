"""Suggestion mapper and repository.

Stores one row per draft a model produced.

**One mutation, and it is a decision.** :meth:`SqlSuggestionRepository.decide`
is the single write that changes a stored suggestion, and its ``WHERE`` clause
names ``pending`` -- so a second decision, or two racing, cannot both succeed.
Nothing returns a suggestion to pending, so a decision cannot be undone
(ADR-062).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, insert, select, update

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    ConversationId,
    SuggestionId,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.suggestion import (
    ProposalType,
    Suggestion,
    SuggestionStatus,
)
from tgassist.domain.ports.suggestion_repository import SuggestionRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import suggestions
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "created_at"


class SuggestionMapper(EntityMapper[Suggestion]):
    """Converts between :class:`Suggestion` and its row."""

    def to_domain(self, row: Any) -> Suggestion:
        """Build a suggestion from a row."""
        created_at = from_stored_datetime(_as_iso(row.created_at))
        if created_at is None:  # pragma: no cover - schema forbids
            msg = "A suggestion row is missing its timestamp"
            raise DomainValidationError(msg, user_message="That suggestion record is incomplete.")

        return Suggestion(
            id=SuggestionId(row.id),
            account_id=AccountId(row.account_id),
            chat_id=ChatId(row.chat_id),
            conversation_id=(
                ConversationId(row.conversation_id) if row.conversation_id is not None else None
            ),
            ai_call_id=AiCallId(row.ai_call_id),
            proposal_type=ProposalType(row.proposal_type),
            title=row.title,
            description=row.description,
            payload=row.payload,
            status=SuggestionStatus(row.status),
            created_at=created_at,
            decided_at=from_stored_datetime(_as_iso(row.decided_at)),
        )

    def to_params(self, entity: Suggestion) -> dict[str, Any]:
        """Build column values from a suggestion.

        The payload is stored as it was serialised, not re-serialised: the
        entity sorted its keys once, and a second pass could produce different
        bytes for the same suggestion.
        """
        return {
            "id": int(entity.id),
            "account_id": int(entity.account_id),
            "chat_id": int(entity.chat_id),
            "conversation_id": (
                int(entity.conversation_id) if entity.conversation_id is not None else None
            ),
            "ai_call_id": int(entity.ai_call_id),
            "proposal_type": entity.proposal_type.value,
            "title": entity.title,
            "description": entity.description,
            "payload": entity.payload,
            "status": entity.status.value,
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


class SqlSuggestionRepository(Repository[Suggestion], SuggestionRepository):
    """Stores the drafts of one account.

    Scoped at construction (ADR-039). No ``update``, no ``delete``, and no
    ``execute``: accepting a suggestion records agreement, and there is nothing
    here that could act on one.
    """

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = SuggestionMapper()
        self._paginator = KeysetPaginator(
            sort_column=suggestions.c.created_at,
            tiebreak_column=suggestions.c.id,
            sort_field=SORT_FIELD,
        )

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    def _scoped(self) -> Select[Any]:
        """Return the base query, already filtered to this account."""
        return select(suggestions).where(suggestions.c.account_id == int(self._account_id))

    async def add(self, suggestion: Suggestion) -> None:
        """Persist one draft."""
        self._require_own(suggestion, operation="add")
        await self.execute_write(
            insert(suggestions).values(self._mapper.to_params(suggestion)),
            operation="add_suggestion",
            conflict_message="That suggestion has already been recorded.",
        )

    async def get(self, suggestion_id: SuggestionId) -> Suggestion | None:
        """Return one of this account's suggestions, decided or not."""
        row = await self.fetch_one(
            self._scoped().where(suggestions.c.id == int(suggestion_id)),
            operation="get_suggestion",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def list_pending(self, request: PageRequest) -> Page[Suggestion]:
        """Return one page of this account's undecided suggestions, newest first."""
        return await self.fetch_page(
            self._scoped().where(suggestions.c.status == SuggestionStatus.PENDING.value),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_pending_suggestions",
        )

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Suggestion]:
        """Return one page of one chat's suggestions, decided or not, newest first."""
        return await self.fetch_page(
            self._scoped().where(suggestions.c.chat_id == int(chat_id)),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_suggestions_by_chat",
        )

    async def decide(
        self, suggestion_id: SuggestionId, status: SuggestionStatus, now: datetime
    ) -> bool:
        """Record a decision about one suggestion, once.

        ``status = 'pending'`` is part of the condition rather than something
        checked first: a check-then-write could be overtaken between the two,
        and this cannot.
        """
        result = await self.execute_write(
            update(suggestions)
            .where(
                suggestions.c.id == int(suggestion_id),
                suggestions.c.account_id == int(self._account_id),
                suggestions.c.status == SuggestionStatus.PENDING.value,
            )
            .values(status=status.value, decided_at=now),
            operation="decide_suggestion",
            conflict_message="That suggestion could not be decided.",
        )
        return bool(result.rowcount)

    def _require_own(self, suggestion: Suggestion, *, operation: str) -> None:
        """Refuse a suggestion belonging to a different account."""
        if suggestion.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a suggestion of account "
                f"{int(suggestion.account_id)} through a repository scoped to account "
                f"{int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That suggestion belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "suggestion_account": int(suggestion.account_id),
                },
            )


def suggestion_repository(uow: UnitOfWork, account_id: AccountId) -> SqlSuggestionRepository:
    """Build a suggestion repository scoped to one account.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlSuggestionRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlSuggestionRepository(uow, account_id)


__all__ = [
    "SqlSuggestionRepository",
    "SuggestionMapper",
    "suggestion_repository",
]
