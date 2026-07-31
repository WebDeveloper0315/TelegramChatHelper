"""Conversation mapper and repository.

Stores bounded episodes of interaction, derived from messages.

The only repository in this system with a ``delete``, and the difference is not
an inconsistency: every other aggregate records something somebody decided, so
removing one destroys information nothing else holds. A Conversation records
something this application computed from messages that are still there, so a
stale one is not history -- it is a wrong answer, and leaving it would be worse
than removing it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, insert, select, update
from sqlalchemy import delete as delete_statement

from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ConversationId,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.conversation_repository import ConversationRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import conversations
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "started_at"


class ConversationMapper(EntityMapper[Conversation]):
    """Converts between :class:`Conversation` and its row."""

    def to_domain(self, row: Any) -> Conversation:
        """Build a conversation from a row."""
        started_at = from_stored_datetime(_as_iso(row.started_at))
        ended_at = from_stored_datetime(_as_iso(row.ended_at))
        created_at = from_stored_datetime(_as_iso(row.created_at))
        updated_at = from_stored_datetime(_as_iso(row.updated_at))
        if None in (started_at, ended_at, created_at, updated_at):  # pragma: no cover - schema
            msg = "A conversation row is missing its timestamps"
            raise DomainValidationError(msg, user_message="That conversation is incomplete.")

        return Conversation(
            id=ConversationId(row.id),
            account_id=AccountId(row.account_id),
            chat_id=ChatId(row.chat_id),
            started_at=started_at,  # type: ignore[arg-type]
            ended_at=ended_at,  # type: ignore[arg-type]
            message_count=int(row.message_count),
            created_at=created_at,  # type: ignore[arg-type]
            updated_at=updated_at,  # type: ignore[arg-type]
        )

    def to_params(self, entity: Conversation) -> dict[str, Any]:
        """Build column values from a conversation."""
        return {
            "id": int(entity.id),
            "account_id": int(entity.account_id),
            "chat_id": int(entity.chat_id),
            "started_at": entity.started_at,
            "ended_at": entity.ended_at,
            "message_count": entity.message_count,
            "created_at": entity.created_at,
            "updated_at": entity.updated_at,
        }


def _as_iso(value: Any) -> str | None:
    """Render a stored timestamp as ISO text, whichever form the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class SqlConversationRepository(Repository[Conversation], ConversationRepository):
    """Stores the conversations of one account.

    Scoped at construction (ADR-039). Every query is filtered by the account it
    was built for.
    """

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = ConversationMapper()
        self._paginator = KeysetPaginator(
            sort_column=conversations.c.started_at,
            tiebreak_column=conversations.c.id,
            sort_field=SORT_FIELD,
        )

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def get(self, conversation_id: ConversationId) -> Conversation | None:
        """Return one of this account's conversations, or ``None`` if absent."""
        row = await self.fetch_one(
            self._scoped().where(conversations.c.id == int(conversation_id)),
            operation="get_conversation",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Conversation]:
        """Return one page of a chat's conversations, newest first by default."""
        return await self.fetch_page(
            self._scoped().where(conversations.c.chat_id == int(chat_id)),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_conversations",
        )

    async def list_from(
        self, chat_id: ChatId, started_at: datetime | None = None
    ) -> tuple[Conversation, ...]:
        """Return a chat's conversations beginning at or after an instant, in order."""
        statement = self._scoped().where(conversations.c.chat_id == int(chat_id))
        if started_at is not None:
            statement = statement.where(conversations.c.started_at >= started_at)

        rows = await self.fetch_all(
            statement.order_by(conversations.c.started_at.asc(), conversations.c.id.asc()),
            operation="list_conversations_from",
        )
        return tuple(self._mapper.to_domain(row) for row in rows)

    async def latest_before(self, chat_id: ChatId, instant: datetime) -> Conversation | None:
        """Return the last conversation beginning at or before an instant.

        One row, served by the same index the listing uses -- read backwards
        rather than forwards, which costs nothing and saves a scan of everything
        newer.
        """
        row = await self.fetch_one(
            self._scoped()
            .where(
                conversations.c.chat_id == int(chat_id),
                conversations.c.started_at <= instant,
            )
            .order_by(conversations.c.started_at.desc(), conversations.c.id.desc())
            .limit(1),
            operation="latest_conversation_before",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def add(self, conversation: Conversation) -> None:
        """Persist a new conversation."""
        self._require_own(conversation, operation="add")
        await self.execute_write(
            insert(conversations).values(self._mapper.to_params(conversation)),
            operation="add_conversation",
            conflict_message="That chat already has a conversation beginning then.",
        )

    async def update(self, conversation: Conversation) -> None:
        """Persist a conversation whose extent changed.

        Raises:
            RecordNotFoundError: If no row matches.
        """
        self._require_own(conversation, operation="update")
        params = self._mapper.to_params(conversation)
        # The key, the scope, and the moment this conversation was first
        # computed. Rewriting any of them would move a conversation to another
        # chat or make a rebuild look like a first discovery.
        for column in ("id", "account_id", "chat_id", "created_at"):
            params.pop(column, None)

        result = await self.execute_write(
            update(conversations)
            .where(
                conversations.c.id == int(conversation.id),
                conversations.c.account_id == int(self._account_id),
            )
            .values(**params),
            operation="update_conversation",
            conflict_message="That chat already has a conversation beginning then.",
        )
        if result.rowcount == 0:
            msg = f"No conversation {int(conversation.id)} in account {int(self._account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That conversation was not found.",
                context={"conversation_id": int(conversation.id)},
            )

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove a conversation that no longer describes any messages.

        Absence is not an error: a pass that computed the same removal twice has
        made no mistake.
        """
        await self.execute_write(
            delete_statement(conversations).where(
                conversations.c.id == int(conversation_id),
                conversations.c.account_id == int(self._account_id),
            ),
            operation="delete_conversation",
        )

    def _scoped(self) -> Select[Any]:
        """Return the base query, already filtered to this account."""
        return select(conversations).where(conversations.c.account_id == int(self._account_id))

    def _require_own(self, conversation: Conversation, *, operation: str) -> None:
        """Refuse a conversation belonging to a different account."""
        if conversation.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a conversation of account "
                f"{int(conversation.account_id)} through a repository scoped to "
                f"account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That conversation belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "conversation_account": int(conversation.account_id),
                },
            )


def conversation_repository(uow: UnitOfWork, account_id: AccountId) -> SqlConversationRepository:
    """Build a conversation repository scoped to one account.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlConversationRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlConversationRepository(uow, account_id)


__all__ = [
    "ConversationMapper",
    "SqlConversationRepository",
    "conversation_repository",
]
