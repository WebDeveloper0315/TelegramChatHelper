"""The account repository, run against both implementations.

The SQL repository and the in-memory fake run the identical contract suite from
Milestone 1.0, plus the account-specific obligations below. The first real
aggregate is where that suite stops being theoretical.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.unit_of_work import InMemoryUnitOfWork
from tests.support.repository_contract import RepositoryContract, RepositoryUnderTest
from tgassist.domain.errors import ConstraintViolationError, RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAlchemyUnitOfWork,
    SqliteDatabase,
)

EPOCH = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SORT_FIELD = "created_at"


def make_account(index: int) -> Account:
    """Build the nth distinct account.

    Timestamps advance every third account, so several share one -- the tie the
    pagination tiebreaker exists to resolve.
    """
    return Account.create(
        account_id=AccountId(index),
        telegram_user_id=TelegramUserId(1000 + index),
        display_name=f"account-{index:03d}",
        now=EPOCH + timedelta(minutes=index // 3),
    )


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[RepositoryUnderTest[Account, AccountId]]:
    """The SQL repository, inside an open transaction on a migrated database."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "accounts.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()
    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    try:
        repository = SqlAccountRepository(uow)
        yield RepositoryUnderTest(
            add=repository.add,
            get=repository.get,
            page=repository.list_accounts,
            identity=lambda account: account.id,
            make=make_account,
            uow=uow,
            sort_field=SORT_FIELD,
            extra={"repository": repository},
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> RepositoryUnderTest[Account, AccountId]:
    """The in-memory repository, inside an open transaction."""
    uow = InMemoryUnitOfWork()
    await uow.begin()
    repository = InMemoryAccountRepository()
    return RepositoryUnderTest(
        add=repository.add,
        get=repository.get,
        page=repository.list_accounts,
        identity=lambda account: account.id,
        make=make_account,
        uow=uow,
        sort_field=SORT_FIELD,
        extra={"repository": repository},
    )


class TestSqlAccountRepositoryContract(RepositoryContract[Account, AccountId]):
    """The SQLAlchemy implementation."""

    @pytest.fixture
    def subject(
        self, sql_subject: RepositoryUnderTest[Account, AccountId]
    ) -> RepositoryUnderTest[Account, AccountId]:
        return sql_subject


class TestInMemoryAccountRepositoryContract(RepositoryContract[Account, AccountId]):
    """The in-memory fake."""

    @pytest.fixture
    def subject(
        self, memory_subject: RepositoryUnderTest[Account, AccountId]
    ) -> RepositoryUnderTest[Account, AccountId]:
        return memory_subject


@pytest.fixture(params=["sql", "memory"])
def accounts(request: pytest.FixtureRequest) -> AccountRepository:
    """Both account repository implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    subject: RepositoryUnderTest[Account, AccountId] = request.getfixturevalue(name)
    repository: AccountRepository = subject.extra["repository"]
    return repository


class TestAccountRepositoryBehaviour:
    """Obligations specific to accounts, held by both implementations."""

    def test_satisfies_the_port(self, accounts: AccountRepository) -> None:
        assert isinstance(accounts, AccountRepository)

    async def test_lookup_by_telegram_id(self, accounts: AccountRepository) -> None:
        account = make_account(1)
        await accounts.add(account)

        found = await accounts.get_by_telegram_id(account.telegram_user_id)

        assert found is not None
        assert found.id == account.id

    async def test_unknown_telegram_id_returns_none(self, accounts: AccountRepository) -> None:
        assert await accounts.get_by_telegram_id(TelegramUserId(999_999)) is None

    async def test_duplicate_telegram_id_is_refused(self, accounts: AccountRepository) -> None:
        # Two local accounts for one Telegram user would make "which account is
        # this message for" unanswerable.
        await accounts.add(make_account(1))
        duplicate = Account.create(
            account_id=AccountId(2),
            telegram_user_id=make_account(1).telegram_user_id,
            display_name="duplicate",
            now=EPOCH,
        )

        with pytest.raises(ConstraintViolationError):
            await accounts.add(duplicate)

    async def test_duplicate_identifier_is_refused(self, accounts: AccountRepository) -> None:
        await accounts.add(make_account(1))

        with pytest.raises(ConstraintViolationError):
            await accounts.add(make_account(1))

    async def test_no_active_account_initially(self, accounts: AccountRepository) -> None:
        # An ordinary state: a fresh installation has no account at all.
        assert await accounts.get_active() is None

    async def test_active_account_is_found(self, accounts: AccountRepository) -> None:
        await accounts.add(make_account(1).activated(EPOCH))

        active = await accounts.get_active()

        assert active is not None
        assert active.is_active

    async def test_a_second_active_account_is_refused(self, accounts: AccountRepository) -> None:
        # The single-active invariant, enforced by the store rather than by
        # every caller remembering to deactivate first.
        await accounts.add(make_account(1).activated(EPOCH))

        with pytest.raises(ConstraintViolationError):
            await accounts.add(make_account(2).activated(EPOCH))

    async def test_set_active_moves_the_flag(self, accounts: AccountRepository) -> None:
        await accounts.add(make_account(1).activated(EPOCH))
        await accounts.add(make_account(2))
        later = EPOCH + timedelta(hours=1)

        activated = await accounts.set_active(AccountId(2), later)

        assert activated.is_active
        active = await accounts.get_active()
        assert active is not None
        assert int(active.id) == 2

    async def test_set_active_deactivates_the_previous(self, accounts: AccountRepository) -> None:
        await accounts.add(make_account(1).activated(EPOCH))
        await accounts.add(make_account(2))

        await accounts.set_active(AccountId(2), EPOCH + timedelta(hours=1))

        previous = await accounts.get(AccountId(1))
        assert previous is not None
        assert previous.is_active is False

    async def test_set_active_on_the_active_account_is_a_no_op(
        self, accounts: AccountRepository
    ) -> None:
        await accounts.add(make_account(1).activated(EPOCH))

        result = await accounts.set_active(AccountId(1), EPOCH + timedelta(hours=1))

        assert result.is_active
        assert result.updated_at == EPOCH

    async def test_set_active_on_an_unknown_account_raises(
        self, accounts: AccountRepository
    ) -> None:
        # Unlike a lookup, this method promises a result: activating something
        # that does not exist is a caller mistake worth reporting.
        with pytest.raises(RecordNotFoundError):
            await accounts.set_active(AccountId(999), EPOCH)


class TestImplementationsAgree:
    """Both implementations must produce identical results."""

    async def test_pagination_matches(
        self,
        sql_subject: RepositoryUnderTest[Account, AccountId],
        memory_subject: RepositoryUnderTest[Account, AccountId],
    ) -> None:
        for index in range(12):
            account = make_account(index + 1)
            await sql_subject.add(account)
            await memory_subject.add(account)

        async def walk(subject: RepositoryUnderTest[Account, AccountId]) -> list[int]:
            collected: list[int] = []
            cursor: str | None = None
            while True:
                page = await subject.page(PageRequest(cursor=cursor, limit=5))
                collected.extend(int(a.id) for a in page.items)
                if not page.has_more:
                    return collected
                cursor = page.next_cursor

        assert await walk(sql_subject) == await walk(memory_subject)

    async def test_activation_matches(
        self,
        sql_subject: RepositoryUnderTest[Account, AccountId],
        memory_subject: RepositoryUnderTest[Account, AccountId],
    ) -> None:
        sql: AccountRepository = sql_subject.extra["repository"]
        memory: AccountRepository = memory_subject.extra["repository"]
        for index in (1, 2):
            account = make_account(index)
            await sql.add(account)
            await memory.add(account)

        later = EPOCH + timedelta(hours=1)
        sql_result = await sql.set_active(AccountId(2), later)
        memory_result = await memory.set_active(AccountId(2), later)

        assert sql_result == memory_result
