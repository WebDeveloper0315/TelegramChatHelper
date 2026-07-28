"""The message repository, run against both implementations.

Messages are the first append-only aggregate, so this suite asserts what that
means: no update path exists, and the ordering, ownership and idempotency
guarantees hold on both implementations.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.message_repository import InMemoryMessageRepository, InMemoryMessageStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork
from tests.support.repository_contract import RepositoryContract, RepositoryUnderTest
from tgassist.domain.errors import ConstraintViolationError, DomainValidationError
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    MessageId,
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.message import Message, MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.message_repository import MessageRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAlchemyUnitOfWork,
    SqlChatRepository,
    SqlContactRepository,
    SqliteDatabase,
    SqlMessageRepository,
)

EPOCH = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
INGESTED = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)

SORT_FIELD = "sent_at"


def chat_id_for(account_id: AccountId) -> ChatId:
    """Return the identifier of an account's one chat."""
    return ChatId(100 + int(account_id))


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account."""
    return Account.create(
        account_id=account_id,
        telegram_user_id=TelegramUserId(1000 + int(account_id)),
        display_name=f"account-{int(account_id)}",
        now=EPOCH,
        is_active=is_active,
    )


def make_contact(account_id: AccountId) -> Contact:
    """Build an account's one contact."""
    return Contact.create(
        contact_id=ContactId(10 + int(account_id)),
        account_id=account_id,
        telegram_user_id=TelegramUserId(5000 + int(account_id)),
        display_name=f"contact-{int(account_id)}",
        now=EPOCH,
    )


def make_chat(account_id: AccountId) -> Chat:
    """Build an account's one private chat."""
    return Chat.private_with(
        chat_id=chat_id_for(account_id),
        account_id=account_id,
        telegram_chat_id=TelegramChatId(7000 + int(account_id)),
        contact_id=ContactId(10 + int(account_id)),
        now=EPOCH,
    )


def make_message(index: int, *, account_id: AccountId = ACCOUNT_A) -> Message:
    """Build the nth distinct message, ordered by when it was sent."""
    return Message.record(
        message_id=MessageId(index * 10 + int(account_id)),
        account_id=account_id,
        chat_id=chat_id_for(account_id),
        sender_kind=SenderKind.CONTACT,
        text=f"message {index}",
        sent_at=EPOCH + timedelta(minutes=index),
        ingested_at=INGESTED,
        telegram_message_id=TelegramMessageId(index * 100 + int(account_id)),
    )


def make_unidentified(index: int, *, account_id: AccountId = ACCOUNT_A) -> Message:
    """Build a message from a source that issues no identifiers."""
    return Message.record(
        message_id=MessageId(90_000 + index),
        account_id=account_id,
        chat_id=chat_id_for(account_id),
        sender_kind=SenderKind.OPERATOR,
        text=f"typed {index}",
        sent_at=EPOCH + timedelta(minutes=index),
        ingested_at=INGESTED,
    )


# ---------------------------------------------------------------------------
# The shared contract suite
# ---------------------------------------------------------------------------


@pytest.fixture
async def sql_subject_for_contract(
    tmp_path: Path,
) -> AsyncIterator[RepositoryUnderTest[Message, MessageId]]:
    """The SQL repository, adapted to the shared contract's vocabulary."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "messages.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    await SqlAccountRepository(uow).add(make_account(ACCOUNT_A, is_active=True))
    await SqlContactRepository(uow, ACCOUNT_A).add(make_contact(ACCOUNT_A))
    await SqlChatRepository(uow, ACCOUNT_A).add(make_chat(ACCOUNT_A))

    try:
        yield _adapt(SqlMessageRepository(uow, ACCOUNT_A), uow)
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject_for_contract() -> RepositoryUnderTest[Message, MessageId]:
    """The in-memory repository, adapted to the shared contract's vocabulary."""
    uow = InMemoryUnitOfWork()
    await uow.begin()
    store = InMemoryMessageStore(chats={int(chat_id_for(ACCOUNT_A)): int(ACCOUNT_A)})
    return _adapt(InMemoryMessageRepository(store, ACCOUNT_A), uow)


