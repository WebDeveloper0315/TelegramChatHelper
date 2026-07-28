"""The chat repository, run against both implementations.

Chat is the graph's edge, so this suite asserts the properties that make it an
edge rather than another table: a chat reaches exactly one contact, that contact
belongs to the same account, and the relationship survives neither a deleted
account nor a purged contact.

The composite foreign key on ``(account_id, contact_id)`` is the milestone's
central guarantee (ADR-043), so it is tested against the real schema *and*
against a fake that models it independently.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.chat_repository import InMemoryChatRepository, InMemoryChatStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork
from tests.support.repository_contract import RepositoryContract, RepositoryUnderTest
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import AiProcessingMode, Chat, ChatType
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAlchemyUnitOfWork,
    SqlChatRepository,
    SqlContactRepository,
    SqliteDatabase,
)

EPOCH = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)

SORT_FIELD = "created_at"

# One contact per account per index, so a chat can always be built for either.
CONTACTS = 60


def contact_id_for(index: int, account_id: AccountId) -> ContactId:
    """Return the identifier of the nth contact of an account."""
    return ContactId(index * 10 + int(account_id))


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own chats."""
    return Account.create(
        account_id=account_id,
        telegram_user_id=TelegramUserId(1000 + int(account_id)),
        display_name=f"account-{int(account_id)}",
        now=EPOCH,
        is_active=is_active,
    )


def make_contact(index: int, account_id: AccountId) -> Contact:
    """Build the nth contact of an account."""
    return Contact.create(
        contact_id=contact_id_for(index, account_id),
        account_id=account_id,
        telegram_user_id=TelegramUserId(index * 1000 + int(account_id)),
        display_name=f"contact-{index}",
        now=EPOCH,
    )


def make_chat(index: int, *, account_id: AccountId = ACCOUNT_A) -> Chat:
    """Build the nth distinct private chat, ordered by creation time."""
    return Chat.private_with(
        chat_id=ChatId(index * 10 + int(account_id)),
        account_id=account_id,
        telegram_chat_id=TelegramChatId(index * 100 + int(account_id)),
        contact_id=contact_id_for(index, account_id),
        now=EPOCH + timedelta(minutes=index),
    )


def make_group(index: int, *, account_id: AccountId = ACCOUNT_A) -> Chat:
    """Build a group chat, with the negative identifier Telegram would give it."""
    return Chat.group_titled(
        chat_id=ChatId(50_000 + index),
        account_id=account_id,
        telegram_chat_id=TelegramChatId(-(index * 100 + int(account_id))),
        chat_type=ChatType.GROUP,
        title=f"group-{index}",
        now=EPOCH + timedelta(minutes=index),
    )


# ---------------------------------------------------------------------------
# The shared contract suite
# ---------------------------------------------------------------------------


@pytest.fixture
async def sql_subject_for_contract(
    tmp_path: Path,
) -> AsyncIterator[RepositoryUnderTest[Chat, ChatId]]:
    """The SQL repository, adapted to the shared contract's vocabulary."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "chats.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    await SqlAccountRepository(uow).add(make_account(ACCOUNT_A, is_active=True))
    contacts = SqlContactRepository(uow, ACCOUNT_A)
    for index in range(1, CONTACTS):
        await contacts.add(make_contact(index, ACCOUNT_A))

    repository = SqlChatRepository(uow, ACCOUNT_A)
    try:
        yield _adapt(repository, uow)
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject_for_contract() -> RepositoryUnderTest[Chat, ChatId]:
    """The in-memory repository, adapted to the shared contract's vocabulary."""
    uow = InMemoryUnitOfWork()
    await uow.begin()
    store = InMemoryChatStore(
        known_accounts={int(ACCOUNT_A)},
        contacts={int(contact_id_for(i, ACCOUNT_A)): int(ACCOUNT_A) for i in range(1, CONTACTS)},
    )
    return _adapt(InMemoryChatRepository(store, ACCOUNT_A), uow)


def _adapt(repository: ChatRepository, uow: object) -> RepositoryUnderTest[Chat, ChatId]:
    """Describe a chat repository in the contract's terms.

    No ``soft_delete``: a chat has no lifecycle of its own. It exists because a
    conversation exists, and it goes when its account or contact goes.
    """
    return RepositoryUnderTest(
        add=repository.add,
        get=repository.get,
        page=repository.list_chats,
        identity=lambda chat: chat.id,
        make=make_chat,
        uow=uow,  # type: ignore[arg-type]
        sort_field=SORT_FIELD,
    )


