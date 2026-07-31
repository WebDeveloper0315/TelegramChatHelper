"""In-memory sync cursor repository.

Written independently of the SQL implementation and sharing one store across
scoped instances, for the reason the other fakes do: a fake holding only its own
account's rows would pass an isolation test by having nothing to leak.

The store also stands in for the **composite** foreign key to ``chats``, which
is the constraint that matters most here. A fake that accepted a cursor naming
another account's chat would make every test built on it agree with a schema
that refuses exactly that (ADR-043).
"""

from __future__ import annotations

from dataclasses import replace

from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.identifiers import AccountId, ChatId
from tgassist.domain.model.sync_cursor import SyncCursor
from tgassist.domain.ports.sync_cursor_repository import SyncCursorRepository


class InMemorySyncCursorStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_chats", "cursors")

    def __init__(self, chats: dict[int, int] | None = None) -> None:
        """Create a store.

        Args:
            chats: Chat identifier to owning account, standing in for the
                composite foreign key to ``chats``. ``None`` accepts any.
        """
        self.cursors: dict[int, SyncCursor] = {}
        self._chats = chats

    def chat_belongs_to(self, chat_id: ChatId, account_id: AccountId) -> bool:
        """Report whether a chat exists **and** belongs to this account.

        Both halves, because that is what the composite foreign key checks. A
        version testing only existence would accept exactly the cross-account
        row the constraint was added to make impossible.
        """
        if self._chats is None:
            return True
        return self._chats.get(int(chat_id)) == int(account_id)

    def register_chat(self, chat_id: ChatId, account_id: AccountId) -> None:
        """Record a chat as existing under an account."""
        if self._chats is not None:
            self._chats[int(chat_id)] = int(account_id)

    def delete_chat(self, chat_id: ChatId) -> None:
        """Delete a chat and cascade to its cursor, as the schema does."""
        if self._chats is not None:
            self._chats.pop(int(chat_id), None)
        self.cursors.pop(int(chat_id), None)


class InMemorySyncCursorRepository(SyncCursorRepository):
    """Stores one account's synchronisation bookmarks in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemorySyncCursorStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def get(self, chat_id: ChatId) -> SyncCursor | None:
        """Return one chat's cursor, or ``None`` if it has never been synced."""
        found = self._store.cursors.get(int(chat_id))
        if found is None or found.account_id != self._account_id:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def add(self, cursor: SyncCursor) -> None:
        """Persist a new cursor."""
        self._require_own(cursor, operation="add")
        if not self._store.chat_belongs_to(cursor.chat_id, cursor.account_id):
            msg = f"No chat {int(cursor.chat_id)} in account {int(cursor.account_id)}"
            raise ConstraintViolationError(
                msg, user_message="That chat does not exist in this account."
            )
        if int(cursor.chat_id) in self._store.cursors:
            msg = f"Chat {int(cursor.chat_id)} already has a sync cursor"
            raise ConstraintViolationError(
                msg, user_message="That chat already has a synchronisation bookmark."
            )
        self._store.cursors[int(cursor.chat_id)] = cursor

    async def update(self, cursor: SyncCursor) -> None:
        """Persist an advanced cursor."""
        self._require_own(cursor, operation="update")
        if await self.get(cursor.chat_id) is None:
            msg = f"Chat {int(cursor.chat_id)} has no sync cursor to update"
            raise RecordNotFoundError(
                msg, user_message="That chat has no synchronisation bookmark yet."
            )
        self._store.cursors[int(cursor.chat_id)] = cursor

    async def save(self, cursor: SyncCursor) -> None:
        """Persist a cursor, whether or not the chat already had one."""
        self._require_own(cursor, operation="save")
        if await self.get(cursor.chat_id) is None:
            await self.add(cursor)
        else:
            await self.update(cursor)

    def _require_own(self, cursor: SyncCursor, *, operation: str) -> None:
        """Refuse a cursor belonging to a different account."""
        if cursor.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a sync cursor for account {int(cursor.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That synchronisation bookmark belongs to a different account.",
            )
