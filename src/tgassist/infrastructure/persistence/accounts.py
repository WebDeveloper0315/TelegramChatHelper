"""Account mapper and repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, insert, select, update

from tgassist.domain.errors import RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_bool, from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import accounts
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "created_at"


class AccountMapper(EntityMapper[Account]):
    """Converts between :class:`Account` and its row.

    Both directions are total: every column the table declares is either
    written by :meth:`to_params` or is one this application does not own. The
    round-trip property is asserted by test, which is what catches a column
    added by a migration and forgotten here.
    """

    def to_domain(self, row: Any) -> Account:
        """Build an account from a row.

        SQLite has no native boolean or timezone-aware timestamp, so both are
        converted back through the shared helpers rather than by each mapper
        inventing its own reading of the stored form.
        """
        created_at = from_stored_datetime(_as_iso(row.created_at))
        updated_at = from_stored_datetime(_as_iso(row.updated_at))
        if created_at is None or updated_at is None:  # pragma: no cover - schema forbids
            msg = "An account row is missing its timestamps"
            raise ValueError(msg)

        return Account(
            id=AccountId(row.id),
            telegram_user_id=TelegramUserId(row.telegram_user_id),
            display_name=row.display_name,
            timezone=row.timezone,
            is_active=bool(from_stored_bool(row.is_active)),
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_params(self, entity: Account) -> dict[str, Any]:
        """Build column values from an account."""
        return {
            "id": int(entity.id),
            "telegram_user_id": int(entity.telegram_user_id),
            "display_name": entity.display_name,
            "timezone": entity.timezone,
            "is_active": entity.is_active,
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


class SqlAccountRepository(Repository[Account], AccountRepository):
    """Stores accounts in SQLite.

    Account is the ownership root, so this is the one repository that is not
    account-scoped: there is no outer scope to scope it to.
    """

    __slots__ = ("_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork) -> None:
        """Bind to the transaction this repository will run in."""
        super().__init__(uow)
        self._mapper = AccountMapper()
        self._paginator = KeysetPaginator(
            sort_column=accounts.c.created_at,
            tiebreak_column=accounts.c.id,
            sort_field=SORT_FIELD,
        )

    def _base(self) -> Select[Any]:
        return select(accounts)

    async def add(self, account: Account) -> None:
        """Persist a new account."""
        await self.execute_write(
            insert(accounts).values(self._mapper.to_params(account)),
            operation="add_account",
            conflict_message=("That account already exists, or another account is already active."),
        )

    async def get(self, account_id: AccountId) -> Account | None:
        """Return the account with this identifier, or ``None`` if absent."""
        row = await self.fetch_one(
            self._base().where(accounts.c.id == int(account_id)), operation="get_account"
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def get_by_telegram_id(self, telegram_user_id: TelegramUserId) -> Account | None:
        """Return the account for this Telegram user, or ``None`` if absent."""
        row = await self.fetch_one(
            self._base().where(accounts.c.telegram_user_id == int(telegram_user_id)),
            operation="get_account_by_telegram_id",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def get_active(self) -> Account | None:
        """Return the account currently being operated, or ``None`` if none is."""
        row = await self.fetch_one(
            self._base().where(accounts.c.is_active.is_(True)), operation="get_active_account"
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def list_accounts(self, request: PageRequest) -> Page[Account]:
        """Return one page of accounts."""
        return await self.fetch_page(
            self._base(),
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_accounts",
        )

    async def set_active(self, account_id: AccountId, now: datetime) -> Account:
        """Make this account the active one, deactivating any other.

        Deactivation happens **first**. The partial unique index permits only
        one active row, so activating before deactivating would violate it
        mid-statement and fail -- the ordering is a consequence of the invariant
        being enforced by the database rather than by hope.
        """
        account = await self.get(account_id)
        if account is None:
            msg = f"No account with identifier {int(account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That account was not found.",
                context={"account_id": int(account_id)},
            )
        if account.is_active:
            return account

        await self.execute_write(
            update(accounts)
            .where(accounts.c.is_active.is_(True))
            .values(is_active=False, updated_at=now),
            operation="deactivate_accounts",
        )
        activated = account.activated(now)
        await self.execute_write(
            update(accounts)
            .where(accounts.c.id == int(account_id))
            .values(is_active=True, updated_at=now),
            operation="activate_account",
            conflict_message="Another account is already active.",
        )
        return activated


def account_repository(uow: UnitOfWork) -> SqlAccountRepository:
    """Build an account repository bound to a unit of work.

    Takes the port type so it matches the uniform ``RepositoryFactory`` shape
    use cases declare, and narrows here: a SQL repository genuinely needs a SQL
    transaction to enlist in, and pairing it with an in-memory one would produce
    writes that go nowhere.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlAccountRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlAccountRepository(uow)


__all__ = [
    "AccountMapper",
    "SqlAccountRepository",
    "account_repository",
]
