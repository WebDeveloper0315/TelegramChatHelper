"""In-memory chat repository.

Shares one store across scoped instances, so account isolation has to do real
work rather than passing by having nothing to leak.

The store also stands in for the constraints the schema enforces: the foreign
key to ``accounts``, the **composite** foreign key to ``contacts`` on
``(account_id, contact_id)``, the unique Telegram chat identifier, and the
partial unique index giving a contact at most one private chat. A fake that
accepted rows the schema refuses would make every use-case test built on it a
false positive -- and the composite key in particular is the guarantee this
milestone exists to establish, so a fake that ignored it would be checking the
wrong thing most convincingly.
"""

from __future__ import annotations

from dataclasses import replace

from tests.fakes.pagination import paginate
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.chat_repository import ChatRepository


class InMemoryChatStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_contacts", "_known_accounts", "chats")

    def __init__(
        self,
        known_accounts: set[int] | None = None,
        contacts: dict[int, int] | None = None,
    ) -> None:
        """Create a store.

        Args:
            known_accounts: Accounts that exist, standing in for the foreign key
                to ``accounts``. ``None`` accepts any.
            contacts: Contact identifier to owning account, standing in for the
                composite foreign key to ``contacts``. ``None`` accepts any.
        """
        self.chats: dict[int, Chat] = {}
        self._known_accounts = known_accounts
        self._contacts = contacts

    def account_exists(self, account_id: AccountId) -> bool:
        """Report whether the referenced account exists."""
        if self._known_accounts is None:
            return True
        return int(account_id) in self._known_accounts

    def contact_belongs_to(self, contact_id: ContactId, account_id: AccountId) -> bool:
        """Report whether a contact exists **and** belongs to this account.

        Both halves, because that is what the composite foreign key checks. A
        version testing only existence would accept exactly the cross-account
        row the constraint was added to make impossible.
        """
        if self._contacts is None:
            return True
        return self._contacts.get(int(contact_id)) == int(account_id)

    def register_contact(self, contact_id: ContactId, account_id: AccountId) -> None:
        """Record a contact as existing under an account."""
        if self._contacts is not None:
            self._contacts[int(contact_id)] = int(account_id)

    def delete_account(self, account_id: AccountId) -> None:
        """Delete an account and cascade to its chats, as the schema does."""
        if self._known_accounts is not None:
            self._known_accounts.discard(int(account_id))
        for chat_id in [key for key, value in self.chats.items() if value.account_id == account_id]:
            del self.chats[chat_id]

    def delete_contact(self, contact_id: ContactId) -> None:
        """Delete a contact and cascade to its private chat, as the schema does."""
        if self._contacts is not None:
            self._contacts.pop(int(contact_id), None)
        for chat_id in [key for key, value in self.chats.items() if value.contact_id == contact_id]:
            del self.chats[chat_id]


class InMemoryChatRepository(ChatRepository):
    """Stores one account's chats in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemoryChatStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def add(self, chat: Chat) -> None:
        """Persist a new chat."""
        self._require_own(chat, operation="add")
        if not self._store.account_exists(chat.account_id):
            msg = f"No account {int(chat.account_id)} to own this chat"
            raise ConstraintViolationError(msg, user_message="That account does not exist.")
        if chat.contact_id is not None and not self._store.contact_belongs_to(
            chat.contact_id, chat.account_id
        ):
            msg = (
                f"Contact {int(chat.contact_id)} does not exist under account "
                f"{int(chat.account_id)}"
            )
            raise ConstraintViolationError(
                msg, user_message="That contact does not belong to this account."
            )
        if int(chat.id) in self._store.chats:
            msg = f"Chat {int(chat.id)} already exists"
            raise ConstraintViolationError(msg, user_message="That chat already exists.")
        if self._by_telegram_id(chat.telegram_chat_id) is not None:
            msg = f"Telegram chat {int(chat.telegram_chat_id)} is already recorded"
            raise ConstraintViolationError(msg, user_message="This account already has that chat.")
        if chat.contact_id is not None and self._private_with(chat.contact_id) is not None:
            msg = f"Contact {int(chat.contact_id)} already has a private chat"
            raise ConstraintViolationError(
                msg, user_message="That contact already has a private chat."
            )
        self._store.chats[int(chat.id)] = chat

    async def get(self, chat_id: ChatId) -> Chat | None:
        """Return one of this account's chats, or ``None`` if absent."""
        found = self._store.chats.get(int(chat_id))
        if found is None or found.account_id != self._account_id:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def get_by_telegram_id(self, telegram_chat_id: TelegramChatId) -> Chat | None:
        """Return this account's chat with a Telegram identifier, or ``None``."""
        found = self._by_telegram_id(telegram_chat_id)
        return replace(found) if found is not None else None

    async def get_private_with(self, contact_id: ContactId) -> Chat | None:
        """Return this account's private chat with a contact, or ``None``."""
        found = self._private_with(contact_id)
        return replace(found) if found is not None else None

    async def list_chats(self, request: PageRequest) -> Page[Chat]:
        """Return one page of this account's chats."""
        return paginate(
            [chat for chat in self._store.chats.values() if chat.account_id == self._account_id],
            request,
            sort_key=lambda chat: (chat.created_at, int(chat.id)),
            identity=lambda chat: int(chat.id),
        )

    async def update(self, chat: Chat) -> None:
        """Persist a changed chat."""
        self._require_own(chat, operation="update")
        existing = self._store.chats.get(int(chat.id))
        if existing is None or existing.account_id != self._account_id:
            msg = f"No chat {int(chat.id)} in account {int(self._account_id)}"
            raise RecordNotFoundError(msg, user_message="That chat was not found.")
        # Identity, ownership and creation time belong to the original row.
        self._store.chats[int(chat.id)] = replace(
            chat,
            telegram_chat_id=existing.telegram_chat_id,
            contact_id=existing.contact_id,
            created_at=existing.created_at,
        )

    def _by_telegram_id(self, telegram_chat_id: TelegramChatId) -> Chat | None:
        for chat in self._store.chats.values():
            if chat.account_id == self._account_id and chat.telegram_chat_id == telegram_chat_id:
                return chat
        return None

    def _private_with(self, contact_id: ContactId) -> Chat | None:
        for chat in self._store.chats.values():
            if chat.account_id == self._account_id and chat.contact_id == contact_id:
                return chat
        return None

    def _require_own(self, chat: Chat, *, operation: str) -> None:
        """Refuse a chat belonging to a different account."""
        if chat.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a chat of account {int(chat.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That chat belongs to a different account."
            )
