"""In-memory message repository.

Shares one store across scoped instances, and models the constraints the schema
enforces: the composite foreign key to ``chats`` on ``(account_id, chat_id)``,
and the **partial** unique index on ``(account_id, chat_id,
telegram_message_id)``.

The partial index is the one most easily faked wrongly. A store treating
``telegram_message_id`` as unconditionally unique would reject the second
message with no external identifier -- and every message the CLI ingests has
none, so the fake would refuse the ordinary case while the real schema permits
it.
"""

from __future__ import annotations

from dataclasses import replace

from tests.fakes.pagination import paginate
from tgassist.domain.errors import ConstraintViolationError, DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    MessageId,
    TelegramMessageId,
)
from tgassist.domain.model.message import Message
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.message_repository import MessageRepository


class InMemoryMessageStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_chats", "messages")

    def __init__(self, chats: dict[int, int] | None = None) -> None:
        """Create a store.

        Args:
            chats: Chat identifier to owning account, standing in for the
                composite foreign key to ``chats``. ``None`` accepts any.
        """
        self.messages: dict[int, Message] = {}
        self._chats = chats

    def chat_belongs_to(self, chat_id: ChatId, account_id: AccountId) -> bool:
        """Report whether a chat exists **and** belongs to this account."""
        if self._chats is None:
            return True
        return self._chats.get(int(chat_id)) == int(account_id)

    def register_chat(self, chat_id: ChatId, account_id: AccountId) -> None:
        """Record a chat as existing under an account."""
        if self._chats is not None:
            self._chats[int(chat_id)] = int(account_id)

    def delete_chat(self, chat_id: ChatId) -> None:
        """Delete a chat and cascade to its messages, as the schema does."""
        if self._chats is not None:
            self._chats.pop(int(chat_id), None)
        for message_id in [key for key, value in self.messages.items() if value.chat_id == chat_id]:
            del self.messages[message_id]

    def delete_account(self, account_id: AccountId) -> None:
        """Delete an account and cascade to its messages, as the schema does."""
        if self._chats is not None:
            for chat_id in [key for key, value in self._chats.items() if value == int(account_id)]:
                del self._chats[chat_id]
        for message_id in [
            key for key, value in self.messages.items() if value.account_id == account_id
        ]:
            del self.messages[message_id]


class InMemoryMessageRepository(MessageRepository):
    """Stores one account's messages in a shared dictionary.

    Append-only, like the port and the SQL implementation: no update, no delete.
    """

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemoryMessageStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def add(self, message: Message) -> None:
        """Persist a message."""
        self._require_own(message, operation="add")
        if not self._store.chat_belongs_to(message.chat_id, message.account_id):
            msg = (
                f"Chat {int(message.chat_id)} does not exist under account "
                f"{int(message.account_id)}"
            )
            raise ConstraintViolationError(
                msg, user_message="That chat does not belong to this account."
            )
        if int(message.id) in self._store.messages:
            msg = f"Message {int(message.id)} already exists"
            raise ConstraintViolationError(msg, user_message="That message already exists.")
        # Partial: only messages carrying an external identifier can collide.
        if message.telegram_message_id is not None and (
            self._find(message.chat_id, message.telegram_message_id) is not None
        ):
            msg = (
                f"Chat {int(message.chat_id)} already holds Telegram message "
                f"{int(message.telegram_message_id)}"
            )
            raise ConstraintViolationError(
                msg, user_message="That message has already been ingested."
            )
        self._store.messages[int(message.id)] = message

    async def get(self, message_id: MessageId) -> Message | None:
        """Return one of this account's messages, or ``None`` if absent."""
        found = self._store.messages.get(int(message_id))
        if found is None or found.account_id != self._account_id:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def get_by_telegram_id(
        self, chat_id: ChatId, telegram_message_id: TelegramMessageId
    ) -> Message | None:
        """Return a message by its identifier in its chat, or ``None``."""
        found = self._find(chat_id, telegram_message_id)
        return replace(found) if found is not None else None

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Message]:
        """Return one page of a chat's messages."""
        return paginate(
            [
                message
                for message in self._store.messages.values()
                if message.account_id == self._account_id and message.chat_id == chat_id
            ],
            request,
            sort_key=lambda message: (message.sent_at, int(message.id)),
            identity=lambda message: int(message.id),
        )

    def _find(self, chat_id: ChatId, telegram_message_id: TelegramMessageId) -> Message | None:
        for message in self._store.messages.values():
            if (
                message.account_id == self._account_id
                and message.chat_id == chat_id
                and message.telegram_message_id == telegram_message_id
            ):
                return message
        return None

    def _require_own(self, message: Message, *, operation: str) -> None:
        """Refuse a message belonging to a different account."""
        if message.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a message of account {int(message.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That message belongs to a different account."
            )