def _adapt(repository: MessageRepository, uow: object) -> RepositoryUnderTest[Message, MessageId]:
    """Describe a message repository in the contract's terms.

    No ``soft_delete``: nothing deletes a message. Retention is Milestone 10 and
    purge is Milestone 11, and the absence of the capability here is the same
    statement the port makes by having no delete method.
    """

    async def page(request: PageRequest) -> object:
        return await repository.list_by_chat(chat_id_for(ACCOUNT_A), request)

    return RepositoryUnderTest(
        add=repository.add,
        get=repository.get,
        page=page,  # type: ignore[arg-type]
        identity=lambda message: message.id,
        make=make_message,
        uow=uow,  # type: ignore[arg-type]
        sort_field=SORT_FIELD,
    )


class TestSqlMessageRepositoryContract(RepositoryContract[Message, MessageId]):
    """The SQL implementation against the shared suite."""

    @pytest.fixture
    def subject(
        self, sql_subject_for_contract: RepositoryUnderTest[Message, MessageId]
    ) -> RepositoryUnderTest[Message, MessageId]:
        return sql_subject_for_contract


class TestInMemoryMessageRepositoryContract(RepositoryContract[Message, MessageId]):
    """The in-memory implementation against the same suite."""

    @pytest.fixture
    def subject(
        self, memory_subject_for_contract: RepositoryUnderTest[Message, MessageId]
    ) -> RepositoryUnderTest[Message, MessageId]:
        return memory_subject_for_contract


# ---------------------------------------------------------------------------
# Obligations specific to an append-only, idempotent store
# ---------------------------------------------------------------------------


