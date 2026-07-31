"""The conversation repository, run against both implementations.

What is asserted here is ownership, the **composite** foreign key, cascade
deletion, scope isolation, and the two obligations this table exists for: that a
conversation's extent survives a round trip exactly, and that two conversations
in one chat cannot begin at the same instant.

That second one is the whole of "conversations do not overlap". Combined with
each being a contiguous run of messages, it is what makes membership-by-time-
range exact rather than merely usual (ADR-056).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.conversation_repository import (
    InMemoryConversationRepository,
    InMemoryConversationStore,
)
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    ConversationId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.conversation_repository import ConversationRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAlchemyUnitOfWork,
    SqlChatRepository,
    SqlContactRepository,
    SqlConversationRepository,
    SqliteDatabase,
)

EPOCH = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CHAT_A = ChatId(11)
CHAT_B = ChatId(22)
ABSENT_CHAT = ChatId(999)


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own a chat."""
    return Account.create(
        account_id=account_id,
        telegram_user_id=TelegramUserId(1000 + int(account_id)),
        display_name=f"account-{int(account_id)}",
        now=EPOCH,
        is_active=is_active,
    )


def make_contact(account_id: AccountId) -> Contact:
    """Build the contact a private chat needs."""
    return Contact.create(
        contact_id=ContactId(100 + int(account_id)),
        account_id=account_id,
        telegram_user_id=TelegramUserId(2000 + int(account_id)),
        display_name=f"person-{int(account_id)}",
        now=EPOCH,
    )


def make_chat(chat_id: ChatId, account_id: AccountId) -> Chat:
    """Build a private chat to hold conversations."""
    return Chat.private_with(
        chat_id=chat_id,
        account_id=account_id,
        telegram_chat_id=TelegramChatId(5000 + int(chat_id)),
        contact_id=ContactId(100 + int(account_id)),
        now=EPOCH,
    )


def make_conversation(  # noqa: PLR0913 - one argument per field a test varies
    conversation_id: int,
    chat_id: ChatId,
    account_id: AccountId,
    *,
    offset_minutes: int = 0,
    length_minutes: int = 30,
    message_count: int = 5,
) -> Conversation:
    """Build a conversation beginning at a chosen offset from the epoch."""
    started_at = EPOCH + timedelta(minutes=offset_minutes)
    return Conversation.spanning(
        conversation_id=ConversationId(conversation_id),
        account_id=account_id,
        chat_id=chat_id,
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=length_minutes),
        message_count=message_count,
        now=EPOCH,
    )


