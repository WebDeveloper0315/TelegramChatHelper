"""Chat mapper and repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, insert, select, update

from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.model.chat import AiProcessingMode, Chat, ChatType
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_bool, from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import chats
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "created_at"


class ChatMapper(EntityMapper[Chat]):
    """Converts between :class:`Chat` and its row."""

    def to_domain(self, row: Any) -> Chat:
        """Build a chat from a row."""
        created_at = from_stored_datetime(_as_iso(row.created_at))
        updated_at = from_stored_datetime(_as_iso(row.updated_at))
        if created_at is None or updated_at is None:  # pragma: no cover - schema forbids
            msg = "A chat row is missing its timestamps"
            raise DomainValidationError(msg, user_message="That chat is incomplete.")

        return Chat(
            id=ChatId(row.id),
            account_id=AccountId(row.account_id),
            telegram_chat_id=TelegramChatId(row.telegram_chat_id),
            chat_type=ChatType(row.chat_type),
            contact_id=ContactId(row.contact_id) if row.contact_id is not None else None,
            title=row.title,
            sync_enabled=bool(from_stored_bool(row.sync_enabled)),
            ai_processing_mode=AiProcessingMode(row.ai_processing_mode),
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_params(self, entity: Chat) -> dict[str, Any]:
        """Build column values from a chat.

        Enumerations are stored as their string values rather than as ordinals,
        for the same reasons as everywhere else: an ordinal changes meaning if a
        member is inserted mid-enum, and it makes the file unreadable.
        """
        return {
            "id": int(entity.id),
            "account_id": int(entity.account_id),
            "telegram_chat_id": int(entity.telegram_chat_id),
            "chat_type": entity.chat_type.value,
            "contact_id": int(entity.contact_id) if entity.contact_id is not None else None,
            "title": entity.title,
            "sync_enabled": entity.sync_enabled,
            "ai_processing_mode": entity.ai_processing_mode.value,
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


class SqlChatRepository(Repository[Chat], ChatRepository):
    """Stores the chats of one account.

    Scoped at construction, so every query below is filtered by the account this
    repository was built for and there is no path that forgets to be (ADR-039).
    """

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = ChatMapper()
        self._paginator = KeysetPaginator(
            sort_column=chats.c.created_at,
            tiebreak_column=chats.c.id,
            sort_field=SORT_FIELD,
        )

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    def _scoped(self) -> Select[Any]:
        """Return the base query, already filtered to this account."""
        return select(chats).where(chats.c.account_id == int(self._account_id))

    async def add(self, chat: Chat) -> None:
        """Persist a new chat."""
        self._require_own(chat, operation="add")
        await self.execute_write(
            insert(chats).values(self._mapper.to_params(chat)),
            operation="add_chat",
            conflict_message=(
                "This account already has that chat, or already has a private "
                "chat with that contact."
            ),
        )

    async def get(self, chat_id: ChatId) -> Chat | None:
        """Return one of this account's chats, or ``None`` if absent."""
        row = await self.fetch_one(
            self._scoped().where(chats.c.id == int(chat_id)), operation="get_chat"
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def get_by_telegram_id(self, telegram_chat_id: TelegramChatId) -> Chat | None:
        """Return this account's chat with a Telegram identifier, or ``None``."""
        row = await self.fetch_one(
            self._scoped().where(chats.c.telegram_chat_id == int(telegram_chat_id)),
            operation="get_chat_by_telegram_id",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def get_private_with(self, contact_id: ContactId) -> Chat | None:
        """Return this account's private chat with a contact, or ``None``."""
        row = await self.fetch_one(
            self._scoped().where(chats.c.contact_id == int(contact_id)),
            operation="get_private_chat",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def list_chats(self, request: PageRequest) -> Page[Chat]:
        """Return one page of this account's chats."""
        return await self.fetch_page(
            self._scoped(),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_chats",
        )

    async def update(self, chat: Chat) -> None:
        """Persist a changed chat.

        Raises:
            RecordNotFoundError: If no row matches.
        """
        self._require_own(chat, operation="update")
        params = self._mapper.to_params(chat)
        # Identity, ownership and creation time belong to the original row.
        # Rewriting the contact would move the chat to another person, which is
        # a different chat rather than a change to this one.
        for immutable in ("id", "account_id", "telegram_chat_id", "contact_id", "created_at"):
            params.pop(immutable, None)

        result = await self.execute_write(
            update(chats).where(self._owns(chat.id)).values(**params),
            operation="update_chat",
        )
        if result.rowcount == 0:
            msg = f"No chat {int(chat.id)} in account {int(self._account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That chat was not found.",
                context={"chat_id": int(chat.id), "account_id": int(self._account_id)},
            )

    def _owns(self, chat_id: ChatId) -> ColumnElement[bool]:
        """Return the predicate matching one of *this account's* rows.

        The account clause is not redundant with the primary key: without it a
        caller holding another account's identifier would update that account's
        row, which is what the scope exists to prevent.
        """
        return (chats.c.id == int(chat_id)) & (chats.c.account_id == int(self._account_id))

    def _require_own(self, chat: Chat, *, operation: str) -> None:
        """Refuse a chat belonging to a different account."""
        if chat.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a chat of account {int(chat.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That chat belongs to a different account.",
                context={"scope": int(self._account_id), "chat_account": int(chat.account_id)},
            )


def chat_repository(uow: UnitOfWork, account_id: AccountId) -> SqlChatRepository:
    """Build a chat repository scoped to one account.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlChatRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlChatRepository(uow, account_id)


__all__ = [
    "ChatMapper",
    "SqlChatRepository",
    "chat_repository",
]