@dataclass
class MessageSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_chat: object
    delete_account: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[MessageSubject]:
    """The SQL repository against a migrated database with two populated accounts."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "messages.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    accounts = SqlAccountRepository(uow)
    await accounts.add(make_account(ACCOUNT_A, is_active=True))
    await accounts.add(make_account(ACCOUNT_B))
    for account_id in (ACCOUNT_A, ACCOUNT_B):
        await SqlContactRepository(uow, account_id).add(make_contact(account_id))
        await SqlChatRepository(uow, account_id).add(make_chat(account_id))

    async def delete_chat(chat_id: ChatId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM chats WHERE id = :id"), {"id": int(chat_id)}
            )
        )

    async def delete_account(account_id: AccountId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM accounts WHERE id = :id"), {"id": int(account_id)}
            )
        )

    try:
        yield MessageSubject(
            for_account=lambda account_id: SqlMessageRepository(uow, account_id),
            delete_chat=delete_chat,
            delete_account=delete_account,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> MessageSubject:
    """The in-memory repository against a shared store with two populated accounts."""
    store = InMemoryMessageStore(
        chats={
            int(chat_id_for(account_id)): int(account_id) for account_id in (ACCOUNT_A, ACCOUNT_B)
        }
    )

    async def delete_chat(chat_id: ChatId) -> None:
        store.delete_chat(chat_id)

    async def delete_account(account_id: AccountId) -> None:
        store.delete_account(account_id)

    return MessageSubject(
        for_account=lambda account_id: InMemoryMessageRepository(store, account_id),
        delete_chat=delete_chat,
        delete_account=delete_account,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> MessageSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: MessageSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: MessageSubject, account_id: AccountId) -> MessageRepository:
    """Build a repository scoped to an account."""
    built: MessageRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestAppendOnly:
    """A message is an immutable factual record, and the interface says so."""

    def test_the_port_exposes_no_update_or_delete(self) -> None:
        # The guarantee is structural. A convention that messages are not
        # modified is a convention somebody eventually breaks.
        for forbidden in ("update", "delete", "soft_delete", "remove", "purge"):
            assert not hasattr(MessageRepository, forbidden)

    def test_neither_implementation_exposes_one(self) -> None:
        for implementation in (SqlMessageRepository, InMemoryMessageRepository):
            for forbidden in ("update", "delete", "soft_delete", "remove", "purge"):
                assert not hasattr(implementation, forbidden), (
                    f"{implementation.__name__}.{forbidden} exists"
                )

    async def test_a_stored_message_reads_back_identical(self, subject: MessageSubject) -> None:
        message = make_message(1)
        await repo(subject, ACCOUNT_A).add(message)

        assert await repo(subject, ACCOUNT_A).get(message.id) == message


class TestIdempotency:
    """Re-ingesting is safe, which is what makes re-synchronisation possible."""

    async def test_a_repeat_is_recognisable_before_writing(self, subject: MessageSubject) -> None:
        # The lookup that lets the pipeline report "already present" instead of
        # arriving at a constraint violation.
        message = make_message(1)
        messages = repo(subject, ACCOUNT_A)
        await messages.add(message)

        found = await messages.get_by_telegram_id(
            message.chat_id, message.telegram_message_id or TelegramMessageId(1)
        )

        assert found is not None
        assert found.id == message.id

    async def test_an_absent_telegram_id_returns_none(self, subject: MessageSubject) -> None:
        found = await repo(subject, ACCOUNT_A).get_by_telegram_id(
            chat_id_for(ACCOUNT_A), TelegramMessageId(404)
        )

        assert found is None

    async def test_a_duplicate_telegram_id_in_one_chat_is_refused(
        self, subject: MessageSubject
    ) -> None:
        messages = repo(subject, ACCOUNT_A)
        first = make_message(1)
        await messages.add(first)

        duplicate = Message.record(
            message_id=MessageId(99_999),
            account_id=ACCOUNT_A,
            chat_id=first.chat_id,
            sender_kind=SenderKind.CONTACT,
            text="same identifier, different message",
            sent_at=EPOCH,
            ingested_at=INGESTED,
            telegram_message_id=first.telegram_message_id,
        )

        with pytest.raises(ConstraintViolationError):
            await messages.add(duplicate)

    async def test_many_messages_without_an_identifier_are_permitted(
        self, subject: MessageSubject
    ) -> None:
        # The partial index. A non-partial one would reject the second message
        # from any source that issues no identifiers -- which is every source
        # except Telegram (ADR-045).
        messages = repo(subject, ACCOUNT_A)

        for index in range(1, 4):
            await messages.add(make_unidentified(index))

        page = await messages.list_by_chat(chat_id_for(ACCOUNT_A), PageRequest(limit=50))
        assert len(page) == 3

    async def test_the_same_telegram_id_may_appear_in_two_accounts(
        self, subject: MessageSubject
    ) -> None:
        # Telegram message identifiers are unique only within a chat, so the
        # same number names a different message elsewhere.
        for account_id in (ACCOUNT_A, ACCOUNT_B):
            await repo(subject, account_id).add(make_message(1, account_id=account_id))

        assert (
            await repo(subject, ACCOUNT_A).get_by_telegram_id(
                chat_id_for(ACCOUNT_A), TelegramMessageId(101)
            )
            is not None
        )
        assert (
            await repo(subject, ACCOUNT_B).get_by_telegram_id(
                chat_id_for(ACCOUNT_B), TelegramMessageId(102)
            )
            is not None
        )


class TestOwnership:
    """A message belongs to one account, and to one of that account's chats."""

    async def test_exposes_its_scope(self, subject: MessageSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    async def test_a_foreign_message_cannot_be_added(self, subject: MessageSubject) -> None:
        with pytest.raises(DomainValidationError, match="scoped to account"):
            await repo(subject, ACCOUNT_A).add(make_message(1, account_id=ACCOUNT_B))

    async def test_a_message_cannot_be_filed_in_another_accounts_chat(
        self, subject: MessageSubject
    ) -> None:
        # The composite foreign key (ADR-043). A simple chat_id reference would
        # accept this row.
        trespasser = Message.record(
            message_id=MessageId(77_777),
            account_id=ACCOUNT_A,
            chat_id=chat_id_for(ACCOUNT_B),
            sender_kind=SenderKind.CONTACT,
            text="filed in the wrong place",
            sent_at=EPOCH,
            ingested_at=INGESTED,
        )

        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(trespasser)

    async def test_a_message_for_a_missing_chat_is_refused(self, subject: MessageSubject) -> None:
        orphan = Message.record(
            message_id=MessageId(77_777),
            account_id=ACCOUNT_A,
            chat_id=ChatId(999_999),
            sender_kind=SenderKind.CONTACT,
            text="nowhere",
            sent_at=EPOCH,
            ingested_at=INGESTED,
        )

        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(orphan)

    async def test_one_account_does_not_see_another(self, subject: MessageSubject) -> None:
        message = make_message(1, account_id=ACCOUNT_A)
        await repo(subject, ACCOUNT_A).add(message)

        assert await repo(subject, ACCOUNT_B).get(message.id) is None

    async def test_a_history_read_does_not_cross_accounts(self, subject: MessageSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_message(1, account_id=ACCOUNT_A))

        page = await repo(subject, ACCOUNT_B).list_by_chat(
            chat_id_for(ACCOUNT_A), PageRequest(limit=50)
        )

        assert len(page) == 0

    async def test_no_method_accepts_an_account_argument(self) -> None:
        for name in ("get", "add", "get_by_telegram_id", "list_by_chat"):
            signature = inspect.signature(getattr(SqlMessageRepository, name))
            assert "account_id" not in signature.parameters


class TestHistoryOrdering:
    """History is read in the order messages were sent, not ingested."""

    async def test_newest_sent_first(self, subject: MessageSubject) -> None:
        messages = repo(subject, ACCOUNT_A)
        for index in (1, 2, 3):
            await messages.add(make_message(index))

        page = await messages.list_by_chat(chat_id_for(ACCOUNT_A), PageRequest(limit=50))

        assert [m.text for m in page] == ["message 3", "message 2", "message 1"]

    async def test_a_backfilled_message_sorts_by_when_it_was_sent(
        self, subject: MessageSubject
    ) -> None:
        # The reason ordering is by sent_at. A backfill inserts old messages
        # after new ones, and history in insertion order would be nonsense.
        messages = repo(subject, ACCOUNT_A)
        await messages.add(make_message(5))

        backfilled = Message.record(
            message_id=MessageId(4242),
            account_id=ACCOUNT_A,
            chat_id=chat_id_for(ACCOUNT_A),
            sender_kind=SenderKind.CONTACT,
            text="sent years ago, ingested now",
            sent_at=EPOCH - timedelta(days=365),
            ingested_at=INGESTED + timedelta(days=1),
        )
        await messages.add(backfilled)

        page = await messages.list_by_chat(chat_id_for(ACCOUNT_A), PageRequest(limit=50))

        assert page.items[-1].id == backfilled.id

    async def test_messages_of_another_chat_are_excluded(self, subject: MessageSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_message(1))

        page = await repo(subject, ACCOUNT_A).list_by_chat(ChatId(999), PageRequest(limit=50))

        assert len(page) == 0


class TestCascadeDeletion:
    """Messages do not outlive the chat or the account that owns them."""

    async def test_deleting_a_chat_removes_its_messages(self, subject: MessageSubject) -> None:
        message = make_message(1)
        await repo(subject, ACCOUNT_A).add(message)

        await subject.delete_chat(chat_id_for(ACCOUNT_A))  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get(message.id) is None

    async def test_deleting_an_account_removes_its_messages(self, subject: MessageSubject) -> None:
        message = make_message(1)
        await repo(subject, ACCOUNT_A).add(message)

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get(message.id) is None

    async def test_deleting_one_account_leaves_the_others_messages(
        self, subject: MessageSubject
    ) -> None:
        await repo(subject, ACCOUNT_A).add(make_message(1, account_id=ACCOUNT_A))
        kept = make_message(1, account_id=ACCOUNT_B)
        await repo(subject, ACCOUNT_B).add(kept)

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_B).get(kept.id) is not None


