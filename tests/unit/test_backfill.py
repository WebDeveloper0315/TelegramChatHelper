"""Resumable history backfill.

Three layers, because the guarantees live at different depths:

* the cursor itself, which is a domain rule and is checked as one;
* the use case against fakes, which is where the loop's behaviour lives;
* the use case against a **real SQLite database**, which is the only place crash
  safety can be observed at all. The in-memory repositories write through
  immediately, so a rollback is invisible to them -- and rollback is the whole
  of what "the cursor never advances past what was persisted" means.

The last layer includes the test this slice exists for: an exception thrown
immediately before commit, proving that no message was persisted and the
bookmark did not move.

No test here needs a Telegram account, a network or a real native library.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from tgassist.application.use_cases.backfill import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_HORIZON_DAYS,
    BackfillStop,
    SyncHistory,
)
from tgassist.application.use_cases.contact import ContactTransition
from tgassist.application.use_cases.message import IngestMessages
from tgassist.domain.errors import (
    AuthorizationError,
    DomainValidationError,
    RecordNotFoundError,
    TelegramError,
)
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
from tgassist.domain.model.message import MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.model.sync_cursor import SyncCursor
from tgassist.domain.model.telegram import HistoryPage, TelegramMessage
from tgassist.domain.ports.sync_cursor_repository import SyncCursorRepository
from tgassist.domain.ports.telegram_gateway import TelegramGateway
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.telegram.client import TdjsonClient
from tgassist.infrastructure.telegram.gateway import GatewaySettings, TdlibGateway
from tgassist.presentation.cli.app import app

runner = CliRunner()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
OPERATOR_A = TelegramUserId(1001)
OPERATOR_B = TelegramUserId(1002)

CHAT = ChatId(11)
CONTACT = ContactId(101)
TELEGRAM_CHAT = TelegramChatId(5000)
COUNTERPART = TelegramUserId(2002)

TIMEOUT = 5.0


def message(number: int, *, age_days: int = 0, outgoing: bool = False) -> TelegramMessage:
    """Build a message whose identifier orders it, newest identifier last.

    ``age_days`` places it before the horizon when it needs to be, which is how
    the horizon tests describe an old chat without inventing a second clock.
    """
    return TelegramMessage(
        id=TelegramMessageId(number),
        chat_id=TELEGRAM_CHAT,
        sender_id=COUNTERPART,
        sent_at=NOW - timedelta(days=age_days, minutes=1000 - number),
        text=f"message {number}",
        is_outgoing=outgoing,
    )


#: Twenty-five messages, so a batch size of ten gives three pages with a short
#: last one -- the shape a real chat has.
HISTORY = tuple(message(number) for number in range(1, 26))


# ---------------------------------------------------------------------------
# The cursor
# ---------------------------------------------------------------------------


class TestSyncCursor:
    def _fresh(self) -> SyncCursor:
        return SyncCursor.start(account_id=ACCOUNT_A, chat_id=CHAT, now=NOW)

    def test_a_fresh_cursor_resumes_from_the_newest(self) -> None:
        # None is what "start at the newest" means to the gateway, so the first
        # run needs no special case.
        assert self._fresh().resume_from is None
        assert not self._fresh().has_synced

    def test_a_batch_sets_both_ends(self) -> None:
        moved = self._fresh().with_batch(
            oldest=TelegramMessageId(16), newest=TelegramMessageId(25), now=NOW
        )

        assert moved.oldest_synced_message_id == TelegramMessageId(16)
        assert moved.newest_synced_message_id == TelegramMessageId(25)
        assert moved.resume_from == TelegramMessageId(16)

    def test_the_floor_only_moves_down(self) -> None:
        first = self._fresh().with_batch(
            oldest=TelegramMessageId(16), newest=TelegramMessageId(25), now=NOW
        )

        second = first.with_batch(
            oldest=TelegramMessageId(6), newest=TelegramMessageId(15), now=NOW
        )

        assert second.oldest_synced_message_id == TelegramMessageId(6)

    def test_the_ceiling_only_moves_up(self) -> None:
        # Backfill never returns anything newer, but expressing the ceiling as a
        # maximum is what lets live updates advance it through the same method.
        first = self._fresh().with_batch(
            oldest=TelegramMessageId(16), newest=TelegramMessageId(25), now=NOW
        )

        second = first.with_batch(
            oldest=TelegramMessageId(6), newest=TelegramMessageId(15), now=NOW
        )

        assert second.newest_synced_message_id == TelegramMessageId(25)

    def test_a_batch_that_is_not_a_range_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot end at"):
            self._fresh().with_batch(
                oldest=TelegramMessageId(25), newest=TelegramMessageId(16), now=NOW
            )

    def test_a_cursor_with_one_end_is_unrepresentable(self) -> None:
        with pytest.raises(DomainValidationError, match="one end of its range"):
            replace(self._fresh(), oldest_synced_message_id=TelegramMessageId(5))

    def test_a_cursor_whose_ends_are_inverted_is_unrepresentable(self) -> None:
        with pytest.raises(DomainValidationError, match="is newer than its newest"):
            replace(
                self._fresh(),
                oldest_synced_message_id=TelegramMessageId(9),
                newest_synced_message_id=TelegramMessageId(4),
            )

    def test_completion_records_the_horizon_it_meant(self) -> None:
        horizon = NOW - timedelta(days=365)

        done = self._fresh().completed(NOW, horizon=horizon)

        assert done.backfill_complete
        assert done.backfill_horizon == horizon

    def test_completing_twice_changes_nothing(self) -> None:
        horizon = NOW - timedelta(days=365)
        done = self._fresh().completed(NOW, horizon=horizon)

        assert done.completed(NOW + timedelta(hours=1), horizon=horizon) is done

    def test_a_bounded_backfill_does_not_reach_further_than_it_went(self) -> None:
        done = self._fresh().completed(NOW, horizon=NOW - timedelta(days=365))

        assert done.reaches_back_to(NOW - timedelta(days=365))
        assert done.reaches_back_to(NOW - timedelta(days=100))
        assert not done.reaches_back_to(NOW - timedelta(days=700))
        assert not done.reaches_back_to(None)

    def test_an_unlimited_backfill_reaches_everything(self) -> None:
        done = self._fresh().completed(NOW, horizon=None)

        assert done.reaches_back_to(None)
        assert done.reaches_back_to(NOW - timedelta(days=700))

    def test_reopening_keeps_the_stored_range(self) -> None:
        # Everything above the floor is already stored; re-reading it would be
        # work whose only outcome is recognising what is there.
        done = (
            self._fresh()
            .with_batch(oldest=TelegramMessageId(16), newest=TelegramMessageId(25), now=NOW)
            .completed(NOW, horizon=NOW - timedelta(days=365))
        )

        reopened = done.reopened(NOW, horizon=NOW - timedelta(days=700))

        assert not reopened.backfill_complete
        assert reopened.resume_from == TelegramMessageId(16)

    def test_a_naive_timestamp_is_refused(self) -> None:
        naive = datetime(2026, 7, 1, 12, 0)  # noqa: DTZ001 - the point of the test

        with pytest.raises(DomainValidationError, match="timezone-aware"):
            replace(self._fresh(), last_sync_at=naive)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class _Harness:
    """A backfill environment built entirely from fakes."""

    def __init__(self, *, batch_size: int = 10, horizon_days: int = DEFAULT_HORIZON_DAYS) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A)},
        )
        self.message_store = InMemoryMessageStore(chats={})
        self.cursor_store = InMemorySyncCursorStore(chats={})
        self.clock = AdvanceableClock(NOW)
        self.ids = SequentialIdGenerator(start=1000)
        self.bus = RecordingEventBus()
        self.units: list[InMemoryUnitOfWork] = []
        self.batch_size = batch_size
        self.horizon_days = horizon_days
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
        return InMemoryMessageRepository(self.message_store, account_id)

    def cursors(self, _uow: UnitOfWork, account_id: AccountId) -> InMemorySyncCursorRepository:
        return InMemorySyncCursorRepository(self.cursor_store, account_id)

    @property
    def commits(self) -> int:
        """How many transactions committed."""
        return sum(1 for unit in self.units if unit.is_committed)

    async def setup(self, *, sync_enabled: bool = True) -> tuple[Account, Chat]:
        """Create one account with one syncable private chat."""
        account = Account.create(
            account_id=ACCOUNT_A,
            telegram_user_id=OPERATOR_A,
            display_name="me",
            now=NOW,
            is_active=True,
        )
        await self.accounts_repository.add(account)
        chat = Chat.private_with(
            chat_id=CHAT,
            account_id=ACCOUNT_A,
            telegram_chat_id=TELEGRAM_CHAT,
            contact_id=CONTACT,
            now=NOW,
            sync_enabled=sync_enabled,
        )
        await self.chats(self.unit_of_work(), ACCOUNT_A).add(chat)
        self.message_store.register_chat(CHAT, ACCOUNT_A)
        self.cursor_store.register_chat(CHAT, ACCOUNT_A)
        return account, chat

    def backfill(self) -> SyncHistory:
        return SyncHistory(
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
            self.batch_size,
            self.horizon_days,
        )

    async def stored(self) -> list[TelegramMessageId]:
        """Return the Telegram identifiers stored for the chat, oldest first."""
        page = await self.messages(self.unit_of_work(), ACCOUNT_A).list_by_chat(
            CHAT, PageRequest(limit=500)
        )
        found = [m.telegram_message_id for m in page.items if m.telegram_message_id is not None]
        return sorted(found)

    async def cursor(self) -> SyncCursor | None:
        return await self.cursors(self.unit_of_work(), ACCOUNT_A).get(CHAT)


@pytest.fixture
def harness() -> _Harness:
    """A fresh environment for one test."""
    return _Harness()


@pytest.fixture
async def gateway() -> AsyncIterator[FakeTelegramGateway]:
    """A connected, authorized gateway holding twenty-five messages."""
    fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
    fake.script_history(TELEGRAM_CHAT, *HISTORY)
    await fake.connect()
    try:
        yield fake
    finally:
        await fake.disconnect()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class TestEmptyHistory:
    async def test_a_chat_with_no_messages_completes_immediately(self, harness: _Harness) -> None:
        await harness.setup()
        empty = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        await empty.connect()

        report = await harness.backfill().execute(empty, int(CHAT))
        await empty.disconnect()

        assert report.stored == 0
        assert report.batches == 0
        assert report.stop_reason == BackfillStop.BEGINNING
        assert report.is_complete

    async def test_it_stores_nothing(self, harness: _Harness) -> None:
        await harness.setup()
        empty = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        await empty.connect()

        await harness.backfill().execute(empty, int(CHAT))
        await empty.disconnect()

        assert await harness.stored() == []


class TestASinglePage:
    async def test_a_history_shorter_than_one_batch_is_stored_whole(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        harness.batch_size = 100
        await harness.setup()

        report = await harness.backfill().execute(gateway, int(CHAT))

        assert report.stored == 25
        assert report.batches == 1
        assert len(await harness.stored()) == 25

    async def test_it_then_reaches_the_beginning(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # A short page is not proof of the beginning -- the next fetch returning
        # nothing is, which is why the loop asks again rather than inferring.
        harness.batch_size = 100
        await harness.setup()

        report = await harness.backfill().execute(gateway, int(CHAT))

        assert report.stop_reason == BackfillStop.BEGINNING
        assert report.is_complete


class TestMultiplePages:
    async def test_every_message_is_stored_exactly_once(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()

        report = await harness.backfill().execute(gateway, int(CHAT))

        assert report.stored == 25
        assert await harness.stored() == [TelegramMessageId(n) for n in range(1, 26)]

    async def test_it_takes_one_batch_per_page(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()

        report = await harness.backfill().execute(gateway, int(CHAT))

        # Twenty-five messages at ten per batch: three batches, then an empty
        # page that ends the run.
        assert report.batches == 3

    async def test_one_transaction_per_batch(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Not one per run: ADR-034 permits one transaction at a time for the
        # whole application, so a run-long transaction would be a freeze.
        #
        # Measured on a *second* run, so the bookkeeping transactions -- the one
        # that created the cursor, and the one that will mark the chat complete
        # -- are not in the count. One batch, one commit.
        await harness.setup()
        backfill = harness.backfill()
        await backfill.execute(gateway, int(CHAT), max_batches=1)
        before = harness.commits

        report = await backfill.execute(gateway, int(CHAT), max_batches=1)

        assert report.batches == 1
        assert harness.commits - before == 1

    async def test_the_cursor_ends_at_the_oldest_message(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()

        await harness.backfill().execute(gateway, int(CHAT))

        cursor = await harness.cursor()
        assert cursor is not None
        assert cursor.oldest_synced_message_id == TelegramMessageId(1)
        assert cursor.newest_synced_message_id == TelegramMessageId(25)

    async def test_telegram_is_read_newest_first(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The only direction Telegram's history API supports efficiently, and
        # the only one where an interruption leaves a contiguous stored range.
        await harness.setup()

        report = await harness.backfill().execute(gateway, int(CHAT), max_batches=1)

        cursor = report.cursor
        assert cursor.newest_synced_message_id == TelegramMessageId(25)
        assert cursor.oldest_synced_message_id == TelegramMessageId(16)

    async def test_history_reads_back_newest_first(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # Storage order is insertion order; reading order is sent_at. A backfill
        # inserts old messages after new ones, so the two must differ.
        await harness.setup()
        await harness.backfill().execute(gateway, int(CHAT))

        page = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_by_chat(
            CHAT, PageRequest(limit=5)
        )

        assert [m.telegram_message_id for m in page.items] == [
            TelegramMessageId(n) for n in (25, 24, 23, 22, 21)
        ]


class TestInterruptionAndResume:
    async def test_a_bounded_run_stops_where_it_was_told(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()

        report = await harness.backfill().execute(gateway, int(CHAT), max_batches=1)

        assert report.batches == 1
        assert report.stored == 10
        assert report.stop_reason == BackfillStop.BATCH_LIMIT
        assert not report.is_complete

    async def test_the_next_run_continues_from_there(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        backfill = harness.backfill()
        await backfill.execute(gateway, int(CHAT), max_batches=1)

        second = await backfill.execute(gateway, int(CHAT), max_batches=1)

        assert second.stored == 10
        assert second.skipped == 0
        assert second.cursor.oldest_synced_message_id == TelegramMessageId(6)

    async def test_resuming_never_re_reads_what_it_stored(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # The cursor names a stored message and the fetch is strictly older, so
        # a resume costs no duplicate network traffic either.
        await harness.setup()
        backfill = harness.backfill()
        await backfill.execute(gateway, int(CHAT), max_batches=1)

        second = await backfill.execute(gateway, int(CHAT), max_batches=1)

        assert second.skipped == 0

    async def test_resuming_to_the_end_stores_everything_exactly_once(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        backfill = harness.backfill()

        for _ in range(5):
            report = await backfill.execute(gateway, int(CHAT), max_batches=1)
            if report.is_complete:
                break

        assert await harness.stored() == [TelegramMessageId(n) for n in range(1, 26)]

    async def test_a_run_interrupted_by_telegram_keeps_what_it_committed(
        self, harness: _Harness
    ) -> None:
        # An exception mid-run is not a rollback of the run: the batches that
        # committed are accounted for, and the cursor names exactly them.
        await harness.setup()
        failing = _FailsOnPage(ACCOUNT_A, starts_authorized=True, fail_after=2)
        failing.script_history(TELEGRAM_CHAT, *HISTORY)
        await failing.connect()

        with pytest.raises(TelegramError, match="lost"):
            await harness.backfill().execute(failing, int(CHAT))
        await failing.disconnect()

        cursor = await harness.cursor()
        assert cursor is not None
        assert cursor.oldest_synced_message_id == TelegramMessageId(6)
        assert await harness.stored() == [TelegramMessageId(n) for n in range(6, 26)]

    async def test_and_the_next_run_finishes_it(self, harness: _Harness) -> None:
        await harness.setup()
        failing = _FailsOnPage(ACCOUNT_A, starts_authorized=True, fail_after=2)
        failing.script_history(TELEGRAM_CHAT, *HISTORY)
        await failing.connect()
        with pytest.raises(TelegramError):
            await harness.backfill().execute(failing, int(CHAT))
        await failing.disconnect()

        healthy = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        healthy.script_history(TELEGRAM_CHAT, *HISTORY)
        await healthy.connect()
        report = await harness.backfill().execute(healthy, int(CHAT))
        await healthy.disconnect()

        assert report.stored == 5
        assert report.is_complete
        assert await harness.stored() == [TelegramMessageId(n) for n in range(1, 26)]


class TestIdempotency:
    async def test_a_second_complete_run_does_nothing(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        backfill = harness.backfill()
        await backfill.execute(gateway, int(CHAT))

        second = await backfill.execute(gateway, int(CHAT))

        assert second.stop_reason == BackfillStop.ALREADY_COMPLETE
        assert second.stored == 0
        assert second.batches == 0

    async def test_a_reset_re_reads_and_stores_nothing_new(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # What makes a reset cheap in everything but network: every message is
        # recognised by its Telegram identifier and skipped (ADR-045).
        await harness.setup()
        backfill = harness.backfill()
        await backfill.execute(gateway, int(CHAT))

        report = await backfill.execute(gateway, int(CHAT), reset=True)

        assert report.stored == 0
        assert report.skipped == 25
        assert len(await harness.stored()) == 25

    async def test_a_reset_ends_complete_again(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        backfill = harness.backfill()
        await backfill.execute(gateway, int(CHAT))

        report = await backfill.execute(gateway, int(CHAT), reset=True)

        assert report.is_complete
        assert report.cursor.oldest_synced_message_id == TelegramMessageId(1)

    async def test_telegram_repeating_a_message_stores_it_once(self, harness: _Harness) -> None:
        # Telegram returning the same identifier twice in one page is not
        # something it does; the index that makes it harmless is, and this is
        # what proves the index is what the ingestion relies on.
        await harness.setup()
        duplicating = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        duplicating.script_history(TELEGRAM_CHAT, message(1), message(2), message(2), message(3))
        await duplicating.connect()

        report = await harness.backfill().execute(duplicating, int(CHAT))
        await duplicating.disconnect()

        assert report.stored == 3
        assert await harness.stored() == [TelegramMessageId(n) for n in (1, 2, 3)]


class TestTheHorizon:
    async def test_it_stops_at_the_configured_depth(self, harness: _Harness) -> None:
        harness.horizon_days = 10
        await harness.setup()
        old = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        old.script_history(
            TELEGRAM_CHAT,
            *(message(n, age_days=100) for n in range(1, 11)),
            *(message(n) for n in range(11, 21)),
        )
        await old.connect()

        report = await harness.backfill().execute(old, int(CHAT))
        await old.disconnect()

        assert report.stop_reason == BackfillStop.HORIZON
        assert report.is_complete
        assert await harness.stored() == [TelegramMessageId(n) for n in range(11, 21)]

    async def test_the_horizon_it_reached_is_recorded(self, harness: _Harness) -> None:
        harness.horizon_days = 10
        await harness.setup()
        old = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        old.script_history(
            TELEGRAM_CHAT, *(message(n, age_days=100) for n in range(1, 11)), message(11)
        )
        await old.connect()

        report = await harness.backfill().execute(old, int(CHAT))
        await old.disconnect()

        assert report.cursor.backfill_horizon == NOW - timedelta(days=10)

    async def test_a_deeper_horizon_reopens_a_completed_backfill(self, harness: _Harness) -> None:
        harness.horizon_days = 10
        await harness.setup()
        old = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        old.script_history(
            TELEGRAM_CHAT,
            *(message(n, age_days=100) for n in range(1, 11)),
            *(message(n) for n in range(11, 21)),
        )
        await old.connect()
        await harness.backfill().execute(old, int(CHAT))

        harness.horizon_days = 500
        report = await harness.backfill().execute(old, int(CHAT))
        await old.disconnect()

        assert report.stored == 10
        assert await harness.stored() == [TelegramMessageId(n) for n in range(1, 21)]

    async def test_a_shallower_horizon_deletes_nothing(self, harness: _Harness) -> None:
        harness.horizon_days = 0
        await harness.setup()
        old = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        old.script_history(TELEGRAM_CHAT, *(message(n, age_days=100) for n in range(1, 11)))
        await old.connect()
        await harness.backfill().execute(old, int(CHAT))

        harness.horizon_days = 10
        report = await harness.backfill().execute(old, int(CHAT))
        await old.disconnect()

        assert report.stop_reason == BackfillStop.ALREADY_COMPLETE
        assert len(await harness.stored()) == 10

    async def test_zero_days_means_no_limit(self, harness: _Harness) -> None:
        harness.horizon_days = 0
        await harness.setup()
        ancient = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        ancient.script_history(TELEGRAM_CHAT, *(message(n, age_days=5000) for n in range(1, 11)))
        await ancient.connect()

        report = await harness.backfill().execute(ancient, int(CHAT))
        await ancient.disconnect()

        assert report.stored == 10
        assert report.stop_reason == BackfillStop.BEGINNING


class TestDeterministicProgress:
    async def test_a_gateway_that_never_advances_is_stopped(self, harness: _Harness) -> None:
        # Unreachable against a gateway that honours before_message_id. The
        # guard is what stops the loop spinning against one that does not.
        await harness.setup()
        stuck = _AlwaysTheSamePage(ACCOUNT_A, starts_authorized=True)
        await stuck.connect()

        report = await harness.backfill().execute(stuck, int(CHAT))
        await stuck.disconnect()

        # Two batches, not one: the first moves the bookmark off "nothing
        # stored", and the second is the one that finds it did not move.
        assert report.stop_reason == BackfillStop.NO_PROGRESS
        assert report.batches == 2


class TestRefusals:
    async def test_a_chat_with_synchronisation_off_is_refused(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        # sync_enabled is the operator's decision (ADR-053), and a backfill that
        # ignored it would ingest a conversation somebody had excluded.
        await harness.setup(sync_enabled=False)

        with pytest.raises(DomainValidationError, match="switched off"):
            await harness.backfill().execute(gateway, int(CHAT))

    async def test_an_unknown_chat_is_reported(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()

        with pytest.raises(RecordNotFoundError, match="No chat"):
            await harness.backfill().execute(gateway, 987)

    async def test_a_gateway_for_another_account_is_refused(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()
        other = Account.create(
            account_id=ACCOUNT_B, telegram_user_id=OPERATOR_B, display_name="them", now=NOW
        )
        await harness.accounts_repository.add(other)

        with pytest.raises(AuthorizationError, match="bound to account"):
            await harness.backfill().execute(gateway, int(CHAT), ACCOUNT_B)

    async def test_zero_batches_is_a_contradiction(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()

        with pytest.raises(DomainValidationError, match="at least one batch"):
            await harness.backfill().execute(gateway, int(CHAT), max_batches=0)

    async def test_an_unauthorized_gateway_is_refused(self, harness: _Harness) -> None:
        await harness.setup()
        signed_out = FakeTelegramGateway(ACCOUNT_A)
        await signed_out.connect()

        with pytest.raises(AuthorizationError):
            await harness.backfill().execute(signed_out, int(CHAT))

        await signed_out.disconnect()


class TestSenderAttribution:
    async def test_an_outgoing_message_is_the_operator(self, harness: _Harness) -> None:
        # Read from Telegram rather than inferred: is_outgoing is stated
        # directly, which is what makes a message sent from another device right.
        await harness.setup()
        mixed = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        mixed.script_history(TELEGRAM_CHAT, message(1), message(2, outgoing=True))
        await mixed.connect()

        await harness.backfill().execute(mixed, int(CHAT))
        await mixed.disconnect()

        page = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_by_chat(
            CHAT, PageRequest(limit=10)
        )
        kinds = {int(m.telegram_message_id or 0): m.sender_kind for m in page.items}
        assert kinds[1] is SenderKind.CONTACT
        assert kinds[2] is SenderKind.OPERATOR

    async def test_a_service_message_belongs_to_nobody(self, harness: _Harness) -> None:
        await harness.setup()
        service = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        service.script_history(
            TELEGRAM_CHAT,
            replace(message(1), sender_id=None, message_type=MessageType.SERVICE, text=None),
        )
        await service.connect()

        await harness.backfill().execute(service, int(CHAT))
        await service.disconnect()

        page = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_by_chat(
            CHAT, PageRequest(limit=10)
        )
        assert page.items[0].sender_kind is SenderKind.SYSTEM


class TestEveryChatAtOnce:
    async def test_it_synchronises_each_syncable_chat(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup()

        reports = await harness.backfill().execute_all(gateway)

        assert len(reports) == 1
        assert reports[0].chat_id == CHAT

    async def test_it_skips_a_chat_with_synchronisation_off(
        self, harness: _Harness, gateway: FakeTelegramGateway
    ) -> None:
        await harness.setup(sync_enabled=False)

        assert await harness.backfill().execute_all(gateway) == ()


# ---------------------------------------------------------------------------
# Gateways that misbehave
# ---------------------------------------------------------------------------


class _FailsOnPage(FakeTelegramGateway):
    """A gateway that loses Telegram part-way through a backfill."""

    __slots__ = ("_fail_after", "_pages")

    def __init__(self, account_id: AccountId, *, starts_authorized: bool, fail_after: int) -> None:
        """Answer ``fail_after`` pages, then fail."""
        super().__init__(account_id, starts_authorized=starts_authorized)
        self._fail_after = fail_after
        self._pages = 0

    async def fetch_history(
        self,
        chat_id: TelegramChatId,
        *,
        before_message_id: TelegramMessageId | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        """Answer, until the page after which Telegram goes away."""
        if self._pages >= self._fail_after:
            msg = "Connection lost"
            raise TelegramError(msg, user_message="Telegram could not be reached.")
        self._pages += 1
        return await super().fetch_history(
            chat_id, before_message_id=before_message_id, limit=limit
        )


class _AlwaysTheSamePage(FakeTelegramGateway):
    """A gateway that ignores the cursor and answers with the same page forever."""

    __slots__ = ()

    async def fetch_history(
        self,
        chat_id: TelegramChatId,
        *,
        before_message_id: TelegramMessageId | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        """Return the same three messages whatever was asked for."""
        del chat_id, before_message_id, limit
        page = (message(3), message(2), message(1))
        return HistoryPage(messages=page, oldest_message_id=TelegramMessageId(1))


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


class _FailsBeforeCommit(SyncCursorRepository):
    """A cursor repository whose ``save`` raises, exactly where the crash matters.

    ``_commit_batch`` ingests, then saves the cursor, then commits. Raising in
    the save is the last instant at which a failure can still take the messages
    with it -- which is what the transaction boundary is claimed to guarantee.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: SyncCursorRepository) -> None:
        """Wrap the repository that would have worked."""
        self._inner = inner

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._inner.account_id

    async def get(self, chat_id: ChatId) -> SyncCursor | None:
        """Read through, so the run can start normally."""
        return await self._inner.get(chat_id)

    async def add(self, cursor: SyncCursor) -> None:
        """Write through, so the run can start normally."""
        await self._inner.add(cursor)

    async def update(self, cursor: SyncCursor) -> None:
        """Fail, as a process dying between the ingest and the commit does."""
        del cursor
        msg = "the process died here"
        raise RuntimeError(msg)

    async def save(self, cursor: SyncCursor) -> None:
        """Fail once the cursor already exists, which is after the first ingest."""
        if await self._inner.get(cursor.chat_id) is None:
            await self._inner.add(cursor)
            return
        msg = "the process died here"
        raise RuntimeError(msg)


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
    """A container over a real SQLite file.

    Closed with ``aclose``: the database owns a worker thread and a connection,
    and the synchronous close cannot shut either down.
    """
    try:
        yield container
    finally:
        await container.aclose()