class TestSqlChatRepositoryContract(RepositoryContract[Chat, ChatId]):
    """The SQL implementation against the shared suite."""

    @pytest.fixture
    def subject(
        self, sql_subject_for_contract: RepositoryUnderTest[Chat, ChatId]
    ) -> RepositoryUnderTest[Chat, ChatId]:
        return sql_subject_for_contract


class TestInMemoryChatRepositoryContract(RepositoryContract[Chat, ChatId]):
    """The in-memory implementation against the same suite."""

    @pytest.fixture
    def subject(
        self, memory_subject_for_contract: RepositoryUnderTest[Chat, ChatId]
    ) -> RepositoryUnderTest[Chat, ChatId]:
        return memory_subject_for_contract


# ---------------------------------------------------------------------------
# Obligations specific to the communication graph
# ---------------------------------------------------------------------------


@dataclass
class ChatSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_account: object
    delete_contact: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[ChatSubject]:
    """The SQL repository against a migrated database with two populated accounts."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "chats.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    accounts = SqlAccountRepository(uow)
    await accounts.add(make_account(ACCOUNT_A, is_active=True))
    await accounts.add(make_account(ACCOUNT_B))
    for account_id in (ACCOUNT_A, ACCOUNT_B):
        contacts = SqlContactRepository(uow, account_id)
        for index in range(1, 4):
            await contacts.add(make_contact(index, account_id))

    async def delete_account(account_id: AccountId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM accounts WHERE id = :id"), {"id": int(account_id)}
            )
        )

    async def delete_contact(contact_id: ContactId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM contacts WHERE id = :id"), {"id": int(contact_id)}
            )
        )

    try:
        yield ChatSubject(
            for_account=lambda account_id: SqlChatRepository(uow, account_id),
            delete_account=delete_account,
            delete_contact=delete_contact,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> ChatSubject:
    """The in-memory repository against a shared store with two populated accounts."""
    store = InMemoryChatStore(
        known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
        contacts={
            int(contact_id_for(index, account_id)): int(account_id)
            for account_id in (ACCOUNT_A, ACCOUNT_B)
            for index in range(1, 4)
        },
    )

    async def delete_account(account_id: AccountId) -> None:
        store.delete_account(account_id)

    async def delete_contact(contact_id: ContactId) -> None:
        store.delete_contact(contact_id)

    return ChatSubject(
        for_account=lambda account_id: InMemoryChatRepository(store, account_id),
        delete_account=delete_account,
        delete_contact=delete_contact,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> ChatSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: ChatSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: ChatSubject, account_id: AccountId) -> ChatRepository:
    """Build a repository scoped to an account."""
    built: ChatRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestTheGraphEdge:
    """A chat reaches exactly one contact, and only its own account's."""

    async def test_a_private_chat_reaches_its_contact(self, subject: ChatSubject) -> None:
        chat = make_chat(1)
        await repo(subject, ACCOUNT_A).add(chat)

        found = await repo(subject, ACCOUNT_A).get_private_with(chat.contact_id or ContactId(0))

        assert found is not None
        assert found.id == chat.id

    async def test_a_contact_has_at_most_one_private_chat(self, subject: ChatSubject) -> None:
        chats = repo(subject, ACCOUNT_A)
        first = make_chat(1)
        await chats.add(first)

        second = Chat.private_with(
            chat_id=ChatId(9999),
            account_id=ACCOUNT_A,
            telegram_chat_id=TelegramChatId(8888),
            contact_id=first.contact_id or ContactId(0),
            now=EPOCH,
        )

        with pytest.raises(ConstraintViolationError):
            await chats.add(second)

    async def test_a_chat_cannot_reach_another_accounts_contact(self, subject: ChatSubject) -> None:
        # The guarantee this milestone exists to establish. The composite
        # foreign key makes the pair the thing that must exist, so a chat in one
        # account naming a contact in another is refused by the store rather
        # than only by the use case that usually checks (ADR-043).
        trespasser = Chat.private_with(
            chat_id=ChatId(7777),
            account_id=ACCOUNT_A,
            telegram_chat_id=TelegramChatId(7777),
            contact_id=contact_id_for(1, ACCOUNT_B),
            now=EPOCH,
        )

        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(trespasser)

    async def test_a_chat_cannot_reach_a_contact_that_does_not_exist(
        self, subject: ChatSubject
    ) -> None:
        orphan = Chat.private_with(
            chat_id=ChatId(7777),
            account_id=ACCOUNT_A,
            telegram_chat_id=TelegramChatId(7777),
            contact_id=ContactId(999_999),
            now=EPOCH,
        )

        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(orphan)

    async def test_a_group_chat_reaches_nobody(self, subject: ChatSubject) -> None:
        group = make_group(1)
        await repo(subject, ACCOUNT_A).add(group)

        found = await repo(subject, ACCOUNT_A).get(group.id)

        assert found is not None
        assert found.contact_id is None
        assert found.title == "group-1"

    async def test_a_negative_telegram_identifier_round_trips(self, subject: ChatSubject) -> None:
        # Telegram numbers groups and channels below zero. A "positive" check
        # would have rejected every group chat ever synchronised.
        group = make_group(1)
        await repo(subject, ACCOUNT_A).add(group)

        found = await repo(subject, ACCOUNT_A).get_by_telegram_id(group.telegram_chat_id)

        assert found is not None
        assert int(found.telegram_chat_id) < 0