@dataclass
class ConversationSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_chat: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[ConversationSubject]:
    """The SQL repository against a migrated database with a chat per account."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "conversations.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    accounts = SqlAccountRepository(uow)
    await accounts.add(make_account(ACCOUNT_A, is_active=True))
    await accounts.add(make_account(ACCOUNT_B))
    for chat_id, account_id in ((CHAT_A, ACCOUNT_A), (CHAT_B, ACCOUNT_B)):
        await SqlContactRepository(uow, account_id).add(make_contact(account_id))
        await SqlChatRepository(uow, account_id).add(make_chat(chat_id, account_id))

    async def delete_chat(chat_id: ChatId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM chats WHERE id = :id"), {"id": int(chat_id)}
            )
        )

    try:
        yield ConversationSubject(
            for_account=lambda account_id: SqlConversationRepository(uow, account_id),
            delete_chat=delete_chat,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> ConversationSubject:
    """The in-memory repository against a shared store with a chat per account."""
    store = InMemoryConversationStore(
        chats={int(CHAT_A): int(ACCOUNT_A), int(CHAT_B): int(ACCOUNT_B)}
    )

    async def delete_chat(chat_id: ChatId) -> None:
        store.delete_chat(chat_id)

    return ConversationSubject(
        for_account=lambda account_id: InMemoryConversationRepository(store, account_id),
        delete_chat=delete_chat,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> ConversationSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: ConversationSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: ConversationSubject, account_id: AccountId) -> ConversationRepository:
    """Build a repository scoped to an account."""
    built: ConversationRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestConversationRepositoryContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: ConversationSubject) -> None:
        assert isinstance(repo(subject, ACCOUNT_A), ConversationRepository)

    def test_exposes_its_scope(self, subject: ConversationSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    async def test_absent_conversation_returns_none(self, subject: ConversationSubject) -> None:
        assert await repo(subject, ACCOUNT_A).get(ConversationId(1)) is None

    async def test_a_stored_conversation_reads_back_exactly(
        self, subject: ConversationSubject
    ) -> None:
        # The extent is the whole content of this aggregate, so a round trip
        # that lost a second of it would lose the conversation's meaning.
        conversations = repo(subject, ACCOUNT_A)
        episode = make_conversation(1, CHAT_A, ACCOUNT_A)

        await conversations.add(episode)

        assert await conversations.get(episode.id) == episode

    async def test_reads_are_snapshots_not_live_views(self, subject: ConversationSubject) -> None:
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))

        first = await conversations.get(ConversationId(1))
        second = await conversations.get(ConversationId(1))

        assert first == second
        assert first is not second

    async def test_a_second_conversation_with_one_identifier_is_refused(
        self, subject: ConversationSubject
    ) -> None:
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A, offset_minutes=60))


class TestNonOverlap:
    """The unique start, which is what makes membership-by-range exact."""

    async def test_two_conversations_cannot_begin_at_one_instant(
        self, subject: ConversationSubject
    ) -> None:
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await conversations.add(make_conversation(2, CHAT_A, ACCOUNT_A))

    async def test_two_chats_may_begin_at_the_same_instant(
        self, subject: ConversationSubject
    ) -> None:
        # The constraint is per chat. Two people can message you at once.
        await repo(subject, ACCOUNT_A).add(make_conversation(1, CHAT_A, ACCOUNT_A))

        await repo(subject, ACCOUNT_B).add(make_conversation(2, CHAT_B, ACCOUNT_B))

        assert await repo(subject, ACCOUNT_B).get(ConversationId(2)) is not None

    async def test_updating_onto_another_start_is_refused(
        self, subject: ConversationSubject
    ) -> None:
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))
        await conversations.add(make_conversation(2, CHAT_A, ACCOUNT_A, offset_minutes=600))

        moved = make_conversation(2, CHAT_A, ACCOUNT_A, offset_minutes=0)
        with pytest.raises(ConstraintViolationError):
            await conversations.update(moved)


class TestUpdatingAnExtent:
    async def test_a_changed_extent_reads_back(self, subject: ConversationSubject) -> None:
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))

        await conversations.update(
            make_conversation(1, CHAT_A, ACCOUNT_A, length_minutes=90, message_count=12)
        )

        found = await conversations.get(ConversationId(1))
        assert found is not None
        assert found.message_count == 12
        assert found.duration == timedelta(minutes=90)

    async def test_the_creation_time_is_not_rewritten(self, subject: ConversationSubject) -> None:
        # A rebuild must not make an existing conversation look newly
        # discovered.
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))
        later = EPOCH + timedelta(days=1)

        revised = make_conversation(1, CHAT_A, ACCOUNT_A, message_count=9)
        await conversations.update(
            revised.spanning_now(
                started_at=revised.started_at,
                ended_at=revised.ended_at,
                message_count=9,
                now=later,
            )
        )

        found = await conversations.get(ConversationId(1))
        assert found is not None
        assert found.created_at == EPOCH

    async def test_updating_an_absent_conversation_is_reported(
        self, subject: ConversationSubject
    ) -> None:
        with pytest.raises(RecordNotFoundError):
            await repo(subject, ACCOUNT_A).update(make_conversation(1, CHAT_A, ACCOUNT_A))


class TestDeleting:
    """The one repository with a delete, because the data is derived."""

    async def test_a_deleted_conversation_is_gone(self, subject: ConversationSubject) -> None:
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))

        await conversations.delete(ConversationId(1))

        assert await conversations.get(ConversationId(1)) is None

    async def test_deleting_an_absent_conversation_is_not_an_error(
        self, subject: ConversationSubject
    ) -> None:
        # A pass that computed the same removal twice has made no mistake.
        await repo(subject, ACCOUNT_A).delete(ConversationId(404))

    async def test_one_account_cannot_delete_anothers(self, subject: ConversationSubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_conversation(2, CHAT_B, ACCOUNT_B))

        await repo(subject, ACCOUNT_A).delete(ConversationId(2))

        assert await repo(subject, ACCOUNT_B).get(ConversationId(2)) is not None


class TestListing:
    async def test_it_returns_a_chats_conversations_newest_first(
        self, subject: ConversationSubject
    ) -> None:
        conversations = repo(subject, ACCOUNT_A)
        for index in range(3):
            await conversations.add(
                make_conversation(index + 1, CHAT_A, ACCOUNT_A, offset_minutes=index * 600)
            )

        page = await conversations.list_by_chat(CHAT_A, PageRequest(limit=10))

        assert [int(c.id) for c in page.items] == [3, 2, 1]

    async def test_list_from_returns_oldest_first(self, subject: ConversationSubject) -> None:
        # The pass reads its window in the order it computes boundaries in.
        conversations = repo(subject, ACCOUNT_A)
        for index in range(3):
            await conversations.add(
                make_conversation(index + 1, CHAT_A, ACCOUNT_A, offset_minutes=index * 600)
            )

        found = await conversations.list_from(CHAT_A, EPOCH)

        assert [int(c.id) for c in found] == [1, 2, 3]

    async def test_list_from_bounds_the_window(self, subject: ConversationSubject) -> None:
        conversations = repo(subject, ACCOUNT_A)
        for index in range(3):
            await conversations.add(
                make_conversation(index + 1, CHAT_A, ACCOUNT_A, offset_minutes=index * 600)
            )

        found = await conversations.list_from(CHAT_A, EPOCH + timedelta(minutes=600))

        assert [int(c.id) for c in found] == [2, 3]

    async def test_list_from_with_no_instant_returns_everything(
        self, subject: ConversationSubject
    ) -> None:
        conversations = repo(subject, ACCOUNT_A)
        for index in range(3):
            await conversations.add(
                make_conversation(index + 1, CHAT_A, ACCOUNT_A, offset_minutes=index * 600)
            )

        assert len(await conversations.list_from(CHAT_A)) == 3

    async def test_latest_before_finds_the_one_an_instant_lands_in(
        self, subject: ConversationSubject
    ) -> None:
        conversations = repo(subject, ACCOUNT_A)
        for index in range(3):
            await conversations.add(
                make_conversation(index + 1, CHAT_A, ACCOUNT_A, offset_minutes=index * 600)
            )

        found = await conversations.latest_before(CHAT_A, EPOCH + timedelta(minutes=610))

        assert found is not None
        assert int(found.id) == 2

    async def test_latest_before_is_inclusive(self, subject: ConversationSubject) -> None:
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))

        found = await conversations.latest_before(CHAT_A, EPOCH)

        assert found is not None
        assert int(found.id) == 1

    async def test_latest_before_returns_none_when_nothing_is_early_enough(
        self, subject: ConversationSubject
    ) -> None:
        # Which means the arriving message is older than everything segmented,
        # and the pass rebuilds the whole chat.
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A, offset_minutes=600))

        assert await conversations.latest_before(CHAT_A, EPOCH) is None

    async def test_another_chat_is_not_listed(self, subject: ConversationSubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_conversation(2, CHAT_B, ACCOUNT_B))

        assert await repo(subject, ACCOUNT_A).list_from(CHAT_A) == ()


class TestOwnership:
    """ADR-043, on a table derived from an account's own messages."""

    async def test_a_conversation_for_another_account_is_refused(
        self, subject: ConversationSubject
    ) -> None:
        with pytest.raises(DomainValidationError):
            await repo(subject, ACCOUNT_A).add(make_conversation(2, CHAT_B, ACCOUNT_B))

    async def test_a_conversation_naming_another_accounts_chat_is_refused(
        self, subject: ConversationSubject
    ) -> None:
        # The composite foreign key, and the reason it is composite. The entity
        # is well formed and the account owns the repository; only the pairing
        # is wrong.
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_conversation(1, CHAT_B, ACCOUNT_A))

    async def test_a_conversation_in_a_chat_that_does_not_exist_is_refused(
        self, subject: ConversationSubject
    ) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_conversation(1, ABSENT_CHAT, ACCOUNT_A))

    async def test_one_account_cannot_see_anothers(self, subject: ConversationSubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_conversation(2, CHAT_B, ACCOUNT_B))

        assert await repo(subject, ACCOUNT_A).get(ConversationId(2)) is None

    async def test_deleting_a_chat_removes_its_conversations(
        self, subject: ConversationSubject
    ) -> None:
        conversations = repo(subject, ACCOUNT_A)
        await conversations.add(make_conversation(1, CHAT_A, ACCOUNT_A))

        await subject.delete_chat(CHAT_A)  # type: ignore[operator]

        assert await conversations.get(ConversationId(1)) is None
