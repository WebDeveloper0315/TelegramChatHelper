"""What synchronisation has achieved so far, read from the database.

A query, and deliberately only a query. It reads stored state -- the session's
two axes, and each chat's bookmark -- and touches neither Telegram nor a running
synchronisation.

That is not a limitation working around a missing feature; it is the honest
scope. ``tgassist sync status`` runs in its own process, so a live run happening
in another one is not something it could observe without inventing a control
channel between processes. What it *can* answer is the question a person
actually asks: how much of my history is here, and how far back does it go.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tgassist.application.use_cases.account_scope import require_account
from tgassist.domain.model.identifiers import AccountId, ChatId, TelegramMessageId
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.session import AuthorizationState, ConnectionState
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.session_repository import SessionRepository
from tgassist.domain.ports.sync_cursor_repository import SyncCursorRepository
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class ChatSyncStatus:
    """Where one chat stands.

    Attributes:
        chat_id: The local identifier.
        title: What to call it -- the chat's own title, or the contact's name
            for a private chat, resolved by the caller that has both.
        sync_enabled: Whether synchronisation is switched on for it.
        backfill_complete: Whether there is nothing further back to fetch for
            the horizon the last run used.
        oldest_synced_message_id: The floor of the stored range, or ``None``.
        newest_synced_message_id: Its ceiling, or ``None``.
        last_sync_at: When a batch last committed for this chat, or ``None``.
    """

    chat_id: ChatId
    title: str
    sync_enabled: bool
    backfill_complete: bool
    oldest_synced_message_id: TelegramMessageId | None
    newest_synced_message_id: TelegramMessageId | None
    last_sync_at: datetime | None

    @property
    def has_synced(self) -> bool:
        """Whether anything has been stored for this chat."""
        return self.oldest_synced_message_id is not None

    @property
    def state(self) -> str:
        """A one-word summary, for display."""
        if not self.sync_enabled:
            return "off"
        if not self.has_synced:
            return "pending"
        return "complete" if self.backfill_complete else "partial"


@dataclass(frozen=True, slots=True)
class SyncStatusReport:
    """Where an account's synchronisation stands.

    Attributes:
        account_id: Whose status this is.
        authorization_state: Whether the stored session has credentials, as of
            the last run. Read from the database, not from Telegram: this
            command opens no connection.
        connection_state: What the last run recorded. Almost always ``offline``
            here, because a run that ended tidily said so.
        chats: One entry per chat, in listing order.
    """

    account_id: AccountId
    authorization_state: AuthorizationState | None
    connection_state: ConnectionState | None
    chats: tuple[ChatSyncStatus, ...]

    @property
    def synchronised(self) -> int:
        """How many chats have a completed backfill."""
        return sum(1 for chat in self.chats if chat.sync_enabled and chat.backfill_complete)

    @property
    def pending(self) -> int:
        """How many chats are switched on but not finished."""
        return sum(1 for chat in self.chats if chat.sync_enabled and not chat.backfill_complete)

    @property
    def is_current(self) -> bool:
        """Whether every chat that syncs has finished its backfill."""
        return self.pending == 0


class GetSyncStatus:
    """Reports what synchronisation has stored, without contacting Telegram."""

    __slots__ = ("_accounts", "_chats", "_cursors", "_sessions", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        cursors: ScopedRepositoryFactory[SyncCursorRepository],
        chats: ScopedRepositoryFactory[ChatRepository],
        sessions: ScopedRepositoryFactory[SessionRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._cursors = cursors
        self._chats = chats
        self._sessions = sessions
        self._accounts = accounts

    async def execute(self, account_id: AccountId | None = None) -> SyncStatusReport:
        """Return where this account's synchronisation stands.

        One transaction for the whole report, so every chat's bookmark is read
        from the same instant. A report assembled across several would describe
        a state the database was never in.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
        """
        async with self._unit_of_work() as uow:
            account = await require_account(self._accounts(uow), account_id)
            session = await self._sessions(uow, account.id).get()
            chats = self._chats(uow, account.id)
            cursors = self._cursors(uow, account.id)

            found: list[ChatSyncStatus] = []
            request = PageRequest()
            while True:
                page = await chats.list_chats(request)
                for chat in page.items:
                    cursor = await cursors.get(chat.id)
                    found.append(
                        ChatSyncStatus(
                            chat_id=chat.id,
                            title=chat.title if chat.title is not None else _private_label(chat.id),
                            sync_enabled=chat.sync_enabled,
                            backfill_complete=(
                                cursor.backfill_complete if cursor is not None else False
                            ),
                            oldest_synced_message_id=(
                                cursor.oldest_synced_message_id if cursor is not None else None
                            ),
                            newest_synced_message_id=(
                                cursor.newest_synced_message_id if cursor is not None else None
                            ),
                            last_sync_at=cursor.last_sync_at if cursor is not None else None,
                        )
                    )
                if page.next_cursor is None:
                    break
                request = PageRequest(cursor=page.next_cursor, limit=request.limit)

        return SyncStatusReport(
            account_id=account.id,
            authorization_state=session.authorization_state if session is not None else None,
            connection_state=session.connection_state if session is not None else None,
            chats=tuple(found),
        )


def _private_label(chat_id: ChatId) -> str:
    """Name a private chat, whose title belongs to its contact.

    The contact's name would be better and would cost a second lookup per chat
    for a column this report only prints. The identifier is what the other
    commands take, so it is at least the useful thing to see.
    """
    return f"private chat {int(chat_id)}"


__all__ = [
    "ChatSyncStatus",
    "GetSyncStatus",
    "SyncStatusReport",
]
