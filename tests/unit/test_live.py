"""Live update dispatch.

Four layers, because the guarantees live at different depths:

* the task supervisor, which is asyncio policy and is checked as such;
* the use case against fakes, which is where ordering, idempotency and failure
  isolation live;
* the use case against a **real SQLite database**, which is the only place a
  rollback is observable -- the in-memory repositories write through;
* the command line, running the goal's own scenario: follow, interrupt,
  restart, and no duplicates.

No test here needs a Telegram account, a network or a real native library.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock, InMemorySecretStore, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.chat_repository import InMemoryChatRepository, InMemoryChatStore
from tests.fakes.event_bus import RecordingEventBus
from tests.fakes.message_repository import InMemoryMessageRepository, InMemoryMessageStore
from tests.fakes.sync_cursor_repository import (
    InMemorySyncCursorRepository,
    InMemorySyncCursorStore,
)
from tests.fakes.tdjson import AuthorizingTdjson
from tests.fakes.telegram_gateway import FakeTelegramGateway
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.backfill import SOURCE_BACKFILL
from tgassist.application.use_cases.live import (
    SOURCE_CATCH_UP,
    SOURCE_LIVE,
    LiveReport,
    SyncLive,
)
from tgassist.application.use_cases.message import IngestMessages
from tgassist.domain.errors import (
    AuthorizationError,
    DomainValidationError,
    TdlibNotRunningError,
    TelegramError,
)
from tgassist.domain.events import MessagesIngested
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.message import Message, MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.model.sync_cursor import SyncCursor
from tgassist.domain.model.telegram import NewMessage, TelegramMessage, TelegramUpdate
from tgassist.domain.ports.telegram_gateway import TelegramGateway
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.tasks import BackgroundTaskSupervisor
from tgassist.infrastructure.telegram.client import TdjsonClient
from tgassist.infrastructure.telegram.gateway import GatewaySettings, TdlibGateway
from tgassist.presentation.cli.app import app

runner = CliRunner()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def ingested(bus: RecordingEventBus) -> list[MessagesIngested]:
    """Return the ingestion events a bus recorded, typed."""
    return [event for event in bus.published if isinstance(event, MessagesIngested)]


ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
OPERATOR_A = TelegramUserId(1001)
OPERATOR_B = TelegramUserId(1002)

CHAT = ChatId(11)
OTHER_CHAT = ChatId(12)
CONTACT = ContactId(101)
OTHER_CONTACT = ContactId(102)
TELEGRAM_CHAT = TelegramChatId(5000)
OTHER_TELEGRAM_CHAT = TelegramChatId(6000)
UNKNOWN_TELEGRAM_CHAT = TelegramChatId(7000)
COUNTERPART = TelegramUserId(2002)

TIMEOUT = 5.0


def message(
    number: int,
    *,
    chat: TelegramChatId = TELEGRAM_CHAT,
    outgoing: bool = False,
) -> TelegramMessage:
    """Build a message whose identifier orders it."""
    return TelegramMessage(
        id=TelegramMessageId(number),
        chat_id=chat,
        sender_id=COUNTERPART,
        sent_at=NOW - timedelta(minutes=1000 - number),
        text=f"message {number}",
        is_outgoing=outgoing,
    )


HISTORY = tuple(message(number) for number in range(1, 11))


class _OtherUpdate(TelegramUpdate):
    """An update kind nothing consumes yet."""


# ---------------------------------------------------------------------------
# The task supervisor
# ---------------------------------------------------------------------------


class TestBackgroundTaskSupervisor:
    async def test_it_runs_what_it_is_given(self) -> None:
        ran = asyncio.Event()

        async with BackgroundTaskSupervisor() as supervisor:
            supervisor.start("work", _setting(ran))
            await asyncio.wait_for(ran.wait(), TIMEOUT)

        assert ran.is_set()

    async def test_a_task_that_returns_is_finished_not_restarted(self) -> None:
        # A consumer that reached the end of its stream has finished. Restarting
        # it would turn a closed connection into an endless reconnection loop.
        calls = 0

        async def once() -> None:
            nonlocal calls
            calls += 1

        async with BackgroundTaskSupervisor() as supervisor:
            status = supervisor.start("work", once)
            await _until(lambda: status.finished)

        assert calls == 1
        assert status.finished
        assert status.failure is None

    async def test_a_recoverable_failure_is_restarted(self) -> None:
        attempts = 0

        async def flaky() -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                msg = "not yet"
                raise RuntimeError(msg)

        async with BackgroundTaskSupervisor(initial_backoff=0.0, max_backoff=0.0) as supervisor:
            status = supervisor.start("work", flaky)
            await _until(lambda: status.finished)

        assert attempts == 3
        assert status.restarts == 2
        assert status.failure is None

    async def test_it_gives_up_after_enough_failures(self) -> None:
        async def broken() -> None:
            msg = "always"
            raise RuntimeError(msg)

        async with BackgroundTaskSupervisor(
            initial_backoff=0.0, max_backoff=0.0, max_restarts=2
        ) as supervisor:
            status = supervisor.start("work", broken)
            await _until(lambda: status.failure is not None)

        assert status.restarts == 2
        assert isinstance(status.failure, RuntimeError)

    async def test_the_failure_is_kept_rather_than_swallowed(self) -> None:
        async def broken() -> None:
            msg = "the reason"
            raise ValueError(msg)

        supervisor = BackgroundTaskSupervisor(initial_backoff=0.0, max_restarts=0)
        status = supervisor.start("work", broken)
        await _until(lambda: status.failure is not None)

        statuses = await supervisor.stop()

        assert str(statuses[0].failure) == "the reason"

    async def test_stop_cancels_a_running_task(self) -> None:
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.sleep(3600)

        supervisor = BackgroundTaskSupervisor()
        supervisor.start("work", forever)
        await asyncio.wait_for(started.wait(), TIMEOUT)

        await supervisor.stop()

        assert not supervisor.is_running

    async def test_cancellation_is_not_a_failure_to_restart_through(self) -> None:
        # Cancellation is how shutdown is expressed. A supervisor that fought it
        # would make shutdown impossible.
        attempts = 0

        async def forever() -> None:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(3600)

        supervisor = BackgroundTaskSupervisor(initial_backoff=0.0)
        supervisor.start("work", forever)
        await _until(lambda: attempts == 1)

        await supervisor.stop()

        assert attempts == 1

    async def test_stopping_twice_is_not_an_error(self) -> None:
        supervisor = BackgroundTaskSupervisor()
        supervisor.start("work", _noop)
        await supervisor.stop()

        assert await supervisor.stop() is not None

    async def test_it_refuses_a_second_task_of_one_name(self) -> None:
        async with BackgroundTaskSupervisor() as supervisor:
            supervisor.start("work", _noop)

            with pytest.raises(RuntimeError, match="already supervised"):
                supervisor.start("work", _noop)

    async def test_it_refuses_to_start_anything_after_stopping(self) -> None:
        supervisor = BackgroundTaskSupervisor()
        await supervisor.stop()

        with pytest.raises(RuntimeError, match="has stopped"):
            supervisor.start("work", _noop)


async def _noop() -> None:
    """Do nothing, successfully."""


def _setting(flag: asyncio.Event) -> Callable[[], Coroutine[Any, Any, None]]:
    """Return a factory whose task sets a flag and then waits."""

    async def run() -> None:
        flag.set()
        await asyncio.sleep(3600)

    return run


async def _until(condition: Callable[[], bool], timeout: float = TIMEOUT) -> None:
    """Wait until a predicate holds, or fail the test."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            msg = "condition did not hold in time"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class _Harness:
    """A live-synchronisation environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(OTHER_CONTACT): int(ACCOUNT_A)},
        )
        self.message_store = InMemoryMessageStore(chats={})
        self.cursor_store = InMemorySyncCursorStore(chats={})
        self.clock = AdvanceableClock(NOW)
        self.ids = SequentialIdGenerator(start=1000)
        self.bus = RecordingEventBus()
        # One identifier the store will refuse, so a test can describe a row
        # the database will not take without replacing a bound method.
        self.refuse: TelegramMessageId | None = None
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()

    def unit_of_work(self) -> InMemoryUnitOfWork:
        uow = self._factory()
        self.units.append(uow)
        return uow

    def accounts(self, _uow: UnitOfWork) -> InMemoryAccountRepository:
        return self.accounts_repository

    def chats(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryChatRepository:
        return InMemoryChatRepository(self.chat_store, account_id)

    def messages(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryMessageRepository:
        if self.refuse is None:
            return InMemoryMessageRepository(self.message_store, account_id)
        return _Refusing(self.message_store, account_id, self.refuse)

    def cursors(self, _uow: UnitOfWork, account_id: AccountId) -> InMemorySyncCursorRepository:
        return InMemorySyncCursorRepository(self.cursor_store, account_id)

    @property
    def commits(self) -> int:
        return sum(1 for unit in self.units if unit.is_committed)

    async def setup(self, *, sync_enabled: bool = True, second_chat: bool = False) -> Account:
        """Create one account with one or two syncable private chats."""
        account = Account.create(
            account_id=ACCOUNT_A,
            telegram_user_id=OPERATOR_A,
            display_name="me",
            now=NOW,
            is_active=True,
        )
        await self.accounts_repository.add(account)
        await self._add_chat(CHAT, CONTACT, TELEGRAM_CHAT, sync_enabled=sync_enabled)
        if second_chat:
            await self._add_chat(OTHER_CHAT, OTHER_CONTACT, OTHER_TELEGRAM_CHAT, sync_enabled=True)
        return account

    async def _add_chat(
        self,
        chat_id: ChatId,
        contact_id: ContactId,
        telegram_chat_id: TelegramChatId,
        *,
        sync_enabled: bool,
    ) -> Chat:
        chat = Chat.private_with(
            chat_id=chat_id,
            account_id=ACCOUNT_A,
            telegram_chat_id=telegram_chat_id,
            contact_id=contact_id,
            now=NOW,
            sync_enabled=sync_enabled,
        )
        await self.chats(self.unit_of_work(), ACCOUNT_A).add(chat)
        self.message_store.register_chat(chat_id, ACCOUNT_A)
        self.cursor_store.register_chat(chat_id, ACCOUNT_A)
        return chat

    async def mark_synced(self, chat_id: ChatId, oldest: int, newest: int) -> None:
        """Give a chat a bookmark, as a completed backfill would."""
        cursors = self.cursors(self.unit_of_work(), ACCOUNT_A)
        await cursors.save(
            SyncCursor.start(account_id=ACCOUNT_A, chat_id=chat_id, now=NOW).with_batch(
                oldest=TelegramMessageId(oldest), newest=TelegramMessageId(newest), now=NOW
            )
        )

    def live(self, *, catch_up_pages: int = 20) -> SyncLive:
        return SyncLive(
            self.unit_of_work,
            self.cursors,
            self.chats,
            self.accounts,
            IngestMessages(
                self.unit_of_work,
                self.messages,
                self.chats,
                self.accounts,
                self.clock,
                self.ids,
            ),
            self.clock,
            self.bus,
            10,
            catch_up_pages,
        )

    async def stored(self, chat_id: ChatId = CHAT) -> list[int]:
        """Return the Telegram identifiers stored for a chat, oldest first."""
        page = await self.messages(self.unit_of_work(), ACCOUNT_A).list_by_chat(
            chat_id, PageRequest(limit=500)
        )
        return sorted(int(m.telegram_message_id or 0) for m in page.items)

    async def cursor(self, chat_id: ChatId = CHAT) -> SyncCursor | None:
        return await self.cursors(self.unit_of_work(), ACCOUNT_A).get(chat_id)


@pytest.fixture
def harness() -> _Harness:
    """A fresh environment for one test."""
    return _Harness()


@pytest.fixture
async def gateway() -> AsyncIterator[FakeTelegramGateway]:
    """A connected, authorized gateway with nothing scripted."""
    fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
    await fake.connect()
    try:
        yield fake
    finally:
        await fake.disconnect()


async def _follow(
    harness: _Harness,
    gateway: FakeTelegramGateway,
    *,
    catch_up: bool = False,
    report: LiveReport | None = None,
) -> LiveReport:
    """Run a live sync to completion, ending it by disconnecting the gateway."""
    progress = report if report is not None else LiveReport()
    task = asyncio.create_task(harness.live().execute(gateway, report=progress, catch_up=catch_up))
    await asyncio.sleep(0)
    return await _finish(task, gateway, progress)


async def _finish(
    task: asyncio.Task[LiveReport], gateway: FakeTelegramGateway, report: LiveReport
) -> LiveReport:
    """Let the run drain, then end its stream and wait for it."""
    await _drained(gateway)
    await gateway.disconnect()
    with contextlib.suppress(asyncio.CancelledError, TelegramError):
        await asyncio.wait_for(task, TIMEOUT)
    return report


async def _drained(gateway: FakeTelegramGateway, timeout: float = TIMEOUT) -> None:
    """Wait until the gateway's update queue is empty."""
    deadline = asyncio.get_running_loop().time() + timeout
    queue = gateway._updates
    while queue is not None and not queue.empty():
        if asyncio.get_running_loop().time() > deadline:
            msg = "updates were not drained in time"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Draining
