"""Sync cursor mapper and repository.

Stores how far each chat's history backfill has got.

**This repository never commits**, which matters more here than anywhere else in
the persistence layer. The guarantee a cursor provides is that it moves in the
same transaction as the messages it accounts for (ADR-050); a repository that
committed on its own would advance the bookmark past messages that had not been
written, and the next run would resume from a point nothing reached.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update

from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    TelegramMessageId,
)
from tgassist.domain.model.sync_cursor import SyncCursor
from tgassist.domain.ports.sync_cursor_repository import SyncCursorRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import sync_cursors
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork


class SyncCursorMapper(EntityMapper[SyncCursor]):
    """Converts between :class:`SyncCursor` and its row."""

    def to_domain(self, row: Any) -> SyncCursor:
        """Build a cursor from a row."""
        updated_at = from_stored_datetime(_as_iso(row.updated_at))
        if updated_at is None:  # pragma: no cover - schema forbids
            msg = "A sync cursor row is missing its update time"
            raise DomainValidationError(msg, user_message="That bookmark is incomplete.")

        return SyncCursor(
            account_id=AccountId(row.account_id),
            chat_id=ChatId(row.chat_id),
            oldest_synced_message_id=_as_message_id(row.oldest_synced_message_id),
            newest_synced_message_id=_as_message_id(row.newest_synced_message_id),
            backfill_complete=bool(row.backfill_complete),
            backfill_horizon=from_stored_datetime(_as_iso(row.backfill_horizon)),
            last_sync_at=from_stored_datetime(_as_iso(row.last_sync_at)),
            updated_at=updated_at,
        )

    def to_params(self, entity: SyncCursor) -> dict[str, Any]:
        """Build column values from a cursor."""
        return {
            "chat_id": int(entity.chat_id),
            "account_id": int(entity.account_id),
            "oldest_synced_message_id": (
                int(entity.oldest_synced_message_id)
                if entity.oldest_synced_message_id is not None
                else None
            ),
            "newest_synced_message_id": (
                int(entity.newest_synced_message_id)
                if entity.newest_synced_message_id is not None
                else None
            ),
            "backfill_complete": entity.backfill_complete,
            "backfill_horizon": entity.backfill_horizon,
            "last_sync_at": entity.last_sync_at,
            "updated_at": entity.updated_at,
        }


def _as_iso(value: Any) -> str | None:
    """Render a stored timestamp as ISO text, whichever form the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_message_id(value: Any) -> TelegramMessageId | None:
    """Return a stored identifier as its domain type, or ``None``."""
    return TelegramMessageId(int(value)) if value is not None else None


class SqlSyncCursorRepository(Repository[SyncCursor], SyncCursorRepository):
    """Stores the synchronisation bookmarks of one account's chats.

    Scoped at construction (ADR-039). Every query is filtered by the account it
    was built for, so a cursor belonging to another account is not merely
    refused -- it is not visible.
    """

    __slots__ = ("_account_id", "_mapper")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = SyncCursorMapper()

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def get(self, chat_id: ChatId) -> SyncCursor | None:
        """Return one chat's cursor, or ``None`` if it has never been synced."""
        row = await self.fetch_one(
            select(sync_cursors).where(
                sync_cursors.c.chat_id == int(chat_id),
                sync_cursors.c.account_id == int(self._account_id),
            ),
            operation="get_sync_cursor",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def add(self, cursor: SyncCursor) -> None:
        """Persist a new cursor."""
        self._require_own(cursor, operation="add")
        await self.execute_write(
            insert(sync_cursors).values(self._mapper.to_params(cursor)),
            operation="add_sync_cursor",
            conflict_message="That chat already has a synchronisation bookmark.",
        )

    async def update(self, cursor: SyncCursor) -> None:
        """Persist an advanced cursor.

        Raises:
            RecordNotFoundError: If the chat has no cursor. A batch accounted
                for against a bookmark that does not exist would leave messages
                stored and nothing recording that they were.
        """
        self._require_own(cursor, operation="update")
        params = self._mapper.to_params(cursor)
        # The key, and the column that carries the scope. Rewriting either would
        # move a bookmark to another chat or another account.
        params.pop("chat_id", None)
        params.pop("account_id", None)

        result = await self.execute_write(
            update(sync_cursors)
            .where(
                sync_cursors.c.chat_id == int(cursor.chat_id),
                sync_cursors.c.account_id == int(self._account_id),
            )
            .values(**params),
            operation="update_sync_cursor",
        )
        if result.rowcount == 0:
            msg = f"Chat {int(cursor.chat_id)} has no sync cursor to update"
            raise RecordNotFoundError(
                msg,
                user_message="That chat has no synchronisation bookmark yet.",
                context={"chat_id": int(cursor.chat_id)},
            )

    async def save(self, cursor: SyncCursor) -> None:
        """Persist a cursor, whether or not the chat already had one.

        Read-then-write rather than an upsert statement. Both run inside one
        transaction on a single connection (ADR-034), so nothing can insert
        between the two -- and a portable ``INSERT ... ON CONFLICT`` would be
        dialect-specific for a concurrency this application does not have
        (ADR-016).
        """
        self._require_own(cursor, operation="save")
        if await self.get(cursor.chat_id) is None:
            await self.add(cursor)
        else:
            await self.update(cursor)

    def _require_own(self, cursor: SyncCursor, *, operation: str) -> None:
        """Refuse a cursor belonging to a different account.

        The scope makes cross-account reads impossible, but a caller could still
        hand this repository an entity built for another account. The composite
        foreign key would refuse the write, and this refuses it first with a
        message naming the problem rather than a column.
        """
        if cursor.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a sync cursor for account {int(cursor.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That synchronisation bookmark belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "cursor_account": int(cursor.account_id),
                },
            )


def sync_cursor_repository(uow: UnitOfWork, account_id: AccountId) -> SqlSyncCursorRepository:
    """Build a sync cursor repository scoped to one account.

    Matches ``ScopedRepositoryFactory``, so a use case can declare it as a
    dependency and supply the account once, inside its transaction.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlSyncCursorRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlSyncCursorRepository(uow, account_id)


__all__ = [
    "SqlSyncCursorRepository",
    "SyncCursorMapper",
    "sync_cursor_repository",
]
