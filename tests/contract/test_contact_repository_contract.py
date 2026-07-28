"""The contact repository, run against both implementations.

Contact is the first aggregate that is both account-scoped *and* a paginated
collection, so this file does two things. It runs the shared Milestone 1.0
contract suite -- including the soft-deletion branch, which no aggregate had
exercised until now -- and it adds the obligations that only matter when rows
belong to somebody: ownership, foreign-key integrity, cascade deletion, scope
isolation and the archive lifecycle.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.contact_repository import InMemoryContactRepository, InMemoryContactStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork
from tests.support.repository_contract import RepositoryContract, RepositoryUnderTest
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import AccountId, ContactId, TelegramUserId
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.contact_repository import ContactRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAlchemyUnitOfWork,
    SqlContactRepository,
    SqliteDatabase,
)

EPOCH = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
ABSENT_ACCOUNT = AccountId(999)

SORT_FIELD = "created_at"


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own contacts."""
    return Account.create(
        account_id=account_id,
        telegram_user_id=TelegramUserId(1000 + int(account_id)),
        display_name=f"account-{int(account_id)}",
        now=EPOCH,
        is_active=is_active,
    )


def make_contact(index: int, *, account_id: AccountId = ACCOUNT_A) -> Contact:
    """Build the nth distinct contact, ordered by creation time.

    Distinct in both keys the table cares about: the identifier and the Telegram
    user. Sharing either would make the pagination assertions test the wrong
    thing, or fail on a constraint rather than on the property under test.
    """
    return Contact.create(
        contact_id=ContactId(index * 10 + int(account_id)),
        account_id=account_id,
        telegram_user_id=TelegramUserId(index * 100 + int(account_id)),
        display_name=f"contact-{index}",
        username=f"user_{index:04d}",
        now=EPOCH + timedelta(minutes=index),
    )


# ---------------------------------------------------------------------------
# The shared contract suite
# ---------------------------------------------------------------------------


@pytest.fixture
async def sql_subject_for_contract(
    tmp_path: Path,
) -> AsyncIterator[RepositoryUnderTest[Contact, ContactId]]:
    """The SQL repository, adapted to the shared contract's vocabulary."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "contacts.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    await SqlAccountRepository(uow).add(make_account(ACCOUNT_A, is_active=True))
    repository = SqlContactRepository(uow, ACCOUNT_A)

    try:
        yield _adapt(repository, uow)
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject_for_contract() -> RepositoryUnderTest[Contact, ContactId]:
    """The in-memory repository, adapted to the shared contract's vocabulary."""
    uow = InMemoryUnitOfWork()
    await uow.begin()
    store = InMemoryContactStore(known_accounts={int(ACCOUNT_A)})
    return _adapt(InMemoryContactRepository(store, ACCOUNT_A), uow)


def _adapt(repository: ContactRepository, uow: object) -> RepositoryUnderTest[Contact, ContactId]:
    """Describe a contact repository in the contract's terms.

    Soft deletion is an ``update`` with the entity's own transition applied,
    which is the whole reason the port has no ``delete`` method: the rule for
    what may be deleted belongs to the entity.
    """

    async def soft_delete(contact_id: ContactId) -> None:
        found = await repository.get(contact_id, include_deleted=True)
        if found is None:  # pragma: no cover - the contract always adds first
            return
        await repository.update(found.deleted(EPOCH + timedelta(days=1)))

    return RepositoryUnderTest(
        add=repository.add,
        get=repository.get,
        page=repository.list_contacts,
        identity=lambda contact: contact.id,
        make=make_contact,
        uow=uow,  # type: ignore[arg-type]
        soft_delete=soft_delete,
        sort_field=SORT_FIELD,
    )


class TestSqlContactRepositoryContract(RepositoryContract[Contact, ContactId]):
    """The SQL implementation against the shared suite."""

    @pytest.fixture
    def subject(
        self, sql_subject_for_contract: RepositoryUnderTest[Contact, ContactId]
    ) -> RepositoryUnderTest[Contact, ContactId]:
        return sql_subject_for_contract


class TestInMemoryContactRepositoryContract(RepositoryContract[Contact, ContactId]):
    """The in-memory implementation against the same suite."""

    @pytest.fixture
    def subject(
        self, memory_subject_for_contract: RepositoryUnderTest[Contact, ContactId]
    ) -> RepositoryUnderTest[Contact, ContactId]:
        return memory_subject_for_contract