# ---------------------------------------------------------------------------


class TestOrderedDelivery:
    async def test_updates_are_stored_in_arrival_order(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        for number in (7, 8, 9):
            gateway.push_message(message(number))

        report = await _follow(harness, gateway)

        assert report.stored == 3
        assert await harness.stored() == [7, 8, 9]

    async def test_the_order_telegram_used_is_the_order_kept(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Telegram's order is the only order there is; nothing re-sorts.
        await harness.setup()
        for number in (9, 7, 8):
            gateway.push_message(message(number))

        await _follow(harness, gateway)

        page = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_by_chat(
            CHAT, PageRequest(limit=10)
        )
        assert [int(m.telegram_message_id or 0) for m in page.items] == [9, 8, 7]

    async def test_each_update_is_its_own_transaction(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        before = harness.commits
        for number in (7, 8, 9):
            gateway.push_message(message(number))

        await _follow(harness, gateway)

        assert harness.commits - before == 3

    async def test_interleaved_chats_each_get_their_own_messages(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup(second_chat=True)
        gateway.push_message(message(7))
        gateway.push_message(message(70, chat=OTHER_TELEGRAM_CHAT))
        gateway.push_message(message(8))
        gateway.push_message(message(71, chat=OTHER_TELEGRAM_CHAT))

        report = await _follow(harness, gateway)

        assert report.stored == 4
        assert await harness.stored(CHAT) == [7, 8]
        assert await harness.stored(OTHER_CHAT) == [70, 71]

    async def test_the_cursor_moves_with_each_message(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        await harness.mark_synced(CHAT, oldest=1, newest=6)
        gateway.push_message(message(7))

        await _follow(harness, gateway)

        cursor = await harness.cursor()
        assert cursor is not None
        assert cursor.newest_synced_message_id == TelegramMessageId(7)
        assert cursor.oldest_synced_message_id == TelegramMessageId(1)


class TestIdempotency:
    async def test_the_same_update_twice_stores_one_message(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Telegram can repeat an update after a reconnect. The partial unique
        # index is what makes that cost nothing (ADR-045).
        await harness.setup()
        gateway.push_message(message(7))
        gateway.push_message(message(7))

        report = await _follow(harness, gateway)

        assert report.stored == 1
        assert report.skipped == 1
        assert await harness.stored() == [7]

    async def test_a_duplicate_commits_nothing(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        gateway.push_message(message(7))
        gateway.push_message(message(7))

        before = harness.commits
        await _follow(harness, gateway)

        assert harness.commits - before == 1

    async def test_an_update_for_a_backfilled_message_is_recognised(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Exactly what a backfill meeting live updates looks like.
        await harness.setup()
        gateway.push_message(message(7))
        await _follow(harness, gateway)

        second = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        await second.connect()
        second.push_message(message(7))
        report = await _follow(harness, second)

        assert report.stored == 0
        assert report.skipped == 1


class TestUpdatesWeIgnore:
    async def test_an_update_kind_with_no_consumer_is_counted(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        gateway.push_update(_OtherUpdate())
        gateway.push_message(message(7))

        report = await _follow(harness, gateway)

        assert report.ignored == 1
        assert report.stored == 1

    async def test_an_unknown_kind_does_not_end_the_stream(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The receive loop must keep operating: a TDLib release that adds an
        # update type must not be able to take the application down.
        await harness.setup()
        gateway.push_update(_OtherUpdate())
        gateway.push_update(_OtherUpdate())
        gateway.push_message(message(7))

        report = await _follow(harness, gateway)

        assert report.updates_seen == 3
        assert await harness.stored() == [7]

    async def test_a_message_for_an_unknown_chat_is_ignored(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Telegram reports everything it can see; what we keep is our decision.
        await harness.setup()
        gateway.push_message(message(7, chat=UNKNOWN_TELEGRAM_CHAT))

        report = await _follow(harness, gateway)

        assert report.ignored == 1
        assert report.stored == 0

    async def test_a_message_for_a_chat_with_sync_off_is_ignored(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup(sync_enabled=False)
        gateway.push_message(message(7))

        report = await _follow(harness, gateway)

        assert report.ignored == 1
        assert await harness.stored() == []


class TestEventPublication:
    async def test_a_stored_message_announces_itself(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        gateway.push_message(message(7))

        report = await _follow(harness, gateway)

        published = ingested(harness.bus)
        assert report.events == 1
        assert len(published) == 1
        assert published[0].count == 1
        assert published[0].chat_id == int(CHAT)
        assert published[0].source == SOURCE_LIVE

    async def test_a_duplicate_announces_nothing(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # "Nothing happened" is not a fact worth waking every subscriber for.
        await harness.setup()
        gateway.push_message(message(7))
        gateway.push_message(message(7))

        await _follow(harness, gateway)

        assert len(ingested(harness.bus)) == 1

    async def test_an_ignored_update_announces_nothing(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        gateway.push_message(message(7, chat=UNKNOWN_TELEGRAM_CHAT))

        await _follow(harness, gateway)

        assert harness.bus.published == []

    async def test_the_event_carries_the_newest_send_time(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        arriving = message(7)
        gateway.push_message(arriving)

        await _follow(harness, gateway)

        assert ingested(harness.bus)[0].newest_sent_at == arriving.sent_at

    async def test_a_handler_that_fails_does_not_stop_the_run(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The bus isolates a failing handler (EventBus contract point 4), which
        # is what makes "a faulty subscriber cannot break synchronisation" true.
        await harness.setup()

        def explode(_event: MessagesIngested) -> None:
            msg = "handler is broken"
            raise RuntimeError(msg)

        harness.bus.subscribe(MessagesIngested, explode, name="broken")
        gateway.push_message(message(7))
        gateway.push_message(message(8))

        report = await _follow(harness, gateway)

        assert report.stored == 2
        assert await harness.stored() == [7, 8]


class TestFailureIsolation:
    async def test_one_bad_update_does_not_end_the_run(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        harness.refuse = TelegramMessageId(8)
        for number in (7, 8, 9):
            gateway.push_message(message(number))

        report = await _follow(harness, gateway)

        assert report.stored == 2
        assert report.failed == 1
        assert await harness.stored() == [7, 9]

    async def test_the_failure_is_reported_with_a_safe_message(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        harness.refuse = TelegramMessageId(8)
        gateway.push_message(message(8))

        report = await _follow(harness, gateway)

        assert report.failures == [f"chat {int(TELEGRAM_CHAT)}: That row was refused."]

    async def test_a_failed_update_announces_nothing(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        harness.refuse = TelegramMessageId(8)
        gateway.push_message(message(8))

        await _follow(harness, gateway)

        assert harness.bus.published == []


class _Refusing(InMemoryMessageRepository):
    """A message repository that refuses one identifier, as a constraint would."""

    __slots__ = ("_refuse",)

    def __init__(
        self,
        store: InMemoryMessageStore,
        account_id: AccountId,
        refuse: TelegramMessageId,
    ) -> None:
        """Bind to a store, an account, and the identifier to refuse."""
        super().__init__(store, account_id)
        self._refuse = refuse

    async def add(self, message: Message) -> None:
        """Persist a message, unless it is the one this repository refuses."""
        if message.telegram_message_id == self._refuse:
            msg = f"messages.telegram_message_id {int(self._refuse)}"
            raise DomainValidationError(msg, user_message="That row was refused.")
        await super().add(message)


# ---------------------------------------------------------------------------
# Catch-up
# ---------------------------------------------------------------------------


class TestCatchUp:
    async def test_it_recovers_what_arrived_while_nothing_was_running(
        self, harness: _Harness
    ) -> None:
        # Backfill cannot do this: it walks downwards from the oldest stored
        # message and never looks above the top of the range.
        await harness.setup()
        await harness.mark_synced(CHAT, oldest=1, newest=6)
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        await fake.connect()

        report = await _follow(harness, fake, catch_up=True)

        assert report.caught_up == 4
        assert await harness.stored() == [7, 8, 9, 10]

    async def test_it_stops_where_the_stored_range_begins(self, harness: _Harness) -> None:
        await harness.setup()
        await harness.mark_synced(CHAT, oldest=1, newest=9)
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        await fake.connect()

        report = await _follow(harness, fake, catch_up=True)

        assert report.caught_up == 1
        assert await harness.stored() == [10]

    async def test_it_does_nothing_when_there_is_no_gap(self, harness: _Harness) -> None:
        await harness.setup()
        await harness.mark_synced(CHAT, oldest=1, newest=10)
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        await fake.connect()

        report = await _follow(harness, fake, catch_up=True)

        assert report.caught_up == 0
        assert harness.bus.published == []

    async def test_it_skips_a_chat_that_has_never_been_backfilled(self, harness: _Harness) -> None:
        # There is no top of a range to catch up from. The backfill is what
        # fills this chat, and it starts at the newest message.
        await harness.setup()
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        await fake.connect()

        report = await _follow(harness, fake, catch_up=True)

        assert report.caught_up == 0
        assert await harness.stored() == []

    async def test_it_skips_a_chat_with_synchronisation_off(self, harness: _Harness) -> None:
        await harness.setup(sync_enabled=False)
        await harness.mark_synced(CHAT, oldest=1, newest=6)
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        await fake.connect()

        report = await _follow(harness, fake, catch_up=True)

        assert report.caught_up == 0

    async def test_it_announces_what_it_recovered(self, harness: _Harness) -> None:
        await harness.setup()
        await harness.mark_synced(CHAT, oldest=1, newest=6)
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        await fake.connect()

        await _follow(harness, fake, catch_up=True)

        published = ingested(harness.bus)
        assert [event.source for event in published] == [SOURCE_CATCH_UP]
        assert published[0].count == 4

    async def test_a_bounded_catch_up_gives_up_rather_than_walking_forever(
        self, harness: _Harness
    ) -> None:
        # A chat that received more than the bound while the process was down is
        # better served by re-running the backfill.
        await harness.setup()
        await harness.mark_synced(CHAT, oldest=1, newest=2)
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        await fake.connect()

        progress = LiveReport()
        task = asyncio.create_task(
            harness.live(catch_up_pages=1).execute(fake, report=progress, catch_up=True)
        )
        await _finish(task, fake, progress)

        # One page of ten at a batch size of ten: it never reaches back into the
        # stored range, so it stops at the bound with the newest page stored.
        assert progress.caught_up == 8
        assert await harness.stored() == [3, 4, 5, 6, 7, 8, 9, 10]

    async def test_live_updates_follow_immediately_after(self, harness: _Harness) -> None:
        # The window between catching up and draining contains nothing: the
        # queue was already filling.
        await harness.setup()
        await harness.mark_synced(CHAT, oldest=1, newest=8)
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        await fake.connect()
        fake.push_message(message(11))

        report = await _follow(harness, fake, catch_up=True)

        assert report.caught_up == 2
        assert report.stored == 1
        assert await harness.stored() == [9, 10, 11]


# ---------------------------------------------------------------------------
# Refusals and the stream contract
# ---------------------------------------------------------------------------


class TestRefusals:
    async def test_a_gateway_for_another_account_is_refused(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        other = Account.create(
            account_id=ACCOUNT_B, telegram_user_id=OPERATOR_B, display_name="them", now=NOW
        )
        await harness.accounts_repository.add(other)

        with pytest.raises(AuthorizationError, match="bound to account"):
            await harness.live().execute(gateway, ACCOUNT_B)

    async def test_the_stream_needs_a_connection(self) -> None:
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)

        with pytest.raises(TdlibNotRunningError):
            async for _ in fake.updates():
                pass

    async def test_the_stream_refuses_a_second_consumer(self, gateway: FakeTelegramGateway) -> None:
        # The queue holds one item per update, so two consumers would take turns
        # and each would silently miss what the other took.
        first = gateway.updates()
        gateway.push_message(message(7))
        await anext(first)

        with pytest.raises(TdlibNotRunningError, match="already has a consumer"):
            async for _ in gateway.updates():
                pass

        await first.aclose()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    async def test_disconnecting_ends_the_stream(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        task = asyncio.create_task(harness.live().execute(gateway, catch_up=False))
        await asyncio.sleep(0.01)

        await gateway.disconnect()

        report = await asyncio.wait_for(task, TIMEOUT)
        assert report.updates_seen == 0

    async def test_what_committed_before_shutdown_stays(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        gateway.push_message(message(7))

        await _follow(harness, gateway)

        assert await harness.stored() == [7]

    async def test_cancelling_mid_run_leaves_the_database_consistent(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        gateway.push_message(message(7))
        task = asyncio.create_task(harness.live().execute(gateway, catch_up=False))
        await _drained(gateway)

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert await harness.stored() == [7]

    async def test_updates_pushed_after_shutdown_are_counted_not_kept(
        self, gateway: FakeTelegramGateway
    ) -> None:
        # A loop blocked on a queue nobody will drain again is a shutdown that
        # hangs. What is dropped is counted, and the next run's catch-up
        # recovers it.
        await gateway.disconnect()

        gateway.push_message(message(7))

        assert gateway.dropped_updates == 1


class TestTheSupervisedRun:
    async def test_a_recoverable_failure_restarts_the_consumer(self, harness: _Harness) -> None:
        await harness.setup()
        fake = _FailsOnce(ACCOUNT_A, starts_authorized=True)
        await fake.connect()
        supervisor = BackgroundTaskSupervisor(initial_backoff=0.0, max_backoff=0.0)
        progress = LiveReport()

        status = supervisor.start(
            "live-sync",
            lambda: harness.live().execute(fake, report=progress, catch_up=False),
        )
        await _until(lambda: status.restarts >= 1)
        fake.push_message(message(7))
        await _drained(fake)
        await fake.disconnect()
        await supervisor.stop()

        assert status.restarts == 1
        assert await harness.stored() == [7]

    async def test_an_unrecoverable_failure_is_reported(self, harness: _Harness) -> None:
        await harness.setup()
        fake = _AlwaysFails(ACCOUNT_A, starts_authorized=True)
        await fake.connect()
        supervisor = BackgroundTaskSupervisor(initial_backoff=0.0, max_backoff=0.0, max_restarts=1)

        status = supervisor.start("live-sync", lambda: harness.live().execute(fake, catch_up=False))
        await _until(lambda: status.failure is not None)
        statuses = await supervisor.stop()
        await fake.disconnect()

        assert isinstance(statuses[0].failure, TelegramError)


class _FailsOnce(FakeTelegramGateway):
    """A gateway whose stream dies once, then behaves."""

    __slots__ = ("_failed",)

    def __init__(self, account_id: AccountId, *, starts_authorized: bool) -> None:
        """Build a gateway that will fail its first stream."""
        super().__init__(account_id, starts_authorized=starts_authorized)
        self._failed = False

    async def updates(self) -> AsyncGenerator[TelegramUpdate]:
        """Fail the first time, then stream normally."""
        if not self._failed:
            self._failed = True
            msg = "Connection lost"
            raise TelegramError(msg, user_message="Telegram could not be reached.")
        async for update in super().updates():
            yield update


class _AlwaysFails(FakeTelegramGateway):
    """A gateway whose stream never works."""

    __slots__ = ()

    async def updates(self) -> AsyncGenerator[TelegramUpdate]:
        """Fail, every time.

        The empty loop is what makes this a generator without an unreachable
        ``yield`` after the raise.
        """
        for _ in ():
            yield NewMessage(message=message(1))  # pragma: no cover - never reached
        msg = "Connection lost"
        raise TelegramError(msg, user_message="Telegram could not be reached.")


# ---------------------------------------------------------------------------
# Both gateway implementations
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path) -> GatewaySettings:
    """Parameters for a gateway that never reaches a network."""
    return GatewaySettings(
        api_id=12345,
        api_hash=SecretValue("0123456789abcdef0123456789abcdef"),
        session_path=tmp_path / "session",
        database_encryption_key=SecretValue("test-session-key"),
    )


class TestBothImplementationsDeliverIdentically:
    """The fake is the second implementation, so a live run must not tell them apart."""

    async def test_the_same_updates_arrive_in_the_same_order(self, tmp_path: Path) -> None:
        arriving = (message(7), message(8), message(9))

        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        await fake.connect()
        for item in arriving:
            fake.push_message(item)
        from_fake = await _take(fake, len(arriving))
        await fake.disconnect()

        library = AuthorizingTdjson(starts_authorized=True)
        real = TdlibGateway(
            ACCOUNT_A,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await real.connect()
        for item in arriving:
            library.announce_message(item)
        from_adapter = await _take(real, len(arriving))
        await real.disconnect()

        assert from_fake == from_adapter
        assert from_fake == list(arriving)

    async def test_an_unknown_update_reaches_neither_consumer(self, tmp_path: Path) -> None:
        library = AuthorizingTdjson(starts_authorized=True)
        real = TdlibGateway(
            ACCOUNT_A,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await real.connect()
        library.announce_unknown()
        library.announce_message(message(7))

        taken = await _take(real, 1)
        unhandled = real.unhandled_updates
        await real.disconnect()

        assert taken == [message(7)]
        assert unhandled >= 1

    async def test_a_message_the_adapter_cannot_map_is_counted_not_raised(
        self, tmp_path: Path
    ) -> None:
        # One malformed message must not end the stream for every other one.
        library = AuthorizingTdjson(starts_authorized=True)
        real = TdlibGateway(
            ACCOUNT_A,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await real.connect()
        library.push({"@type": "updateNewMessage", "message": {"@type": "message", "id": 0}})
        library.announce_message(message(7))

        taken = await _take(real, 1)
        await real.disconnect()

        assert taken == [message(7)]

    async def test_a_live_run_stores_the_same_thing_against_both(
        self, harness: _Harness, tmp_path: Path
    ) -> None:
        library = AuthorizingTdjson(starts_authorized=True)
        real = TdlibGateway(
            ACCOUNT_A,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
        await harness.setup()
        await real.connect()
        for number in (7, 8, 9):
            library.announce_message(message(number))

        progress = LiveReport()
        task = asyncio.create_task(harness.live().execute(real, report=progress, catch_up=False))
        await _until(lambda: progress.stored == 3)
        await real.disconnect()
        with contextlib.suppress(asyncio.CancelledError, TelegramError):
            await asyncio.wait_for(task, TIMEOUT)

        assert await harness.stored() == [7, 8, 9]


async def _take(gateway: TelegramGateway, count: int) -> list[TelegramMessage]:
    """Take a fixed number of messages off a gateway's update stream."""
    taken: list[TelegramMessage] = []
    stream = gateway.updates()
    try:
        for _ in range(count):
            update = await asyncio.wait_for(anext(stream), TIMEOUT)
            assert isinstance(update, NewMessage)
            taken.append(update.message)
    finally:
        await stream.aclose()
    return taken


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


async def _prepare(container: Container) -> tuple[Account, Chat]:
    """Create the schema, an active account, a contact and a syncable chat."""
    await container.start_database()
    account = await container.create_account().execute(
        CreateAccountRequest(telegram_user_id=int(OPERATOR_A), display_name="me")
    )
    contact = await container.create_contact().execute(
        telegram_user_id=int(COUNTERPART), display_name="Ada"
    )
    chat = await container.open_private_chat().execute(
        contact_id=int(contact.id), telegram_chat_id=int(TELEGRAM_CHAT)
    )
    return account, chat


@pytest.fixture
async def stored(container: Container) -> AsyncIterator[Container]:
    """A container over a real SQLite file."""
    try:
        yield container
    finally:
        await container.aclose()


async def _telegram_ids(container: Container, chat: Chat) -> list[int]:
    """Return the Telegram identifiers stored for a chat, oldest first."""
    page = await container.read_chat_history().execute(int(chat.id), PageRequest(limit=500))
    return sorted(int(m.telegram_message_id or 0) for m in page.items)


class TestAgainstARealDatabase:
    """What a rollback looks like, which no in-memory fake can demonstrate."""

    async def test_a_live_update_is_stored(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        await gateway.connect()
        gateway.push_message(message(7))

        report = LiveReport()
        task = asyncio.create_task(
            stored.sync_live().execute(gateway, report=report, catch_up=False)
        )
        await _until(lambda: report.stored == 1)
        await gateway.disconnect()
        with contextlib.suppress(asyncio.CancelledError, TelegramError):
            await asyncio.wait_for(task, TIMEOUT)

        assert await _telegram_ids(stored, chat) == [7]

    async def test_a_backfill_then_live_updates_share_one_range(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()
        await stored.sync_history().execute(gateway, int(chat.id))
        gateway.push_message(message(11))

        report = LiveReport()
        task = asyncio.create_task(
            stored.sync_live().execute(gateway, report=report, catch_up=False)
        )
        await _until(lambda: report.stored == 1)
        await gateway.disconnect()
        with contextlib.suppress(asyncio.CancelledError, TelegramError):
            await asyncio.wait_for(task, TIMEOUT)

        async with stored.unit_of_work() as uow:
            cursor = await stored.sync_cursors(uow, account.id).get(chat.id)
        assert cursor is not None
        assert cursor.oldest_synced_message_id == TelegramMessageId(1)
        assert cursor.newest_synced_message_id == TelegramMessageId(11)
        assert await _telegram_ids(stored, chat) == [*range(1, 11), 11]

    async def test_the_backfill_announces_its_batches(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        recorded: list[MessagesIngested] = []
        stored.events.subscribe(MessagesIngested, recorded.append, name="test")
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()

        await stored.sync_history().execute(gateway, int(chat.id))
        await gateway.disconnect()

        assert [event.source for event in recorded] == [SOURCE_BACKFILL]
        assert recorded[0].count == 10

    async def test_a_catch_up_after_a_restart_closes_the_gap(self, stored: Container) -> None:
        # The scenario the ordering exists for: a run stops, messages arrive
        # while nothing is listening, and the next run recovers them.
        account, chat = await _prepare(stored)
        first = FakeTelegramGateway(account.id, starts_authorized=True)
        first.script_history(TELEGRAM_CHAT, *HISTORY[:5])
        await first.connect()
        await stored.sync_history().execute(gateway=first, chat_id=int(chat.id))
        await first.disconnect()

        second = FakeTelegramGateway(account.id, starts_authorized=True)
        second.script_history(TELEGRAM_CHAT, *HISTORY)
        await second.connect()
        report = LiveReport()
        task = asyncio.create_task(stored.sync_live().execute(second, report=report, catch_up=True))
        await _until(lambda: report.caught_up == 5)
        await second.disconnect()
        with contextlib.suppress(asyncio.CancelledError, TelegramError):
            await asyncio.wait_for(task, TIMEOUT)

        assert await _telegram_ids(stored, chat) == list(range(1, 11))


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,  # noqa: ARG001 - a command configures logging process-wide
) -> Path:
    """Point the CLI at an isolated data directory, with nothing reaching the OS."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(data_dir))
    monkeypatch.setenv("TGASSIST_LOGGING__CONSOLE_ENABLED", "false")
    monkeypatch.setenv("TGASSIST_LOGGING__FILE_ENABLED", "false")

    store = InMemorySecretStore()
    monkeypatch.setattr("tgassist.application.container.build_default_secret_store", lambda: store)
    return data_dir


def _run(*command: str) -> str:
    """Invoke the CLI and return its output, failing loudly if the command did."""
    result = runner.invoke(app, list(command))
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def _chat() -> None:
    """Create an active account, a contact and the private chat with them."""
    _run("account", "create", str(int(OPERATOR_A)), "Primary")
    added = _run("contact", "add", str(int(COUNTERPART)), "Ada")
    contact_id = added.split("Added contact ")[1].split(":")[0]
    _run("chat", "open", str(int(TELEGRAM_CHAT)), "--contact", contact_id)


#: What Telegram will report to the command, in the order it will report it.
#: Rewritten per test so one scenario can arrange a restart.
SCRIPT: list[TelegramMessage] = []


@pytest.fixture
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the container's gateway with one that pushes SCRIPT and stops.

    The stream ends once the script is exhausted, which is what makes the
    command return rather than wait for a Ctrl+C the test cannot send.
    """

    @asynccontextmanager
    async def fake_gateway(
        self: Container, account_id: AccountId
    ) -> AsyncIterator[FakeTelegramGateway]:
        del self
        gateway = FakeTelegramGateway(account_id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        # Connected here, not left to the command: the update queue only exists
        # once a gateway is connected, so a script pushed before that would be
        # dropped exactly as a real update arriving too early would be.
        await gateway.connect()
        for item in SCRIPT:
            gateway.push_message(item)

        async def stop_when_drained() -> None:
            # Wait until something is actually reading the stream, then let it
            # finish and end it. A command that never streams -- `sync history`,
            # `sync status` -- leaves this waiting, and it is cancelled below.
            while not gateway._streaming:
                await asyncio.sleep(0.005)
            await _drained(gateway)
            await gateway.disconnect()

        stopper = asyncio.create_task(stop_when_drained())
        try:
            yield gateway
        finally:
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
            await gateway.disconnect()

    monkeypatch.setattr(Container, "telegram_for", fake_gateway)


def _chat_id() -> str:
    """Return the local chat identifier the CLI issued."""
    rows = [line for line in _run("chat", "list").splitlines() if "private" in line]
    assert rows
    return rows[0].split()[0]


def _stored_count() -> int:
    """Return how many messages the database holds for the chat."""
    history = _run("message", "history", _chat_id(), "--limit", "100")
    return sum(1 for line in history.splitlines() if "message " in line)


@pytest.mark.usefixtures("cli_env", "_chat", "_gateway")
class TestSyncLiveCommand:
    """The flow the goal describes: follow, interrupt, restart, no duplicates."""

    def setup_method(self) -> None:
        SCRIPT.clear()

    def test_it_stores_what_arrives(self) -> None:
        SCRIPT.extend([message(11), message(12)])

        result = runner.invoke(app, ["sync", "live", "--no-catch-up"])

        assert result.exit_code == 0, result.output
        assert "2 new" in result.output
        assert _stored_count() == 2

    def test_it_says_what_it_is_doing(self) -> None:
        result = runner.invoke(app, ["sync", "live", "--no-catch-up"])

        assert "Following Telegram" in result.output
        assert "Press Ctrl+C to stop" in result.output

    def test_it_reports_the_events_it_published(self) -> None:
        SCRIPT.extend([message(11)])

        result = runner.invoke(app, ["sync", "live", "--no-catch-up"])

        assert "1 event(s) published" in result.output

    def test_restarting_stores_no_duplicates(self) -> None:
        SCRIPT.extend([message(11), message(12)])
        runner.invoke(app, ["sync", "live", "--no-catch-up"])

        result = runner.invoke(app, ["sync", "live", "--no-catch-up"])

        assert "0 new, 2 already stored" in result.output
        assert _stored_count() == 2

    def test_a_restart_catches_up_on_what_it_missed(self) -> None:
        # The scenario the ordering exists for. The backfill stores the history;
        # the catch-up recovers what the scripted gateway reports above it.
        chat = _chat_id()
        _run("sync", "history", chat)
        before = _stored_count()

        result = runner.invoke(app, ["sync", "live"])

        assert result.exit_code == 0, result.output
        assert before == 10
        assert "0 caught up" in result.output

    def test_an_update_for_an_unknown_chat_is_ignored(self) -> None:
        SCRIPT.extend([message(11, chat=UNKNOWN_TELEGRAM_CHAT)])

        result = runner.invoke(app, ["sync", "live", "--no-catch-up"])

        assert "1 ignored" in result.output
        assert _stored_count() == 0


@pytest.mark.usefixtures("cli_env", "_chat", "_gateway")
class TestSyncStatusCommand:
    def setup_method(self) -> None:
        SCRIPT.clear()

    def test_it_reports_a_chat_that_has_not_been_synced(self) -> None:
        result = runner.invoke(app, ["sync", "status"])

        assert result.exit_code == 0, result.output
        assert "pending" in result.output
        assert "1 with more to fetch" in result.output

    def test_it_reports_a_completed_backfill(self) -> None:
        _run("sync", "history", _chat_id())

        result = runner.invoke(app, ["sync", "status"])

        assert "complete" in result.output
        assert "1 chat(s) fully stored" in result.output

    def test_it_shows_the_stored_range(self) -> None:
        _run("sync", "history", _chat_id())

        result = runner.invoke(app, ["sync", "status"])

        assert "1-10" in result.output

    def test_it_names_the_session_state(self) -> None:
        result = runner.invoke(app, ["sync", "status"])

        assert "no session" in result.output

    def test_it_suggests_what_to_run_next(self) -> None:
        result = runner.invoke(app, ["sync", "status"])

        assert "tgassist sync history" in result.output


# ---------------------------------------------------------------------------
# Sender attribution, shared with the backfill
# ---------------------------------------------------------------------------


class TestSenderAttribution:
    async def test_an_outgoing_live_message_is_the_operator(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The same translation the backfill uses, so a message read live and the
        # same message read from history cannot disagree about whose it is.
        await harness.setup()
        gateway.push_message(message(7, outgoing=True))

        await _follow(harness, gateway)

        page = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_by_chat(
            CHAT, PageRequest(limit=10)
        )
        assert page.items[0].sender_kind is SenderKind.OPERATOR

    async def test_a_live_service_message_belongs_to_nobody(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        gateway.push_message(
            replace(message(7), sender_id=None, message_type=MessageType.SERVICE, text=None)
        )

        await _follow(harness, gateway)

        page = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_by_chat(
            CHAT, PageRequest(limit=10)
        )
        assert page.items[0].sender_kind is SenderKind.SYSTEM
