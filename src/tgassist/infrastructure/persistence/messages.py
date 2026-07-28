"""Message mapper and repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, insert, select

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    MessageId,
    TelegramMessageId,
)
from tgassist.domain.model.message import Message, MessageType, SenderKind
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.message_repository import MessageRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import messages
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "sent_at"


class MessageMapper(EntityMapper[Message]):
    """Converts between :class:`Message` and its row."""

    def to_domain(self, row: Any) -> Message:
        """Build a message from a row."""
        sent_at = from_stored_datetime(_as_iso(row.sent_at))
        ingested_at = from_stored_datetime(_as_iso(row.ingested_at))
        if sent_at is None or ingested_at is None:  # pragma: no cover - schema forbids
            msg = "A message row is missing its timestamps"
            raise DomainValidationError(msg, user_message="That message is incomplete.")

        return Message(
            id=MessageId(row.id),
            account_id=AccountId(row.account_id),
            chat_id=ChatId(row.chat_id),
            telegram_message_id=(
                TelegramMessageId(row.telegram_message_id)
                if row.telegram_message_id is not None
                else None
            ),
            sender_kind=SenderKind(row.sender_kind),
            message_type=MessageType(row.message_type),
            text=row.text,
            sent_at=sent_at,
            ingested_at=ingested_at,
        )

    def to_params(self, entity: Message) -> dict[str, Any]:
        """Build column values from a message."""
        return {
            "id": int(entity.id),
            "account_id": int(entity.account_id),
            "chat_id": int(entity.chat_id),
            "telegram_message_id": (
                int(entity.telegram_message_id) if entity.telegram_message_id is not None else None
            ),
            "sender_kind": entity.sender_kind.value,
            "message_type": entity.message_type.value,
            "text": entity.text,
            "sent_at": entity.sent_at,
            "ingested_at": entity.ingested_at,
        }


def _as_iso(value: Any) -> str | None:
    """Render a stored timestamp as ISO text, whichever form the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class SqlMessageRepository(Repository[Message], MessageRepository):
    """Stores the messages of one account.

    Scoped at construction (ADR-039), and **append-only**: there is no update or
    delete method, because a message is an immutable factual record.
    """

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = MessageMapper()
        self._paginator = KeysetPaginator(
            sort_column=messages.c.sent_at,
            tiebreak_column=messages.c.id,
            sort_field=SORT_FIELD,
        )

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    def _scoped(self) -> Select[Any]:
        """Return the base query, already filtered to this account."""
        return select(messages).where(messages.c.account_id == int(self._account_id))

    async def add(self, message: Message) -> None:
        """Persist a message."""
        self._require_own(message, operation="add")
        await self.execute_write(
            insert(messages).values(self._mapper.to_params(message)),
            operation="add_message",
            conflict_message="That message has already been ingested.",
        )

    async def get(self, message_id: MessageId) -> Message | None:
        """Return one of this account's messages, or ``None`` if absent."""
        row = await self.fetch_one(
            self._scoped().where(messages.c.id == int(message_id)), operation="get_message"
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def get_by_telegram_id(
        self, chat_id: ChatId, telegram_message_id: TelegramMessageId
    ) -> Message | None:
        """Return a message by its identifier in its chat, or ``None``."""
        row = await self.fetch_one(
            self._scoped()
            .where(messages.c.chat_id == int(chat_id))
            .where(messages.c.telegram_message_id == int(telegram_message_id)),
            operation="get_message_by_telegram_id",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Message]:
        """Return one page of a chat's messages."""
        return await self.fetch_page(
            self._scoped().where(messages.c.chat_id == int(chat_id)),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_messages",
        )

    def _require_own(self, message: Message, *, operation: str) -> None:
        """Refuse a message belonging to a different account.

        The composite foreign key already makes a message in the wrong account's
        chat unwritable. This catches the case the key cannot: a message whose
        ``account_id`` disagrees with this repository's scope but whose chat
        happens to belong to that other account, which would file a real message
        under the wrong owner.
        """
        if message.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a message of account {int(message.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That message belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "message_account": int(message.account_id),
                },
            )


def message_repository(uow: UnitOfWork, account_id: AccountId) -> SqlMessageRepository:
    """Build a message repository scoped to one account.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlMessageRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlMessageRepository(uow, account_id)


__all__ = [
    "MessageMapper",
    "SqlMessageRepository",
    "message_repository",
]
