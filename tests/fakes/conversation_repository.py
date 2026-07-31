"""In-memory conversation repository.

Written independently of the SQL implementation and sharing one store across
scoped instances, for the reason the other fakes do: a fake holding only its own
account's rows would pass an isolation test by having nothing to leak.

The store also stands in for the two constraints that matter here -- the
composite foreign key to ``chats``, and the unique
``(account_id, chat_id, started_at)`` that makes overlapping conversations
unrepresentable. A fake that accepted either would make every segmentation test
built on it agree with a schema that refuses them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from tests.fakes.pagination import paginate
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import AccountId, ChatId, ConversationId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.conversation_repository import ConversationRepository


class InMemoryConversationStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_chats", "conversations")

    def __init__(self, chats: dict[int, int] | None = None) -> None:
        """Create a store.

        Args:
            chats: Chat identifier to owning account, standing in for the
                composite foreign key to ``chats``. ``None`` accepts any.
        """
        self.conversations: dict[int, Conversation] = {}
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
        """Delete a chat and cascade to its conversations, as the schema does."""
        if self._chats is not None:
            self._chats.pop(int(chat_id), None)
        for identifier in [
            key for key, value in self.conversations.items() if value.chat_id == chat_id
        ]:
            del self.conversations[identifier]


class InMemoryConversationRepository(ConversationRepository):
    """Stores one account's conversations in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemoryConversationStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def get(self, conversation_id: ConversationId) -> Conversation | None:
        """Return one of this account's conversations, or ``None`` if absent."""
        found = self._store.conversations.get(int(conversation_id))
        if found is None or found.account_id != self._account_id:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Conversation]:
        """Return one page of a chat's conversations, newest first by default."""
        return paginate(
            [
                replace(conversation)
                for conversation in self._store.conversations.values()
                if conversation.account_id == self._account_id and conversation.chat_id == chat_id
            ],
            request,
            sort_key=lambda conversation: (conversation.started_at, int(conversation.id)),
            identity=lambda conversation: int(conversation.id),
        )

    async def list_from(
        self, chat_id: ChatId, started_at: datetime | None = None
    ) -> tuple[Conversation, ...]:
        """Return a chat's conversations beginning at or after an instant, in order."""
        found = [
            replace(conversation)
            for conversation in self._store.conversations.values()
            if conversation.account_id == self._account_id
            and conversation.chat_id == chat_id
            and (started_at is None or conversation.started_at >= started_at)
        ]
        return tuple(sorted(found, key=lambda c: (c.started_at, int(c.id))))

    async def latest_before(self, chat_id: ChatId, instant: datetime) -> Conversation | None:
        """Return the last conversation beginning at or before an instant."""
        candidates = [
            conversation
            for conversation in self._store.conversations.values()
            if conversation.account_id == self._account_id
            and conversation.chat_id == chat_id
            and conversation.started_at <= instant
        ]
        if not candidates:
            return None
        return replace(max(candidates, key=lambda c: (c.started_at, int(c.id))))

    async def add(self, conversation: Conversation) -> None:
        """Persist a new conversation."""
        self._require_own(conversation, operation="add")
        if not self._store.chat_belongs_to(conversation.chat_id, conversation.account_id):
            msg = f"No chat {int(conversation.chat_id)} in account {int(conversation.account_id)}"
            raise ConstraintViolationError(
                msg, user_message="That chat does not exist in this account."
            )
        if int(conversation.id) in self._store.conversations:
            msg = f"Conversation {int(conversation.id)} already exists"
            raise ConstraintViolationError(msg, user_message="That conversation already exists.")
        self._require_free_start(conversation)
        self._store.conversations[int(conversation.id)] = conversation

    async def update(self, conversation: Conversation) -> None:
        """Persist a conversation whose extent changed."""
        self._require_own(conversation, operation="update")
        existing = await self.get(conversation.id)
        if existing is None:
            msg = f"No conversation {int(conversation.id)} in account {int(self._account_id)}"
            raise RecordNotFoundError(msg, user_message="That conversation was not found.")
        self._require_free_start(conversation)
        # created_at belongs to the original row, exactly as in SQL.
        self._store.conversations[int(conversation.id)] = replace(
            conversation, created_at=existing.created_at
        )

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove a conversation, tolerating one that is already gone."""
        found = self._store.conversations.get(int(conversation_id))
        if found is not None and found.account_id == self._account_id:
            del self._store.conversations[int(conversation_id)]

    def _require_free_start(self, conversation: Conversation) -> None:
        """Refuse a second conversation beginning at the same instant in one chat.

        The unique index, which is what makes overlapping conversations
        unrepresentable rather than merely undesirable.
        """
        for other in self._store.conversations.values():
            if (
                other.id != conversation.id
                and other.account_id == conversation.account_id
                and other.chat_id == conversation.chat_id
                and other.started_at == conversation.started_at
            ):
                msg = (
                    f"Chat {int(conversation.chat_id)} already has a conversation "
                    f"beginning at {conversation.started_at}"
                )
                raise ConstraintViolationError(
                    msg, user_message="That chat already has a conversation beginning then."
                )

    def _require_own(self, conversation: Conversation, *, operation: str) -> None:
        """Refuse a conversation belonging to a different account."""
        if conversation.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a conversation of account "
                f"{int(conversation.account_id)} through a repository scoped to "
                f"account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That conversation belongs to a different account."
            )
