"""Session mapper and repository.

Stores where an account's encrypted TDLib store lives and what standing the
account has with Telegram. It stores the **name** of the store's encryption key,
never the key: that lives in the operating system credential store
(``SECURITY.md`` section 7).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select, update

from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.session import AuthorizationState, ConnectionState, Session
from tgassist.domain.ports.session_repository import SessionRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import telegram_sessions
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork


class SessionMapper(EntityMapper[Session]):
    """Converts between :class:`Session` and its row."""

    def to_domain(self, row: Any) -> Session:
        """Build a session from a row."""
        created_at = from_stored_datetime(_as_iso(row.created_at))
        updated_at = from_stored_datetime(_as_iso(row.updated_at))
        if created_at is None or updated_at is None:  # pragma: no cover - schema forbids
            msg = "A session row is missing its timestamps"
            raise DomainValidationError(msg, user_message="That session is incomplete.")

        return Session(
            account_id=AccountId(row.account_id),
            authorization_state=AuthorizationState(row.authorization_state),
            connection_state=ConnectionState(row.connection_state),
            session_path=Path(row.session_path),
            encryption_key_ref=row.encryption_key_ref,
            client_version=row.client_version,
            connected_at=from_stored_datetime(_as_iso(row.connected_at)),
            last_activity_at=from_stored_datetime(_as_iso(row.last_activity_at)),
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_params(self, entity: Session) -> dict[str, Any]:
        """Build column values from a session.

        The path is stored as text rather than as a structured value, because
        what it names is a directory on this machine and nothing queries inside
        it. States are stored as their string values for the reason given in
        ``UserProfileMapper``: an ordinal changes meaning if a member is ever
        inserted, and is unreadable to anyone opening the file.
        """
        return {
            "account_id": int(entity.account_id),
            "authorization_state": entity.authorization_state.value,
            "connection_state": entity.connection_state.value,
            "session_path": str(entity.session_path),
            "encryption_key_ref": entity.encryption_key_ref,
            "client_version": entity.client_version,
            "connected_at": entity.connected_at,
            "last_activity_at": entity.last_activity_at,
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


class SqlSessionRepository(Repository[Session], SessionRepository):
    """Stores the Telegram session record of one account.

    Scoped at construction (ADR-039). No method takes an account identifier, so
    every query this class issues is filtered by the account it was built for.
    """

    __slots__ = ("_account_id", "_mapper")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = SessionMapper()

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def get(self) -> Session | None:
        """Return this account's session, or ``None`` if it has none yet."""
        row = await self.fetch_one(
            select(telegram_sessions).where(
                telegram_sessions.c.account_id == int(self._account_id)
            ),
            operation="get_session",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def add(self, session: Session) -> None:
        """Persist a new session record."""
        self._require_own(session, operation="add")
        await self.execute_write(
            insert(telegram_sessions).values(self._mapper.to_params(session)),
            operation="add_session",
            conflict_message="That account already has a session.",
        )

    async def update(self, session: Session) -> None:
        """Replace this account's session record.

        Raises:
            RecordNotFoundError: If the account has no session record.
        """
        self._require_own(session, operation="update")
        params = self._mapper.to_params(session)
        # created_at belongs to the original row, and account_id is the key.
        # Rewriting either would make a session look newly created, or move it
        # to another account.
        params.pop("created_at", None)
        params.pop("account_id", None)

        result = await self.execute_write(
            update(telegram_sessions)
            .where(telegram_sessions.c.account_id == int(self._account_id))
            .values(**params),
            operation="update_session",
        )
        if result.rowcount == 0:
            msg = f"Account {int(self._account_id)} has no session to update"
            raise RecordNotFoundError(
                msg,
                user_message="That account has no session yet.",
                context={"account_id": int(self._account_id)},
            )

    def _require_own(self, session: Session, *, operation: str) -> None:
        """Refuse a session belonging to a different account.

        The scope makes cross-account *reads* impossible, but a caller could
        still hand this repository an entity built for another account. Writing
        it would point one account's record at another account's encrypted
        store, so it is refused here rather than trusted.
        """
        if session.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a session for account {int(session.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That session belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "session_account": int(session.account_id),
                },
            )


def session_repository(uow: UnitOfWork, account_id: AccountId) -> SqlSessionRepository:
    """Build a session repository scoped to one account.

    Matches ``ScopedRepositoryFactory``, so a use case can declare it as a
    dependency and supply the account once, inside its transaction.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlSessionRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlSessionRepository(uow, account_id)


__all__ = [
    "SessionMapper",
    "SqlSessionRepository",
    "session_repository",
]