class TestAccountOwnership:
    """A chat belongs to exactly one account, and cannot be misfiled."""

    async def test_exposes_its_scope(self, subject: ChatSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    async def test_a_foreign_chat_cannot_be_added(self, subject: ChatSubject) -> None:
        with pytest.raises(DomainValidationError, match="scoped to account"):
            await repo(subject, ACCOUNT_A).add(make_chat(1, account_id=ACCOUNT_B))

    async def test_a_foreign_chat_cannot_be_updated(self, subject: ChatSubject) -> None:
        foreign = make_chat(1, account_id=ACCOUNT_B)
        await repo(subject, ACCOUNT_B).add(foreign)

        with pytest.raises(DomainValidationError, match="scoped to account"):
            await repo(subject, ACCOUNT_A).update(
                foreign.with_sync_enabled(enabled=False, now=EPOCH + timedelta(days=1))
            )

    async def test_a_chat_for_a_missing_account_is_refused(self, subject: ChatSubject) -> None:
        absent = AccountId(999)
        orphan = Chat.group_titled(
            chat_id=ChatId(1),
            account_id=absent,
            telegram_chat_id=TelegramChatId(-1),
            chat_type=ChatType.GROUP,
            title="Nowhere",
            now=EPOCH,
        )

        with pytest.raises(ConstraintViolationError):
            await repo(subject, absent).add(orphan)

    async def test_updating_an_absent_chat_raises(self, subject: ChatSubject) -> None:
        with pytest.raises(RecordNotFoundError):
            await repo(subject, ACCOUNT_A).update(make_chat(1))

    async def test_a_duplicate_telegram_chat_is_refused(self, subject: ChatSubject) -> None:
        chats = repo(subject, ACCOUNT_A)
        first = make_chat(1)
        await chats.add(first)

        duplicate = Chat.private_with(
            chat_id=ChatId(9999),
            account_id=ACCOUNT_A,
            telegram_chat_id=first.telegram_chat_id,
            contact_id=contact_id_for(2, ACCOUNT_A),
            now=EPOCH,
        )

        with pytest.raises(ConstraintViolationError):
            await chats.add(duplicate)

    async def test_the_same_telegram_chat_may_belong_to_two_accounts(
        self, subject: ChatSubject
    ) -> None:
        # Two accounts can be in the same group, and each records it separately.
        shared = TelegramChatId(-4242)
        for account_id, chat_id in ((ACCOUNT_A, ChatId(1)), (ACCOUNT_B, ChatId(2))):
            await repo(subject, account_id).add(
                Chat.group_titled(
                    chat_id=chat_id,
                    account_id=account_id,
                    telegram_chat_id=shared,
                    chat_type=ChatType.GROUP,
                    title="Shared",
                    now=EPOCH,
                )
            )

        assert await repo(subject, ACCOUNT_A).get_by_telegram_id(shared) is not None
        assert await repo(subject, ACCOUNT_B).get_by_telegram_id(shared) is not None


class TestScopeIsolation:
    """Two scoped repositories over the same storage never see each other's data."""

    async def test_one_account_does_not_see_another(self, subject: ChatSubject) -> None:
        chat = make_chat(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(chat)

        assert await repo(subject, ACCOUNT_B).get(chat.id) is None

    async def test_a_listing_shows_only_its_own_account(self, subject: ChatSubject) -> None:
        for index in (1, 2, 3):
            await repo(subject, ACCOUNT_A).add(make_chat(index, account_id=ACCOUNT_A))
        await repo(subject, ACCOUNT_B).add(make_chat(1, account_id=ACCOUNT_B))

        page_a = await repo(subject, ACCOUNT_A).list_chats(PageRequest(limit=50))
        page_b = await repo(subject, ACCOUNT_B).list_chats(PageRequest(limit=50))

        assert len(page_a) == 3
        assert len(page_b) == 1
        assert all(chat.account_id == ACCOUNT_A for chat in page_a)

    async def test_a_contact_traversal_does_not_cross_accounts(self, subject: ChatSubject) -> None:
        chat = make_chat(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(chat)

        found = await repo(subject, ACCOUNT_B).get_private_with(chat.contact_id or ContactId(0))

        assert found is None

    async def test_a_telegram_lookup_does_not_cross_accounts(self, subject: ChatSubject) -> None:
        chat = make_chat(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(chat)

        assert await repo(subject, ACCOUNT_B).get_by_telegram_id(chat.telegram_chat_id) is None

    async def test_no_method_accepts_an_account_argument(self) -> None:
        for name in (
            "get",
            "add",
            "update",
            "get_by_telegram_id",
            "get_private_with",
            "list_chats",
        ):
            signature = inspect.signature(getattr(SqlChatRepository, name))
            assert "account_id" not in signature.parameters


class TestCascadeDeletion:
    """The graph does not outlive its nodes."""

    async def test_deleting_an_account_removes_its_chats(self, subject: ChatSubject) -> None:
        chat = make_chat(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(chat)

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get(chat.id) is None

    async def test_deleting_a_contact_removes_their_private_chat(
        self, subject: ChatSubject
    ) -> None:
        # The purge in PRIVACY.md section 7 removes everything referencing a
        # contact. DATABASE.md version 1.0 specified ON DELETE SET NULL here,
        # which would instead leave a private chat with nobody in it -- a row
        # the invariant forbids (ADR-043).
        chat = make_chat(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(chat)

        await subject.delete_contact(chat.contact_id or ContactId(0))  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get(chat.id) is None

    async def test_deleting_a_contact_leaves_group_chats_alone(self, subject: ChatSubject) -> None:
        group = make_group(1)
        private = make_chat(1)
        await repo(subject, ACCOUNT_A).add(group)
        await repo(subject, ACCOUNT_A).add(private)

        await subject.delete_contact(private.contact_id or ContactId(0))  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get(group.id) is not None

    async def test_deleting_an_account_leaves_others_alone(self, subject: ChatSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_chat(1, account_id=ACCOUNT_A))
        kept = make_chat(1, account_id=ACCOUNT_B)
        await repo(subject, ACCOUNT_B).add(kept)

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_B).get(kept.id) is not None


class TestPolicyChanges:
    """Synchronisation and AI policy are the two things a user controls."""

    async def test_disabling_sync_persists(self, subject: ChatSubject) -> None:
        chats = repo(subject, ACCOUNT_A)
        chat = make_chat(1)
        await chats.add(chat)

        await chats.update(chat.with_sync_enabled(enabled=False, now=EPOCH + timedelta(days=1)))
        found = await chats.get(chat.id)

        assert found is not None
        assert not found.sync_enabled

    async def test_disabling_ai_persists(self, subject: ChatSubject) -> None:
        chats = repo(subject, ACCOUNT_A)
        chat = make_chat(1)
        await chats.add(chat)

        await chats.update(
            chat.with_ai_processing_mode(AiProcessingMode.DISABLED, EPOCH + timedelta(days=1))
        )
        found = await chats.get(chat.id)

        assert found is not None
        assert found.ai_processing_mode is AiProcessingMode.DISABLED
        assert not found.allows_ai

    async def test_a_new_chat_keeps_content_local(self, subject: ChatSubject) -> None:
        # ADR-024's default, asserted where it is observable rather than only
        # where it is written.
        chats = repo(subject, ACCOUNT_A)
        chat = make_chat(1)
        await chats.add(chat)

        found = await chats.get(chat.id)

        assert found is not None
        assert found.ai_processing_mode is AiProcessingMode.LOCAL_ONLY
        assert not found.allows_cloud_ai


class TestImplementationsAgree:
    """Both implementations behave identically."""

    async def test_a_private_chat_round_trips_identically(
        self, sql_subject: ChatSubject, memory_subject: ChatSubject
    ) -> None:
        chat = make_chat(1)

        await repo(sql_subject, ACCOUNT_A).add(chat)
        await repo(memory_subject, ACCOUNT_A).add(chat)

        assert await repo(sql_subject, ACCOUNT_A).get(chat.id) == (
            await repo(memory_subject, ACCOUNT_A).get(chat.id)
        )

    async def test_a_group_chat_round_trips_identically(
        self, sql_subject: ChatSubject, memory_subject: ChatSubject
    ) -> None:
        # The nullable columns differ between the two kinds, so both are checked.
        group = make_group(1)

        await repo(sql_subject, ACCOUNT_A).add(group)
        await repo(memory_subject, ACCOUNT_A).add(group)

        from_sql = await repo(sql_subject, ACCOUNT_A).get(group.id)
        from_memory = await repo(memory_subject, ACCOUNT_A).get(group.id)

        assert from_sql == from_memory
        assert from_sql is not None
        assert from_sql.contact_id is None
