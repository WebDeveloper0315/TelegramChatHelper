"""Account use cases.

Four operations, each owning one transaction. They are separate classes rather
than methods on a service, because a class states its dependencies in its
constructor -- ``CreateAccount`` needs a clock and an identifier generator,
``ListAccounts`` needs neither, and a shared ``AccountService`` would hide that
difference behind a constructor demanding everything for everyone.
"""

from __future__ import annotations

from dataclasses import dataclass

from tgassist.domain.errors import ConflictError
from tgassist.domain.events import AccountActivated, AccountCreated
from tgassist.domain.model.account import DEFAULT_TIMEZONE, Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.repository import RepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class CreateAccountRequest:
    """What the caller supplies to create an account."""

    telegram_user_id: int
    display_name: str
    timezone: str = DEFAULT_TIMEZONE


class CreateAccount:
    """Creates an account, activating it if it is the first."""

    __slots__ = ("_accounts", "_clock", "_ids", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._accounts = accounts
        self._clock = clock
        self._ids = ids

    async def execute(self, request: CreateAccountRequest) -> Account:
        """Create and persist an account.

        The first account becomes active automatically. Requiring an explicit
        activation step would leave a fresh installation in a state where
        nothing works and the reason is not obvious.

        Raises:
            ConflictError: If an account for this Telegram user already exists.
                Checked before writing so the caller gets a message naming the
                conflict, rather than a unique-constraint violation naming a
                column.
            ValueError: If the request violates an Account invariant.
        """
        async with self._unit_of_work() as uow:
            accounts = self._accounts(uow)
            telegram_user_id = TelegramUserId(request.telegram_user_id)

            existing = await accounts.get_by_telegram_id(telegram_user_id)
            if existing is not None:
                msg = f"An account for Telegram user {request.telegram_user_id} already exists"
                raise ConflictError(
                    msg,
                    user_message="That Telegram account has already been added.",
                    context={"telegram_user_id": request.telegram_user_id},
                )

            is_first = await accounts.get_active() is None
            account = Account.create(
                account_id=AccountId(self._ids.new_id()),
                telegram_user_id=telegram_user_id,
                display_name=request.display_name,
                timezone=request.timezone,
                now=self._clock.now(),
                is_active=is_first,
            )
            await accounts.add(account)

            uow.add_event(AccountCreated(account_id=int(account.id), is_active=account.is_active))
            await uow.commit()

        return account


class GetAccount:
    """Looks an account up by identifier, or returns the active one."""

    __slots__ = ("_accounts", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._accounts = accounts

    async def execute(self, account_id: int | None = None) -> Account | None:
        """Return an account, or ``None`` if it does not exist.

        Args:
            account_id: Identifier to look up. ``None`` returns the active
                account, which is what a caller usually wants and saves it
                having to ask which one that is.
        """
        async with self._unit_of_work() as uow:
            accounts = self._accounts(uow)
            if account_id is None:
                return await accounts.get_active()
            return await accounts.get(AccountId(account_id))


class ListAccounts:
    """Returns a page of accounts."""

    __slots__ = ("_accounts", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._accounts = accounts

    async def execute(self, request: PageRequest | None = None) -> Page[Account]:
        """Return one page of accounts, newest first."""
        async with self._unit_of_work() as uow:
            return await self._accounts(uow).list_accounts(request or PageRequest())


class SetActiveAccount:
    """Switches which account the application operates."""

    __slots__ = ("_accounts", "_clock", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._accounts = accounts
        self._clock = clock

    async def execute(self, account_id: int) -> Account:
        """Make an account active, deactivating any other.

        Both writes happen in one transaction, so the single-active invariant is
        never briefly violated and never left broken by a failure between them.

        Raises:
            RecordNotFoundError: If no account has this identifier.
        """
        async with self._unit_of_work() as uow:
            accounts = self._accounts(uow)
            account = await accounts.set_active(AccountId(account_id), self._clock.now())
            uow.add_event(AccountActivated(account_id=int(account.id)))
            await uow.commit()

        return account


def collect_and_publish(uow: UnitOfWork) -> list[object]:
    """Return the events a committed transaction released.

    Events are withheld until commit, so this returns nothing if the transaction
    rolled back -- which is what makes "never announce a fact that was rolled
    back" structural rather than a rule callers must remember.
    """
    return list(uow.collect_events())