# ---------------------------------------------------------------------------
# Obligations specific to an account-owned collection
# ---------------------------------------------------------------------------


@dataclass
class ContactSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_account: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[ContactSubject]:
    """The SQL repository against a migrated database with two accounts."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "contacts.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    accounts = SqlAccountRepository(uow)
    await accounts.add(make_account(ACCOUNT_A, is_active=True))
    await accounts.add(make_account(ACCOUNT_B))

    async def delete_account(account_id: AccountId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM accounts WHERE id = :id"), {"id": int(account_id)}
            )
        )

    try:
        yield ContactSubject(
            for_account=lambda account_id: SqlContactRepository(uow, account_id),
            delete_account=delete_account,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> ContactSubject:
    """The in-memory repository against a shared store with two accounts."""
    store = InMemoryContactStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})

    async def delete_account(account_id: AccountId) -> None:
        store.delete_account(account_id)

    return ContactSubject(
        for_account=lambda account_id: InMemoryContactRepository(store, account_id),
        delete_account=delete_account,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> ContactSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: ContactSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: ContactSubject, account_id: AccountId) -> ContactRepository:
    """Build a repository scoped to an account."""
    built: ContactRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestAccountOwnership:
    """A contact belongs to exactly one account, and cannot be misfiled."""

    async def test_exposes_its_scope(self, subject: ContactSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    async def test_a_foreign_contact_cannot_be_added(self, subject: ContactSubject) -> None:
        with pytest.raises(DomainValidationError, match="scoped to account") as excinfo:
            await repo(subject, ACCOUNT_A).add(make_contact(1, account_id=ACCOUNT_B))

        assert "different account" in excinfo.value.user_message

    async def test_a_foreign_contact_cannot_be_updated(self, subject: ContactSubject) -> None:
        foreign = make_contact(1, account_id=ACCOUNT_B)
        await repo(subject, ACCOUNT_B).add(foreign)

        with pytest.raises(DomainValidationError, match="scoped to account"):
            await repo(subject, ACCOUNT_A).update(
                foreign.renamed("Hijacked", EPOCH + timedelta(days=1))
            )

    async def test_a_contact_for_a_missing_account_is_refused(
        self, subject: ContactSubject
    ) -> None:
        # Foreign key integrity: a contact cannot exist without an owner.
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ABSENT_ACCOUNT).add(make_contact(1, account_id=ABSENT_ACCOUNT))

    async def test_updating_an_absent_contact_raises(self, subject: ContactSubject) -> None:
        with pytest.raises(RecordNotFoundError):
            await repo(subject, ACCOUNT_A).update(make_contact(1))


class TestTelegramIdUniqueness:
    """One contact per Telegram user per account -- and no further."""

    async def test_a_duplicate_within_one_account_is_refused(self, subject: ContactSubject) -> None:
        contacts = repo(subject, ACCOUNT_A)
        await contacts.add(make_contact(1))

        duplicate = Contact.create(
            contact_id=ContactId(9999),
            account_id=ACCOUNT_A,
            telegram_user_id=make_contact(1).telegram_user_id,
            display_name="Same person again",
            now=EPOCH,
        )

        with pytest.raises(ConstraintViolationError):
            await contacts.add(duplicate)

    async def test_the_same_telegram_user_may_be_known_to_two_accounts(
        self, subject: ContactSubject
    ) -> None:
        # Two accounts, one person, two contacts: what is remembered about them
        # differs per account, so they are genuinely separate rows.
        shared = TelegramUserId(4242)
        a = Contact.create(
            contact_id=ContactId(1),
            account_id=ACCOUNT_A,
            telegram_user_id=shared,
            display_name="Known to A",
            now=EPOCH,
        )
        b = Contact.create(
            contact_id=ContactId(2),
            account_id=ACCOUNT_B,
            telegram_user_id=shared,
            display_name="Known to B",
            now=EPOCH,
        )

        await repo(subject, ACCOUNT_A).add(a)
        await repo(subject, ACCOUNT_B).add(b)

        found_a = await repo(subject, ACCOUNT_A).get_by_telegram_id(shared)
        found_b = await repo(subject, ACCOUNT_B).get_by_telegram_id(shared)
        assert found_a is not None
        assert found_b is not None
        assert found_a.display_name == "Known to A"
        assert found_b.display_name == "Known to B"

    async def test_a_deleted_contact_still_holds_its_telegram_id(
        self, subject: ContactSubject
    ) -> None:
        # The unique index covers soft-deleted rows deliberately. If it did not,
        # the same person could exist twice with their history attached to
        # whichever row happened to be current.
        contacts = repo(subject, ACCOUNT_A)
        original = make_contact(1)
        await contacts.add(original)
        await contacts.update(original.deleted(EPOCH + timedelta(days=1)))

        replacement = Contact.create(
            contact_id=ContactId(9999),
            account_id=ACCOUNT_A,
            telegram_user_id=original.telegram_user_id,
            display_name="Second attempt",
            now=EPOCH,
        )

        with pytest.raises(ConstraintViolationError):
            await contacts.add(replacement)

    async def test_a_deleted_contact_is_findable_when_asked_for(
        self, subject: ContactSubject
    ) -> None:
        # Which is what lets the caller be told "deleted; restore it" instead of
        # a constraint violation naming a column.
        contacts = repo(subject, ACCOUNT_A)
        original = make_contact(1)
        await contacts.add(original)
        await contacts.update(original.deleted(EPOCH + timedelta(days=1)))

        assert await contacts.get_by_telegram_id(original.telegram_user_id) is None
        assert (
            await contacts.get_by_telegram_id(original.telegram_user_id, include_deleted=True)
            is not None
        )


class TestScopeIsolation:
    """Two scoped repositories over the same storage never see each other's data."""

    async def test_one_account_does_not_see_another(self, subject: ContactSubject) -> None:
        contact = make_contact(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(contact)

        assert await repo(subject, ACCOUNT_B).get(contact.id) is None

    async def test_a_listing_shows_only_its_own_account(self, subject: ContactSubject) -> None:
        for index in (1, 2, 3):
            await repo(subject, ACCOUNT_A).add(make_contact(index, account_id=ACCOUNT_A))
        await repo(subject, ACCOUNT_B).add(make_contact(1, account_id=ACCOUNT_B))

        page_a = await repo(subject, ACCOUNT_A).list_contacts(PageRequest(limit=50))
        page_b = await repo(subject, ACCOUNT_B).list_contacts(PageRequest(limit=50))

        assert len(page_a) == 3
        assert len(page_b) == 1
        assert all(c.account_id == ACCOUNT_A for c in page_a)
        assert all(c.account_id == ACCOUNT_B for c in page_b)

    async def test_a_telegram_lookup_does_not_cross_accounts(self, subject: ContactSubject) -> None:
        contact = make_contact(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(contact)

        assert await repo(subject, ACCOUNT_B).get_by_telegram_id(contact.telegram_user_id) is None

    async def test_no_method_accepts_an_account_argument(self) -> None:
        # The structural guarantee: there is no scope for a caller to get wrong,
        # because there is no parameter to pass (ADR-039).
        for name in ("get", "add", "update", "get_by_telegram_id", "list_contacts"):
            signature = inspect.signature(getattr(SqlContactRepository, name))
            assert "account_id" not in signature.parameters


class TestCascadeDeletion:
    """Deleting an account removes its contacts."""

    async def test_deleting_an_account_removes_its_contacts(self, subject: ContactSubject) -> None:
        contact = make_contact(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(contact)

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get(contact.id, include_deleted=True) is None

    async def test_deleting_an_account_leaves_others_alone(self, subject: ContactSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_contact(1, account_id=ACCOUNT_A))
        kept = make_contact(1, account_id=ACCOUNT_B)
        await repo(subject, ACCOUNT_B).add(kept)

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_B).get(kept.id) is not None


class TestArchiveLifecycle:
    """Archived contacts are hidden from the default list, not lost."""

    async def test_archiving_hides_a_contact_from_the_default_list(
        self, subject: ContactSubject
    ) -> None:
        contacts = repo(subject, ACCOUNT_A)
        contact = make_contact(1)
        await contacts.add(contact)

        await contacts.update(contact.archived(EPOCH + timedelta(days=1)))

        assert len(await contacts.list_contacts(PageRequest(limit=50))) == 0

    async def test_an_archived_contact_is_listed_when_asked_for(
        self, subject: ContactSubject
    ) -> None:
        contacts = repo(subject, ACCOUNT_A)
        contact = make_contact(1)
        await contacts.add(contact)
        await contacts.update(contact.archived(EPOCH + timedelta(days=1)))

        page = await contacts.list_contacts(PageRequest(limit=50), include_archived=True)

        assert len(page) == 1
        assert page.items[0].is_archived

    async def test_an_archived_contact_is_still_retrievable_by_identifier(
        self, subject: ContactSubject
    ) -> None:
        # Unlike deletion. Archiving is "not in my way", not "gone", and restore
        # has to be able to find it without asking for deleted rows.
        contacts = repo(subject, ACCOUNT_A)
        contact = make_contact(1)
        await contacts.add(contact)
        await contacts.update(contact.archived(EPOCH + timedelta(days=1)))

        assert await contacts.get(contact.id) is not None

    async def test_restoring_returns_a_contact_to_the_default_list(
        self, subject: ContactSubject
    ) -> None:
        contacts = repo(subject, ACCOUNT_A)
        contact = make_contact(1)
        await contacts.add(contact)
        later = EPOCH + timedelta(days=1)

        await contacts.update(contact.archived(later))
        await contacts.update(contact.archived(later).restored(later))

        assert len(await contacts.list_contacts(PageRequest(limit=50))) == 1

    async def test_a_deleted_contact_is_never_listed(self, subject: ContactSubject) -> None:
        contacts = repo(subject, ACCOUNT_A)
        contact = make_contact(1)
        await contacts.add(contact)

        await contacts.update(contact.deleted(EPOCH + timedelta(days=1)))

        assert len(await contacts.list_contacts(PageRequest(limit=50))) == 0
        assert len(await contacts.list_contacts(PageRequest(limit=50), include_archived=True)) == 0

    async def test_restoring_a_deleted_contact_brings_it_back(
        self, subject: ContactSubject
    ) -> None:
        contacts = repo(subject, ACCOUNT_A)
        contact = make_contact(1)
        await contacts.add(contact)
        later = EPOCH + timedelta(days=1)
        await contacts.update(contact.deleted(later))

        deleted = await contacts.get(contact.id, include_deleted=True)
        assert deleted is not None
        await contacts.update(deleted.restored(later))

        assert await contacts.get(contact.id) is not None


class TestImplementationsAgree:
    """Both implementations behave identically."""

    async def test_a_contact_round_trips_identically(
        self, sql_subject: ContactSubject, memory_subject: ContactSubject
    ) -> None:
        contact = make_contact(1)

        await repo(sql_subject, ACCOUNT_A).add(contact)
        await repo(memory_subject, ACCOUNT_A).add(contact)

        assert await repo(sql_subject, ACCOUNT_A).get(contact.id) == (
            await repo(memory_subject, ACCOUNT_A).get(contact.id)
        )

    async def test_a_contact_without_a_username_round_trips_identically(
        self, sql_subject: ContactSubject, memory_subject: ContactSubject
    ) -> None:
        # The one nullable column, and therefore the one that can differ between
        # a store that keeps None and one that turns it into "".
        contact = Contact.create(
            contact_id=ContactId(1),
            account_id=ACCOUNT_A,
            telegram_user_id=TelegramUserId(77),
            display_name="No handle",
            now=EPOCH,
        )

        await repo(sql_subject, ACCOUNT_A).add(contact)
        await repo(memory_subject, ACCOUNT_A).add(contact)

        from_sql = await repo(sql_subject, ACCOUNT_A).get(contact.id)
        from_memory = await repo(memory_subject, ACCOUNT_A).get(contact.id)

        assert from_sql == from_memory
        assert from_sql is not None
        assert from_sql.username is None

    async def test_an_archived_contact_round_trips_identically(
        self, sql_subject: ContactSubject, memory_subject: ContactSubject
    ) -> None:
        contact = make_contact(1)
        later = EPOCH + timedelta(days=1)

        for holder in (sql_subject, memory_subject):
            contacts = repo(holder, ACCOUNT_A)
            await contacts.add(contact)
            await contacts.update(contact.archived(later))

        assert await repo(sql_subject, ACCOUNT_A).get(contact.id) == (
            await repo(memory_subject, ACCOUNT_A).get(contact.id)
        )