class TestImplementationsAgree:
    """Both implementations behave identically."""

    async def test_a_message_round_trips_identically(
        self, sql_subject: MessageSubject, memory_subject: MessageSubject
    ) -> None:
        message = make_message(1)

        await repo(sql_subject, ACCOUNT_A).add(message)
        await repo(memory_subject, ACCOUNT_A).add(message)

        assert await repo(sql_subject, ACCOUNT_A).get(message.id) == (
            await repo(memory_subject, ACCOUNT_A).get(message.id)
        )

    async def test_a_message_without_text_round_trips_identically(
        self, sql_subject: MessageSubject, memory_subject: MessageSubject
    ) -> None:
        # Two nullable columns at once, which is where the two stores are most
        # likely to disagree.
        message = Message.record(
            message_id=MessageId(4242),
            account_id=ACCOUNT_A,
            chat_id=chat_id_for(ACCOUNT_A),
            sender_kind=SenderKind.CONTACT,
            message_type=MessageType.STICKER,
            sent_at=EPOCH,
            ingested_at=INGESTED,
        )

        await repo(sql_subject, ACCOUNT_A).add(message)
        await repo(memory_subject, ACCOUNT_A).add(message)

        from_sql = await repo(sql_subject, ACCOUNT_A).get(message.id)
        from_memory = await repo(memory_subject, ACCOUNT_A).get(message.id)

        assert from_sql == from_memory
        assert from_sql is not None
        assert from_sql.text is None
        assert from_sql.telegram_message_id is None