async def _telegram_ids(container: Container, chat: Chat) -> list[int]:
    """Return the Telegram identifiers stored for a chat, oldest first."""
    page = await container.read_chat_history().execute(int(chat.id), PageRequest(limit=500))
    return sorted(int(m.telegram_message_id or 0) for m in page.items)


class TestAgainstARealDatabase:
    """Crash safety, which no in-memory fake can demonstrate.

    The in-memory repositories write through immediately, so a rollback is
    invisible to them -- and rollback is the whole of what "the cursor never
    advances past what was persisted" means.
    """

    async def test_a_backfill_stores_everything(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()

        report = await stored.sync_history().execute(gateway, int(chat.id))
        await gateway.disconnect()

        assert report.stored == 25
        assert await _telegram_ids(stored, chat) == list(range(1, 26))

    async def test_the_cursor_is_written_beside_the_messages(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()

        report = await stored.sync_history().execute(gateway, int(chat.id))
        await gateway.disconnect()

        async with stored.unit_of_work() as uow:
            cursor = await stored.sync_cursors(uow, account.id).get(chat.id)
        assert cursor is not None
        assert cursor == report.cursor
        assert cursor.oldest_synced_message_id == TelegramMessageId(1)

    async def test_an_exception_before_commit_persists_nothing(self, stored: Container) -> None:
        # The test this slice exists for. The ingest has already written its
        # rows into the open transaction when the failure lands.
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()

        backfill = SyncHistory(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(stored.sync_cursors(uow, account_id)),
            stored.chats,
            stored.accounts,
            stored.ingest_messages(),
            stored.clock,
            stored.events,
            DEFAULT_BATCH_SIZE,
            DEFAULT_HORIZON_DAYS,
        )

        with pytest.raises(RuntimeError, match="died here"):
            await backfill.execute(gateway, int(chat.id))
        await gateway.disconnect()

        assert await _telegram_ids(stored, chat) == []

    async def test_and_the_cursor_did_not_move(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()

        backfill = SyncHistory(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(stored.sync_cursors(uow, account_id)),
            stored.chats,
            stored.accounts,
            stored.ingest_messages(),
            stored.clock,
            stored.events,
            DEFAULT_BATCH_SIZE,
            DEFAULT_HORIZON_DAYS,
        )
        with pytest.raises(RuntimeError):
            await backfill.execute(gateway, int(chat.id))
        await gateway.disconnect()

        async with stored.unit_of_work() as uow:
            cursor = await stored.sync_cursors(uow, account.id).get(chat.id)
        assert cursor is not None
        assert cursor.oldest_synced_message_id is None
        assert not cursor.backfill_complete

    async def test_the_run_that_follows_a_crash_starts_from_the_newest(
        self, stored: Container
    ) -> None:
        # The proof that the failed run left nothing to resume past: a healthy
        # run afterwards stores the whole history rather than a suffix of it.
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()
        crashing = SyncHistory(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(stored.sync_cursors(uow, account_id)),
            stored.chats,
            stored.accounts,
            stored.ingest_messages(),
            stored.clock,
            stored.events,
            DEFAULT_BATCH_SIZE,
            DEFAULT_HORIZON_DAYS,
        )
        with pytest.raises(RuntimeError):
            await crashing.execute(gateway, int(chat.id))

        report = await stored.sync_history().execute(gateway, int(chat.id))
        await gateway.disconnect()

        assert report.stored == 25
        assert await _telegram_ids(stored, chat) == list(range(1, 26))

    async def test_an_interrupted_run_resumes_exactly(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()
        backfill = SyncHistory(
            stored.unit_of_work,
            stored.sync_cursors,
            stored.chats,
            stored.accounts,
            stored.ingest_messages(),
            stored.clock,
            stored.events,
            10,
            DEFAULT_HORIZON_DAYS,
        )

        first = await backfill.execute(gateway, int(chat.id), max_batches=1)
        second = await backfill.execute(gateway, int(chat.id))
        await gateway.disconnect()

        assert first.stored == 10
        assert second.stored == 15
        assert second.skipped == 0
        assert await _telegram_ids(stored, chat) == list(range(1, 26))

    async def test_a_rerun_writes_nothing(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()
        await stored.sync_history().execute(gateway, int(chat.id))

        report = await stored.sync_history().execute(gateway, int(chat.id), reset=True)
        await gateway.disconnect()

        assert report.stored == 0
        assert report.skipped == 25
        assert await _telegram_ids(stored, chat) == list(range(1, 26))

    async def test_soft_deleting_the_contact_keeps_the_bookmark(self, stored: Container) -> None:
        # Soft deletion hides a person; it does not throw away the history the
        # operator may still restore. The cursor is part of that history, and
        # losing it would make a restore re-read everything.
        account, chat = await _prepare(stored)
        gateway = FakeTelegramGateway(account.id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        await gateway.connect()
        await stored.sync_history().execute(gateway, int(chat.id))
        await gateway.disconnect()

        contact = (await stored.list_contacts().execute(PageRequest(limit=1))).items[0]
        await stored.change_contact_status().execute(int(contact.id), ContactTransition.DELETE)

        async with stored.unit_of_work() as uow:
            assert await stored.sync_cursors(uow, account.id).get(chat.id) is not None


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


@pytest.fixture(params=["fake", "tdlib"])
async def either(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[TelegramGateway]:
    """An authorized gateway of each implementation, holding the same history."""
    gateway: TelegramGateway
    if request.param == "fake":
        fake = FakeTelegramGateway(ACCOUNT_A, starts_authorized=True)
        fake.script_history(TELEGRAM_CHAT, *HISTORY)
        gateway = fake
    else:
        library = AuthorizingTdjson(starts_authorized=True)
        library.script_history(TELEGRAM_CHAT, *HISTORY)
        gateway = TdlibGateway(
            ACCOUNT_A,
            TdjsonClient(library),
            _settings(tmp_path),
            state_timeout=TIMEOUT,
            startup_timeout=TIMEOUT,
        )
    await gateway.connect()
    try:
        yield gateway
    finally:
        await gateway.disconnect()


class TestBothImplementationsBackfillIdentically:
    """The fake is the second implementation, so a backfill must not tell them apart."""

    async def test_a_whole_backfill_stores_the_same_messages(
        self, harness: _Harness, either: TelegramGateway
    ) -> None:
        await harness.setup()

        report = await harness.backfill().execute(either, int(CHAT))

        assert report.stored == 25
        assert report.stop_reason == BackfillStop.BEGINNING
        assert await harness.stored() == [TelegramMessageId(n) for n in range(1, 26)]

    async def test_it_takes_the_same_batches(
        self, harness: _Harness, either: TelegramGateway
    ) -> None:
        await harness.setup()

        report = await harness.backfill().execute(either, int(CHAT))

        assert report.batches == 3

    async def test_it_resumes_the_same_way(
        self, harness: _Harness, either: TelegramGateway
    ) -> None:
        await harness.setup()
        backfill = harness.backfill()

        first = await backfill.execute(either, int(CHAT), max_batches=1)
        second = await backfill.execute(either, int(CHAT))

        assert first.cursor.oldest_synced_message_id == TelegramMessageId(16)
        assert second.stored == 15
        assert second.skipped == 0

    async def test_a_rerun_stores_nothing_new(
        self, harness: _Harness, either: TelegramGateway
    ) -> None:
        await harness.setup()
        backfill = harness.backfill()
        await backfill.execute(either, int(CHAT))

        report = await backfill.execute(either, int(CHAT), reset=True)

        assert report.stored == 0
        assert report.skipped == 25


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
    monkeypatch.setenv("TGASSIST_TELEGRAM__BACKFILL_BATCH_SIZE", "10")

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
    """Create an active account, a contact and the private chat with them.

    Identifiers are read back from the output rather than assumed: they are
    UUID-v7 derived, so a test that hard-coded one would be testing the
    generator.
    """
    _run("account", "create", str(int(OPERATOR_A)), "Primary")
    added = _run("contact", "add", str(int(COUNTERPART)), "Ada")
    contact_id = added.split("Added contact ")[1].split(":")[0]
    _run("chat", "open", str(int(TELEGRAM_CHAT)), "--contact", contact_id)


@pytest.fixture
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the container's gateway with one holding twenty-five messages."""

    @asynccontextmanager
    async def fake_gateway(
        self: Container, account_id: AccountId
    ) -> AsyncIterator[FakeTelegramGateway]:
        del self
        gateway = FakeTelegramGateway(account_id, starts_authorized=True)
        gateway.script_history(TELEGRAM_CHAT, *HISTORY)
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    monkeypatch.setattr(Container, "telegram_for", fake_gateway)


def _chat_id() -> str:
    """Return the local chat identifier the CLI issued."""
    listing = _run("chat", "list")
    rows = [line for line in listing.splitlines() if "private" in line]
    assert rows, listing
    return rows[0].split()[0]


def _stored_count() -> int:
    """Return how many messages the database holds for the chat."""
    history = _run("message", "history", _chat_id(), "--limit", "100")
    return sum(1 for line in history.splitlines() if "message " in line)


@pytest.mark.usefixtures("cli_env", "_chat", "_gateway")
class TestSyncHistoryCommand:
    """The flow the goal describes: run, interrupt, resume, and nothing lost."""

    def test_a_whole_backfill_reports_what_it_stored(self) -> None:
        result = runner.invoke(app, ["sync", "history", _chat_id()])

        assert result.exit_code == 0, result.output
        assert "25 new" in result.output
        assert "reached the beginning" in result.output

    def test_an_interrupted_run_says_there_is_more(self) -> None:
        result = runner.invoke(app, ["sync", "history", _chat_id(), "--max-batches", "1"])

        assert "10 new" in result.output
        assert "stopped at the batch limit" in result.output
        assert "Run this again to continue" in result.output

    def test_resuming_continues_from_where_it_stopped(self) -> None:
        chat = _chat_id()
        runner.invoke(app, ["sync", "history", chat, "--max-batches", "1"])

        result = runner.invoke(app, ["sync", "history", chat, "--resume"])

        assert result.exit_code == 0, result.output
        assert "15 new, 0 already stored" in result.output

    def test_the_final_history_is_complete_and_has_no_duplicates(self) -> None:
        chat = _chat_id()
        runner.invoke(app, ["sync", "history", chat, "--max-batches", "1"])
        runner.invoke(app, ["sync", "history", chat, "--resume"])

        assert _stored_count() == 25

    def test_running_it_again_does_nothing(self) -> None:
        chat = _chat_id()
        runner.invoke(app, ["sync", "history", chat])

        result = runner.invoke(app, ["sync", "history", chat])

        assert "already complete" in result.output

    def test_reset_re_reads_and_stores_nothing_new(self) -> None:
        chat = _chat_id()
        runner.invoke(app, ["sync", "history", chat])

        result = runner.invoke(app, ["sync", "history", chat, "--reset"])

        assert "0 new, 25 already stored" in result.output
        assert _stored_count() == 25

    def test_resume_and_reset_together_are_refused(self) -> None:
        result = runner.invoke(app, ["sync", "history", _chat_id(), "--resume", "--reset"])

        assert result.exit_code != 0
        assert "opposite things" in result.output

    def test_with_no_chat_it_syncs_every_syncable_one(self) -> None:
        result = runner.invoke(app, ["sync", "history"])

        assert result.exit_code == 0, result.output
        assert "25 message(s) stored across 1 chat(s)" in result.output

    def test_an_unknown_chat_is_reported(self) -> None:
        result = runner.invoke(app, ["sync", "history", "9999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()
