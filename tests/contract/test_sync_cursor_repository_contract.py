"""The sync cursor repository, run against both implementations.

One row per chat, so there is nothing to page. What is asserted here is
ownership, the **composite** foreign key, cascade deletion, scope isolation, and
the one obligation this table exists for: that the bookmark can be advanced and
read back exactly, because everything about resumability depends on it.

The composite key gets the most attention. A simple ``chat_id`` reference would
let one account's cursor name another account's chat, and a fake that accepted
one would make every backfill test built on it agree with a schema that refuses
it (ADR-043).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.sync_cursor_repository import (
    InMemorySyncCursorRepository,
    InMemorySyncCursorStore,
)
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.sync_cursor import SyncCursor
from tgassist.domain.ports.sync_cursor_repository import SyncCursorRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAlchemyUnitOfWork,
    SqlChatRepository,
    SqlContactRepository,
    SqliteDatabase,
    SqlSyncCursorRepository,
)

EPOCH = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
LATER = EPOCH + timedelta(minutes=5)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)

#: One chat per account, so "another account's chat" is a real subject rather
#: than a hypothetical one.
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
    """Build a private chat to be synchronised."""
    return Chat.private_with(
        chat_id=chat_id,
        account_id=account_id,
        telegram_chat_id=TelegramChatId(5000 + int(chat_id)),
        contact_id=ContactId(100 + int(account_id)),
        now=EPOCH,
    )


def make_cursor(chat_id: ChatId, account_id: AccountId) -> SyncCursor:
    """Build a cursor for a chat nothing has been stored from yet."""
    return SyncCursor.start(account_id=account_id, chat_id=chat_id, now=EPOCH)


def advanced(cursor: SyncCursor, *, oldest: int, newest: int) -> SyncCursor:
    """Return a cursor that has accounted for one batch."""
    return cursor.with_batch(
        oldest=TelegramMessageId(oldest), newest=TelegramMessageId(newest), now=LATER
    )


@dataclass
class CursorSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_chat: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[CursorSubject]:
    """The SQL repository against a migrated database with a chat per account."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "cursors.db"))
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
        yield CursorSubject(
            for_account=lambda account_id: SqlSyncCursorRepository(uow, account_id),
            delete_chat=delete_chat,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> CursorSubject:
    """The in-memory repository against a shared store with a chat per account."""
    store = InMemorySyncCursorStore(
        chats={int(CHAT_A): int(ACCOUNT_A), int(CHAT_B): int(ACCOUNT_B)}
    )

    async def delete_chat(chat_id: ChatId) -> None:
        store.delete_chat(chat_id)

    return CursorSubject(
        for_account=lambda account_id: InMemorySyncCursorRepository(store, account_id),
        delete_chat=delete_chat,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> CursorSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: CursorSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: CursorSubject, account_id: AccountId) -> SyncCursorRepository:
    """Build a repository scoped to an account."""
    built: SyncCursorRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestSyncCursorRepositoryContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: CursorSubject) -> None:
        assert isinstance(repo(subject, ACCOUNT_A), SyncCursorRepository)

    def test_exposes_its_scope(self, subject: CursorSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    async def test_absent_cursor_returns_none(self, subject: CursorSubject) -> None:
        # An ordinary state: a chat that has never been synchronised has no
        # bookmark, and that is what "start at the newest" means.
        assert await repo(subject, ACCOUNT_A).get(CHAT_A) is None

    async def test_stored_cursor_can_be_read_back(self, subject: CursorSubject) -> None:
        cursors = repo(subject, ACCOUNT_A)
        cursor = make_cursor(CHAT_A, ACCOUNT_A)

        await cursors.add(cursor)

        assert await cursors.get(CHAT_A) == cursor

    async def test_reads_are_snapshots_not_live_views(self, subject: CursorSubject) -> None:
        cursors = repo(subject, ACCOUNT_A)
        await cursors.add(make_cursor(CHAT_A, ACCOUNT_A))

        first = await cursors.get(CHAT_A)
        second = await cursors.get(CHAT_A)

        assert first == second
        assert first is not second

    async def test_a_second_cursor_for_one_chat_is_refused(self, subject: CursorSubject) -> None:
        # Exactly one cursor per chat, enforced by the primary key.
        cursors = repo(subject, ACCOUNT_A)
        await cursors.add(make_cursor(CHAT_A, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await cursors.add(make_cursor(CHAT_A, ACCOUNT_A))


class TestAdvancingTheBookmark:
    """The operation everything about resumability depends on."""

    async def test_both_ends_survive_the_round_trip(self, subject: CursorSubject) -> None:
        cursors = repo(subject, ACCOUNT_A)
        await cursors.add(make_cursor(CHAT_A, ACCOUNT_A))

        await cursors.update(advanced(make_cursor(CHAT_A, ACCOUNT_A), oldest=51, newest=100))

        found = await cursors.get(CHAT_A)
        assert found is not None
        assert found.oldest_synced_message_id == TelegramMessageId(51)
        assert found.newest_synced_message_id == TelegramMessageId(100)
        assert found.last_sync_at == LATER

    async def test_completion_and_its_horizon_survive_together(
        self, subject: CursorSubject
    ) -> None:
        # "Complete" without the horizon cannot distinguish reaching the
        # beginning from stopping where we were told to.
        cursors = repo(subject, ACCOUNT_A)
        await cursors.add(make_cursor(CHAT_A, ACCOUNT_A))
        horizon = EPOCH - timedelta(days=365)

        await cursors.update(make_cursor(CHAT_A, ACCOUNT_A).completed(LATER, horizon=horizon))

        found = await cursors.get(CHAT_A)
        assert found is not None
        assert found.backfill_complete
        assert found.backfill_horizon == horizon

    async def test_updating_an_absent_cursor_is_reported(self, subject: CursorSubject) -> None:
        # A batch accounted for against a bookmark that does not exist would
        # leave messages stored and nothing recording that they were.
        with pytest.raises(RecordNotFoundError):
            await repo(subject, ACCOUNT_A).update(make_cursor(CHAT_A, ACCOUNT_A))

    async def test_save_creates_when_absent(self, subject: CursorSubject) -> None:
        cursors = repo(subject, ACCOUNT_A)

        await cursors.save(make_cursor(CHAT_A, ACCOUNT_A))

        assert await cursors.get(CHAT_A) is not None

    async def test_save_replaces_when_present(self, subject: CursorSubject) -> None:
        cursors = repo(subject, ACCOUNT_A)
        await cursors.save(make_cursor(CHAT_A, ACCOUNT_A))

        await cursors.save(advanced(make_cursor(CHAT_A, ACCOUNT_A), oldest=51, newest=100))

        found = await cursors.get(CHAT_A)
        assert found is not None
        assert found.oldest_synced_message_id == TelegramMessageId(51)

    async def test_save_is_the_reset(self, subject: CursorSubject) -> None:
        # Resetting writes a fresh cursor rather than deleting one, so there is
        # no window in which a chat has messages and no bookmark.
        cursors = repo(subject, ACCOUNT_A)
        await cursors.save(advanced(make_cursor(CHAT_A, ACCOUNT_A), oldest=51, newest=100))

        await cursors.save(make_cursor(CHAT_A, ACCOUNT_A))

        found = await cursors.get(CHAT_A)
        assert found is not None
        assert found.oldest_synced_message_id is None
        assert not found.backfill_complete


class TestOwnership:
    """ADR-043, on the table that would otherwise be the easiest place to break it."""

    async def test_a_cursor_for_another_account_is_refused(self, subject: CursorSubject) -> None:
        with pytest.raises(DomainValidationError):
            await repo(subject, ACCOUNT_A).add(make_cursor(CHAT_B, ACCOUNT_B))

    async def test_updating_another_accounts_cursor_is_refused(
        self, subject: CursorSubject
    ) -> None:
        await repo(subject, ACCOUNT_B).add(make_cursor(CHAT_B, ACCOUNT_B))

        with pytest.raises(DomainValidationError):
            await repo(subject, ACCOUNT_A).update(make_cursor(CHAT_B, ACCOUNT_B))

    async def test_a_cursor_naming_another_accounts_chat_is_refused(
        self, subject: CursorSubject
    ) -> None:
        # The composite foreign key, and the whole reason it is composite. The
        # entity is well formed and the account owns the repository; only the
        # pairing is wrong.
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(
                SyncCursor.start(account_id=ACCOUNT_A, chat_id=CHAT_B, now=EPOCH)
            )

    async def test_a_cursor_for_a_chat_that_does_not_exist_is_refused(
        self, subject: CursorSubject
    ) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(
                SyncCursor.start(account_id=ACCOUNT_A, chat_id=ABSENT_CHAT, now=EPOCH)
            )

    async def test_one_account_cannot_see_anothers_cursor(self, subject: CursorSubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_cursor(CHAT_B, ACCOUNT_B))

        assert await repo(subject, ACCOUNT_A).get(CHAT_B) is None

    async def test_deleting_a_chat_removes_its_cursor(self, subject: CursorSubject) -> None:
        cursors = repo(subject, ACCOUNT_A)
        await cursors.add(make_cursor(CHAT_A, ACCOUNT_A))

        await subject.delete_chat(CHAT_A)  # type: ignore[operator]

        assert await cursors.get(CHAT_A) is None
