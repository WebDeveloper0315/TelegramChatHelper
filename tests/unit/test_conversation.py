"""Conversation segmentation.

Four layers, because the guarantees live at different depths:

* the **rule**, a pure function, checked as one -- given the same messages it
  gives the same boundaries, and nothing else can affect it;
* the **entity**, whose invariants say what a conversation can be;
* the **pass** against fakes, which is where identity, incrementality and the
  transaction boundary live;
* the pass against a **real SQLite database** and the command line, which is
  where a rollback is observable at all.

No test here needs a Telegram account, a network or a real native library, and
none of them needs an AI: segmentation reads timestamps and counts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock, InMemorySecretStore, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.chat_repository import InMemoryChatRepository, InMemoryChatStore
from tests.fakes.conversation_repository import (
    InMemoryConversationRepository,
    InMemoryConversationStore,
)
from tests.fakes.message_repository import InMemoryMessageRepository, InMemoryMessageStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.conversation import (
    GetConversation,
    ListConversations,
    SegmentConversations,
)
from tgassist.application.use_cases.message import IncomingMessage
from tgassist.domain.errors import ConstraintViolationError, DomainValidationError
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    ConversationId,
    MessageId,
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.message import Message, MessageType, SenderKind
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.conversation_repository import ConversationRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.domain.services.segmentation import (
    DEFAULT_GAP_MINUTES,
    DEFAULT_MAX_MESSAGES,
    Segment,
    SegmentationRules,
    in_order,
    segment,
)
from tgassist.presentation.cli.app import app

runner = CliRunner()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
START = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CHAT = ChatId(11)
OTHER_CHAT = ChatId(12)
CONTACT = ContactId(101)
OTHER_CONTACT = ContactId(102)
TELEGRAM_CHAT = TelegramChatId(5000)
COUNTERPART = TelegramUserId(2002)

GAP = timedelta(minutes=DEFAULT_GAP_MINUTES)


def message(
    number: int,
    *,
    at: datetime | None = None,
    chat: ChatId = CHAT,
    account: AccountId = ACCOUNT_A,
    telegram_id: int | None = None,
) -> Message:
    """Build a stored message. ``at`` defaults to a minute per number."""
    return Message.record(
        message_id=MessageId(number),
        account_id=account,
        chat_id=chat,
        sender_kind=SenderKind.CONTACT,
        sent_at=at if at is not None else START + timedelta(minutes=number),
        ingested_at=NOW,
        text=f"message {number}",
        telegram_message_id=(
            TelegramMessageId(telegram_id) if telegram_id is not None else TelegramMessageId(number)
        ),
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestSegmentationRules:
    def test_a_gap_of_exactly_the_threshold_continues_the_conversation(self) -> None:
        # The choice matters only at the boundary, and it is made in one place
        # so both directions of the test read the same rule.
        rules = SegmentationRules(gap=GAP)

        assert not rules.separates(START, START + GAP)

    def test_a_gap_longer_than_the_threshold_separates(self) -> None:
        rules = SegmentationRules(gap=GAP)

        assert rules.separates(START, START + GAP + timedelta(seconds=1))

    def test_a_gap_of_nothing_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="must be positive"):
            SegmentationRules(gap=timedelta(0))

    def test_a_conversation_of_no_messages_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="at least one message"):
            SegmentationRules(max_messages=0)

    def test_the_defaults_are_the_documented_ones(self) -> None:
        rules = SegmentationRules()

        assert rules.gap == timedelta(minutes=DEFAULT_GAP_MINUTES)
        assert rules.max_messages == DEFAULT_MAX_MESSAGES


class TestSegmenting:
    def _rules(self, minutes: int = 60, cap: int = 200) -> SegmentationRules:
        return SegmentationRules(gap=timedelta(minutes=minutes), max_messages=cap)

    def test_no_messages_produce_no_segments(self) -> None:
        assert segment([], self._rules()) == ()

    def test_one_message_is_one_segment(self) -> None:
        (only,) = segment([message(1)], self._rules())

        assert only.message_count == 1
        assert only.started_at == only.ended_at

    def test_messages_within_the_gap_are_one_segment(self) -> None:
        found = segment([message(n) for n in range(1, 6)], self._rules())

        assert len(found) == 1
        assert found[0].message_count == 5

    def test_a_silence_begins_a_new_segment(self) -> None:
        late = message(9, at=START + timedelta(minutes=5) + timedelta(hours=2))

        found = segment([message(1), message(2), late], self._rules())

        assert [piece.message_count for piece in found] == [2, 1]

    def test_the_message_cap_begins_a_new_segment(self) -> None:
        # Not a semantic boundary -- a bound on how much context a later feature
        # can be asked to hold.
        found = segment([message(n) for n in range(1, 8)], self._rules(cap=3))

        assert [piece.message_count for piece in found] == [3, 3, 1]

    def test_every_message_lands_in_exactly_one_segment(self) -> None:
        messages = [message(n) for n in range(1, 20)]

        found = segment(messages, self._rules(minutes=2))

        placed = [m for piece in found for m in piece.messages]
        assert len(placed) == len(messages)
        assert {int(m.id) for m in placed} == {int(m.id) for m in messages}

    def test_segments_are_contiguous_and_ordered(self) -> None:
        found = segment([message(n) for n in range(1, 20)], self._rules(minutes=2))

        for earlier, later in pairwise(found):
            assert earlier.ended_at < later.started_at

    def test_the_order_messages_arrive_in_does_not_matter(self) -> None:
        # The property the whole slice rests on. A backfill hands them over
        # newest first; a rebuild reads them oldest first.
        messages = [message(n) for n in range(1, 12)]
        rules = self._rules(minutes=2)

        forwards = segment(messages, rules)
        backwards = segment(list(reversed(messages)), rules)

        assert [p.messages for p in forwards] == [p.messages for p in backwards]

    def test_two_messages_at_one_instant_are_ordered_by_telegram(self) -> None:
        # sent_at alone is not a total order, and the tiebreak must not be
        # insertion order: a backfill stores an older message later.
        same = START + timedelta(minutes=1)
        second = message(1, at=same, telegram_id=90)
        first = message(2, at=same, telegram_id=10)

        assert [int(m.id) for m in in_order([second, first])] == [2, 1]

    def test_a_message_with_no_telegram_identifier_still_orders(self) -> None:
        same = START + timedelta(minutes=1)
        typed = Message.record(
            message_id=MessageId(7),
            account_id=ACCOUNT_A,
            chat_id=CHAT,
            sender_kind=SenderKind.OPERATOR,
            sent_at=same,
            ingested_at=NOW,
            text="typed",
        )

        ordered = in_order([message(1, at=same, telegram_id=5), typed])

        assert [int(m.id) for m in ordered] == [7, 1]

    def test_an_empty_segment_is_unrepresentable(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot be empty"):
            Segment(messages=())


# ---------------------------------------------------------------------------
# The entity
# ---------------------------------------------------------------------------


def conversation(**overrides: object) -> Conversation:
    """Build a conversation, with optional field overrides."""
    values: dict[str, object] = {
        "conversation_id": ConversationId(1),
        "account_id": ACCOUNT_A,
        "chat_id": CHAT,
        "started_at": START,
        "ended_at": START + timedelta(minutes=30),
        "message_count": 5,
        "now": NOW,
    }
    values.update(overrides)
    return Conversation.spanning(**values)  # type: ignore[arg-type]


class TestConversation:
    def test_it_knows_how_long_it_lasted(self) -> None:
        assert conversation().duration == timedelta(minutes=30)

    def test_it_cannot_end_before_it_began(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot end before"):
            conversation(ended_at=START - timedelta(minutes=1))

    def test_it_cannot_be_empty(self) -> None:
        # An empty conversation is a row that should have been deleted, and
        # permitting it would let a segmentation bug survive as data.
        with pytest.raises(DomainValidationError, match="at least one message"):
            conversation(message_count=0)

    def test_a_single_message_conversation_is_a_point(self) -> None:
        one = conversation(ended_at=START, message_count=1)

        assert one.is_single_message
        assert one.duration == timedelta(0)

    def test_it_contains_an_instant_inside_its_span(self) -> None:
        episode = conversation()

        assert episode.contains(START)
        assert episode.contains(START + timedelta(minutes=15))
        assert episode.contains(START + timedelta(minutes=30))

    def test_it_does_not_contain_an_instant_outside(self) -> None:
        episode = conversation()

        assert not episode.contains(START - timedelta(seconds=1))
        assert not episode.contains(START + timedelta(minutes=31))

    def test_openness_is_asked_of_an_instant_not_stored(self) -> None:
        # A flag would be true when written and wrong an hour later, with no job
        # to correct it.
        episode = conversation(ended_at=NOW - timedelta(hours=1))

        assert episode.is_open_at(NOW, timedelta(hours=6))
        assert not episode.is_open_at(NOW, timedelta(minutes=30))

    def test_respanning_to_the_same_extent_changes_nothing(self) -> None:
        episode = conversation()

        assert (
            episode.spanning_now(
                started_at=episode.started_at,
                ended_at=episode.ended_at,
                message_count=episode.message_count,
                now=NOW + timedelta(days=1),
            )
            is episode
        )

    def test_respanning_moves_the_update_time(self) -> None:
        episode = conversation()
        later = NOW + timedelta(days=1)

        revised = episode.spanning_now(
            started_at=episode.started_at,
            ended_at=episode.ended_at + timedelta(minutes=5),
            message_count=6,
            now=later,
        )

        assert revised.updated_at == later
        assert revised.created_at == episode.created_at

    def test_a_naive_timestamp_is_refused(self) -> None:
        naive = datetime(2026, 6, 1, 9, 0)  # noqa: DTZ001 - the point of the test

        with pytest.raises(DomainValidationError, match="timezone-aware"):
            conversation(started_at=naive)


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class _Harness:
    """A segmentation environment built entirely from fakes."""

    def __init__(self, *, gap_minutes: int = 60, cap: int = 200) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(OTHER_CONTACT): int(ACCOUNT_A)},
        )
        self.message_store = InMemoryMessageStore(chats={})
        self.conversation_store = InMemoryConversationStore(chats={})
        self.clock = AdvanceableClock(NOW)
        self.ids = SequentialIdGenerator(start=1000)
        self.rules = SegmentationRules(gap=timedelta(minutes=gap_minutes), max_messages=cap)
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
        return InMemoryMessageRepository(self.message_store, account_id)

    def conversations(
        self, _uow: UnitOfWork, account_id: AccountId
    ) -> InMemoryConversationRepository:
        return InMemoryConversationRepository(self.conversation_store, account_id)

    @property
    def commits(self) -> int:
        return sum(1 for unit in self.units if unit.is_committed)

    async def setup(self, *, second_chat: bool = False) -> Account:
        account = Account.create(
            account_id=ACCOUNT_A,
            telegram_user_id=TelegramUserId(1001),
            display_name="me",
            now=NOW,
            is_active=True,
        )
        await self.accounts_repository.add(account)
        await self._add_chat(CHAT, CONTACT, TELEGRAM_CHAT)
        if second_chat:
            await self._add_chat(OTHER_CHAT, OTHER_CONTACT, TelegramChatId(6000))
        return account

    async def _add_chat(
        self, chat_id: ChatId, contact_id: ContactId, telegram_chat_id: TelegramChatId
    ) -> None:
        await self.chats(self.unit_of_work(), ACCOUNT_A).add(
            Chat.private_with(
                chat_id=chat_id,
                account_id=ACCOUNT_A,
                telegram_chat_id=telegram_chat_id,
                contact_id=contact_id,
                now=NOW,
            )
        )
        self.message_store.register_chat(chat_id, ACCOUNT_A)
        self.conversation_store.register_chat(chat_id, ACCOUNT_A)

    async def store(self, *messages: Message) -> None:
        """Put messages in the store, as ingestion would."""
        repository = self.messages(self.unit_of_work(), ACCOUNT_A)
        for item in messages:
            await repository.add(item)

    def segmenter(self) -> SegmentConversations:
        return SegmentConversations(
            self.unit_of_work,
            self.conversations,
            self.messages,
            self.accounts,
            self.clock,
            self.ids,
            self.rules,
        )

    async def stored(self, chat_id: ChatId = CHAT) -> tuple[Conversation, ...]:
        """Return a chat's conversations, oldest first."""
        return await self.conversations(self.unit_of_work(), ACCOUNT_A).list_from(chat_id)

    async def spans(self, chat_id: ChatId = CHAT) -> list[tuple[datetime, datetime, int]]:
        """Return each conversation's extent, oldest first."""
        return [(c.started_at, c.ended_at, c.message_count) for c in await self.stored(chat_id)]

    async def identities(self, chat_id: ChatId = CHAT) -> list[int]:
        """Return each conversation's identifier, oldest first."""
        return [int(c.id) for c in await self.stored(chat_id)]


@pytest.fixture
async def harness() -> _Harness:
    """One active account with one chat."""
    built = _Harness()
    await built.setup()
    return built


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


class TestFirstPass:
    async def test_an_empty_chat_produces_nothing(self, harness: _Harness) -> None:
        report = await harness.segmenter().execute(int(CHAT))

        assert report.messages == 0
        assert report.conversations == 0
        assert await harness.stored() == ()

    async def test_an_empty_chat_commits_nothing(self, harness: _Harness) -> None:
        before = harness.commits

        await harness.segmenter().execute(int(CHAT))

        assert harness.commits == before

    async def test_one_message_becomes_one_conversation(self, harness: _Harness) -> None:
        await harness.store(message(1))

        report = await harness.segmenter().execute(int(CHAT))

        (only,) = await harness.stored()
        assert report.created == 1
        assert only.message_count == 1
        assert only.started_at == only.ended_at == message(1).sent_at

    async def test_messages_within_the_gap_become_one_conversation(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in range(1, 6)))

        await harness.segmenter().execute(int(CHAT))

        (only,) = await harness.stored()
        assert only.message_count == 5
        assert only.started_at == message(1).sent_at
        assert only.ended_at == message(5).sent_at

    async def test_a_silence_produces_two_conversations(self, harness: _Harness) -> None:
        await harness.store(message(1), message(2), message(9, at=START + timedelta(hours=4)))

        report = await harness.segmenter().execute(int(CHAT))

        assert report.created == 2
        assert [count for _s, _e, count in await harness.spans()] == [2, 1]

    async def test_one_pass_is_one_transaction(self, harness: _Harness) -> None:
        # Everything the pass writes commits together or not at all.
        await harness.store(*(message(n) for n in range(1, 20)))
        harness.rules = SegmentationRules(gap=timedelta(minutes=2))
        before = harness.commits

        await harness.segmenter().execute(int(CHAT))

        assert harness.commits - before == 1

    async def test_another_chat_is_untouched(self, harness: _Harness) -> None:
        await harness._add_chat(OTHER_CHAT, OTHER_CONTACT, TelegramChatId(6000))
        await harness.store(message(1), message(2, chat=OTHER_CHAT))

        await harness.segmenter().execute(int(CHAT))

        assert len(await harness.stored(CHAT)) == 1
        assert await harness.stored(OTHER_CHAT) == ()


class TestIdempotency:
    async def test_a_second_pass_changes_nothing(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in range(1, 6)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))

        report = await segmenter.execute(int(CHAT))

        assert report.unchanged == 1
        assert report.created == 0
        assert not report.changed

    async def test_a_second_pass_commits_nothing(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in range(1, 6)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        before = harness.commits

        await segmenter.execute(int(CHAT))

        assert harness.commits == before

    async def test_identities_survive_a_rebuild(self, harness: _Harness) -> None:
        # The property that makes anything able to reference a conversation.
        await harness.store(
            *(message(n) for n in range(1, 6)),
            *(message(n, at=START + timedelta(hours=4, minutes=n)) for n in range(9, 12)),
        )
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        before = await harness.identities()

        await segmenter.execute(int(CHAT))

        assert await harness.identities() == before

    async def test_extents_survive_a_rebuild(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in range(1, 12)))
        harness.rules = SegmentationRules(gap=timedelta(minutes=2))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        before = await harness.spans()

        await segmenter.execute(int(CHAT))

        assert await harness.spans() == before

    async def test_a_rebuild_does_not_move_the_update_time(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in range(1, 6)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))

        harness.clock.advance(timedelta(days=1))
        await segmenter.execute(int(CHAT))

        (only,) = await harness.stored()
        assert only.updated_at == NOW

    async def test_re_ingesting_a_message_changes_nothing(self, harness: _Harness) -> None:
        # A duplicate stores nothing, so segmentation has nothing to redo.
        await harness.store(*(message(n) for n in range(1, 6)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        before = await harness.spans()

        with pytest.raises(ConstraintViolationError):
            await harness.store(message(3))
        report = await segmenter.execute(int(CHAT))

        assert await harness.spans() == before
        assert report.unchanged == 1


class TestLiveMessages:
    async def test_a_message_within_the_gap_extends_the_conversation(
        self, harness: _Harness
    ) -> None:
        await harness.store(*(message(n) for n in range(1, 6)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        (before,) = await harness.stored()

        await harness.store(message(6))
        report = await segmenter.execute(int(CHAT), since=message(6).sent_at)

        (after,) = await harness.stored()
        assert report.updated == 1
        assert after.id == before.id
        assert after.message_count == 6
        assert after.ended_at == message(6).sent_at

    async def test_a_message_after_a_silence_creates_a_conversation(
        self, harness: _Harness
    ) -> None:
        await harness.store(*(message(n) for n in range(1, 6)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        (before,) = await harness.stored()

        late = message(9, at=START + timedelta(hours=4))
        await harness.store(late)
        report = await segmenter.execute(int(CHAT), since=late.sent_at)

        stored = await harness.stored()
        assert report.created == 1
        assert len(stored) == 2
        assert stored[0].id == before.id
        assert stored[0].message_count == 5

    async def test_an_earlier_conversation_is_not_rewritten(self, harness: _Harness) -> None:
        # "Re-segmentation changes only what newly arrived messages require."
        await harness.store(*(message(n) for n in range(1, 6)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        (first,) = await harness.stored()

        late = message(9, at=START + timedelta(hours=4))
        await harness.store(late)
        harness.clock.advance(timedelta(hours=1))
        await segmenter.execute(int(CHAT), since=late.sent_at)

        stored = await harness.stored()
        assert stored[0].updated_at == first.updated_at

    async def test_the_window_reads_only_what_it_must(self, harness: _Harness) -> None:
        # An incremental pass reads from the start of the conversation the new
        # message follows -- it needs that conversation's last message to
        # compute the gap -- and nothing earlier.
        await harness.store(
            *(message(n) for n in range(1, 4)),
            *(message(n, at=START + timedelta(hours=4, minutes=n)) for n in (9, 10)),
            *(message(n, at=START + timedelta(hours=8, minutes=n)) for n in (20, 21)),
        )
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        assert len(await harness.stored()) == 3

        late = message(30, at=START + timedelta(hours=8, minutes=30))
        await harness.store(late)
        report = await segmenter.execute(int(CHAT), since=late.sent_at)

        # The third conversation's two messages plus the new one. The first two
        # conversations -- five messages -- were never read.
        assert report.messages == 3


class TestLateHistory:
    async def test_a_message_before_everything_extends_the_first_conversation(
        self, harness: _Harness
    ) -> None:
        await harness.store(*(message(n) for n in range(3, 8)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        (before,) = await harness.stored()

        early = message(1)
        await harness.store(early)
        report = await segmenter.execute(int(CHAT), since=early.sent_at)

        (after,) = await harness.stored()
        assert report.updated == 1
        assert after.id == before.id
        assert after.started_at == early.sent_at
        assert after.message_count == 6

    async def test_a_message_before_a_silence_creates_a_conversation(
        self, harness: _Harness
    ) -> None:
        await harness.store(*(message(n) for n in range(1, 6)))
        segmenter = harness.segmenter()
        await segmenter.execute(int(CHAT))
        (before,) = await harness.stored()

        ancient = message(99, at=START - timedelta(hours=4))
        await harness.store(ancient)
        await segmenter.execute(int(CHAT), since=ancient.sent_at)

        stored = await harness.stored()
        assert len(stored) == 2
        assert stored[0].message_count == 1
        assert stored[1].id == before.id

    async def test_a_message_joining_two_conversations_merges_them(self, harness: _Harness) -> None:
        # Backfill delivering the message that closes a gap. The merged episode
        # keeps the identity of whichever contributed more of it.
        early = [message(n) for n in range(1, 4)]
        late = [message(n, at=START + timedelta(hours=4, minutes=n)) for n in range(9, 11)]
        await harness.store(*early, *late)
        await harness.segmenter().execute(int(CHAT))
        before = await harness.identities()
        assert len(before) == 2

        harness.rules = SegmentationRules(gap=timedelta(hours=8))
        report = await harness.segmenter().execute(int(CHAT))

        stored = await harness.stored()
        assert len(stored) == 1
        assert stored[0].message_count == 5
        assert int(stored[0].id) == before[0]
        assert report.deleted == 1

    async def test_a_smaller_gap_splits_a_conversation(self, harness: _Harness) -> None:
        # The earlier half keeps the identity; the later half is new, because a
        # stored conversation can only be claimed once.
        await harness.store(
            *(message(n) for n in (1, 2)),
            *(message(n, at=START + timedelta(minutes=n + 30)) for n in (3, 4)),
        )
        await harness.segmenter().execute(int(CHAT))
        (before,) = await harness.stored()

        harness.rules = SegmentationRules(gap=timedelta(minutes=5))
        report = await harness.segmenter().execute(int(CHAT))

        stored = await harness.stored()
        assert len(stored) == 2
        assert stored[0].id == before.id
        assert report.created == 1
        assert report.updated == 1


class TestResumedBackfill:
    async def test_history_arriving_in_pages_converges(self, harness: _Harness) -> None:
        # Backfill walks backwards, one page at a time, segmenting after each.
        # The answer must be the one a single rebuild would give.
        pages = [
            [message(n) for n in (8, 9, 10)],
            [message(n) for n in (5, 6, 7)],
            [message(n) for n in (1, 2, 3)],
        ]
        segmenter = harness.segmenter()
        for page in pages:
            await harness.store(*page)
            await segmenter.execute(int(CHAT), since=min(m.sent_at for m in page))
        incremental = await harness.spans()

        # A full rebuild of the same messages.
        await segmenter.execute(int(CHAT))

        assert await harness.spans() == incremental

    async def test_and_the_identities_are_the_ones_it_already_had(self, harness: _Harness) -> None:
        pages = [
            [message(n) for n in (8, 9, 10)],
            [message(n) for n in (1, 2, 3)],
        ]
        segmenter = harness.segmenter()
        for page in pages:
            await harness.store(*page)
            await segmenter.execute(int(CHAT), since=min(m.sent_at for m in page))
        before = await harness.identities()

        await segmenter.execute(int(CHAT))

        assert await harness.identities() == before

    async def test_incremental_and_wholesale_agree_across_a_silence(
        self, harness: _Harness
    ) -> None:
        first = [message(n) for n in (1, 2, 3)]
        second = [message(n, at=START + timedelta(hours=4, minutes=n)) for n in (9, 10)]
        segmenter = harness.segmenter()

        await harness.store(*first)
        await segmenter.execute(int(CHAT))
        await harness.store(*second)
        await segmenter.execute(int(CHAT), since=min(m.sent_at for m in second))
        incremental = await harness.spans()

        fresh = _Harness()
        await fresh.setup()
        await fresh.store(*first, *second)
        await fresh.segmenter().execute(int(CHAT))

        assert await fresh.spans() == incremental


class TestMembershipIsTheRange:
    async def test_a_conversation_reads_back_its_own_messages(self, harness: _Harness) -> None:
        await harness.store(
            *(message(n) for n in range(1, 4)),
            *(message(n, at=START + timedelta(hours=4, minutes=n)) for n in (9, 10)),
        )
        await harness.segmenter().execute(int(CHAT))
        stored = await harness.stored()

        lookup = GetConversation(
            harness.unit_of_work, harness.conversations, harness.messages, harness.accounts
        )
        found = await lookup.execute(int(stored[0].id))

        assert found is not None
        _episode, messages = found
        assert [int(m.id) for m in messages] == [1, 2, 3]

    async def test_every_message_belongs_to_exactly_one_conversation(
        self, harness: _Harness
    ) -> None:
        messages = [message(n) for n in range(1, 20)]
        await harness.store(*messages)
        harness.rules = SegmentationRules(gap=timedelta(minutes=2))
        await harness.segmenter().execute(int(CHAT))

        stored = await harness.stored()

        for item in messages:
            owners = [c for c in stored if c.contains(item.sent_at)]
            assert len(owners) == 1, f"message {int(item.id)} has {len(owners)} owners"

    async def test_an_absent_conversation_is_none(self, harness: _Harness) -> None:
        lookup = GetConversation(
            harness.unit_of_work, harness.conversations, harness.messages, harness.accounts
        )

        assert await lookup.execute(9999) is None


class TestListing:
    async def test_it_returns_conversations_newest_first(self, harness: _Harness) -> None:
        await harness.store(
            *(message(n) for n in range(1, 4)),
            *(message(n, at=START + timedelta(hours=4, minutes=n)) for n in (9, 10)),
        )
        await harness.segmenter().execute(int(CHAT))

        page = await ListConversations(
            harness.unit_of_work, harness.conversations, harness.accounts
        ).execute(int(CHAT), PageRequest(limit=10))

        assert [c.started_at for c in page.items] == sorted(
            (c.started_at for c in page.items), reverse=True
        )

    async def test_a_chat_with_no_conversations_is_empty(self, harness: _Harness) -> None:
        page = await ListConversations(
            harness.unit_of_work, harness.conversations, harness.accounts
        ).execute(int(CHAT), PageRequest(limit=10))

        assert page.items == []


# ---------------------------------------------------------------------------
# Injected failures
# ---------------------------------------------------------------------------


class _FailingConversations(InMemoryConversationRepository):
    """A repository that fails at a chosen point in the pass."""

    __slots__ = ("_on_add", "_on_delete", "_on_update")

    def __init__(
        self,
        store: InMemoryConversationStore,
        account_id: AccountId,
        *,
        on_add: int | None = None,
        on_update: bool = False,
        on_delete: bool = False,
    ) -> None:
        """Bind to a store and choose where to fail."""
        super().__init__(store, account_id)
        self._on_add = on_add
        self._on_update = on_update
        self._on_delete = on_delete

    async def add(self, conversation: Conversation) -> None:
        """Persist, unless this is the addition that fails."""
        if self._on_add is not None:
            if self._on_add == 0:
                msg = "the process died here"
                raise RuntimeError(msg)
            self._on_add -= 1
        await super().add(conversation)

    async def update(self, conversation: Conversation) -> None:
        """Persist, unless updates fail."""
        if self._on_update:
            msg = "the process died here"
            raise RuntimeError(msg)
        await super().update(conversation)

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove, unless deletions fail."""
        if self._on_delete:
            msg = "the process died here"
            raise RuntimeError(msg)
        await super().delete(conversation_id)


class TestInjectedFailures:
    """The in-memory store writes through, so these prove the *call* sequence.

    That a rollback actually discards the writes is proved against a real
    database below -- this layer cannot show it, and saying so is better than a
    test that appears to.
    """

    async def test_a_failure_partway_through_stops_the_pass(self, harness: _Harness) -> None:
        await harness.store(
            *(message(n) for n in range(1, 4)),
            *(message(n, at=START + timedelta(hours=4, minutes=n)) for n in (9, 10)),
            *(message(n, at=START + timedelta(hours=8, minutes=n)) for n in (20, 21)),
        )
        harness.conversations = lambda _uow, account_id: _FailingConversations(  # type: ignore[method-assign]
            harness.conversation_store, account_id, on_add=1
        )

        with pytest.raises(RuntimeError, match="died here"):
            await harness.segmenter().execute(int(CHAT))

    async def test_nothing_commits(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in range(1, 4)))
        harness.conversations = lambda _uow, account_id: _FailingConversations(  # type: ignore[method-assign]
            harness.conversation_store, account_id, on_add=0
        )
        before = harness.commits

        with pytest.raises(RuntimeError):
            await harness.segmenter().execute(int(CHAT))

        assert harness.commits == before

    async def test_a_failure_while_updating_stops_the_pass(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in range(1, 4)))
        await harness.segmenter().execute(int(CHAT))
        await harness.store(message(4))
        harness.conversations = lambda _uow, account_id: _FailingConversations(  # type: ignore[method-assign]
            harness.conversation_store, account_id, on_update=True
        )

        with pytest.raises(RuntimeError, match="died here"):
            await harness.segmenter().execute(int(CHAT))

    async def test_a_failure_while_removing_stops_the_pass(self, harness: _Harness) -> None:
        early = [message(n) for n in range(1, 4)]
        late = [message(n, at=START + timedelta(hours=4, minutes=n)) for n in (9, 10)]
        await harness.store(*early, *late)
        await harness.segmenter().execute(int(CHAT))
        harness.rules = SegmentationRules(gap=timedelta(hours=8))
        harness.conversations = lambda _uow, account_id: _FailingConversations(  # type: ignore[method-assign]
            harness.conversation_store, account_id, on_delete=True
        )

        with pytest.raises(RuntimeError, match="died here"):
            await harness.segmenter().execute(int(CHAT))


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


async def _prepare(container: Container) -> tuple[Account, Chat]:
    """Create the schema, an active account, a contact and a chat."""
    await container.start_database()
    account = await container.create_account().execute(
        CreateAccountRequest(telegram_user_id=int(TelegramUserId(1001)), display_name="me")
    )
    contact = await container.create_contact().execute(
        telegram_user_id=int(COUNTERPART), display_name="Ada"
    )
    chat = await container.open_private_chat().execute(
        contact_id=int(contact.id), telegram_chat_id=int(TELEGRAM_CHAT)
    )
    return account, chat


async def _ingest(container: Container, chat: Chat, *offsets: int) -> None:
    """Store messages at a minute per offset."""
    await container.ingest_messages().execute(
        int(chat.id),
        [
            IncomingMessage(
                sender_kind=SenderKind.CONTACT,
                sent_at=START + timedelta(minutes=offset),
                text=f"message {offset}",
                message_type=MessageType.TEXT,
                telegram_message_id=offset,
            )
            for offset in offsets
        ],
    )


@pytest.fixture
async def stored(container: Container) -> AsyncIterator[Container]:
    """A container over a real SQLite file."""
    try:
        yield container
    finally:
        await container.aclose()


async def _conversations(container: Container, chat: Chat) -> tuple[Conversation, ...]:
    """Return a chat's conversations, oldest first.

    The account is resolved *before* the transaction opens. Resolving it inside
    would open a second one, and the whole application has a single connection
    permitting one at a time (ADR-034) -- so the two would deadlock on the lock.
    """
    account = await container.get_account().execute(None)
    assert account is not None
    async with container.unit_of_work() as uow:
        return await container.conversations(uow, account.id).list_from(chat.id)


async def _spans(container: Container, chat: Chat) -> list[tuple[datetime, datetime, int]]:
    """Return a chat's conversation extents, oldest first."""
    return [
        (c.started_at, c.ended_at, c.message_count) for c in await _conversations(container, chat)
    ]


async def _identities(container: Container, chat: Chat) -> list[int]:
    """Return a chat's conversation identifiers, oldest first."""
    return [int(c.id) for c in await _conversations(container, chat)]


class _AlwaysFailsToDelete(ConversationRepository):
    """A repository that refuses to remove anything, wrapping a real one."""

    __slots__ = ("_inner",)

    def __init__(self, inner: ConversationRepository) -> None:
        """Wrap the repository that would have worked."""
        self._inner = inner

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._inner.account_id

    async def get(self, conversation_id: ConversationId) -> Conversation | None:
        """Read through."""
        return await self._inner.get(conversation_id)

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Conversation]:
        """Read through."""
        return await self._inner.list_by_chat(chat_id, request)

    async def list_from(
        self, chat_id: ChatId, started_at: datetime | None = None
    ) -> tuple[Conversation, ...]:
        """Read through."""
        return await self._inner.list_from(chat_id, started_at)

    async def latest_before(self, chat_id: ChatId, instant: datetime) -> Conversation | None:
        """Read through."""
        return await self._inner.latest_before(chat_id, instant)

    async def add(self, conversation: Conversation) -> None:
        """Write through, so the pass gets far enough to matter."""
        await self._inner.add(conversation)

    async def update(self, conversation: Conversation) -> None:
        """Write through."""
        await self._inner.update(conversation)

    async def delete(self, conversation_id: ConversationId) -> None:
        """Fail, as a process dying between the writes and the commit does."""
        del conversation_id
        msg = "the process died here"
        raise RuntimeError(msg)


class TestAgainstARealDatabase:
    """The transaction boundary, which no in-memory fake can demonstrate."""

    async def test_a_first_pass_stores_conversations(self, stored: Container) -> None:
        _account, chat = await _prepare(stored)
        await _ingest(stored, chat, 1, 2, 3)

        report = await stored.segment_conversations().execute(int(chat.id))

        assert report.created == 1
        assert await _spans(stored, chat) == [
            (START + timedelta(minutes=1), START + timedelta(minutes=3), 3)
        ]

    async def test_a_rebuild_produces_an_identical_result(self, stored: Container) -> None:
        _account, chat = await _prepare(stored)
        await _ingest(stored, chat, 1, 2, 3, 500, 501)

        await stored.segment_conversations().execute(int(chat.id))
        before = (await _spans(stored, chat), await _identities(stored, chat))
        await stored.segment_conversations().execute(int(chat.id))

        assert (await _spans(stored, chat), await _identities(stored, chat)) == before

    async def test_ingestion_segments_without_being_asked(self, stored: Container) -> None:
        # The subscriber, wired at start-up. Ingestion announces what it wrote
        # and segmentation decides what that means; neither imports the other.
        _account, chat = await _prepare(stored)
        stored.subscribe_segmentation()

        await _ingest(stored, chat, 1, 2, 3)

        assert len(await _spans(stored, chat)) == 1

    async def test_a_later_batch_extends_rather_than_duplicates(self, stored: Container) -> None:
        _account, chat = await _prepare(stored)
        stored.subscribe_segmentation()
        await _ingest(stored, chat, 1, 2, 3)
        before = await _identities(stored, chat)

        await _ingest(stored, chat, 4, 5)

        assert await _identities(stored, chat) == before
        assert await _spans(stored, chat) == [
            (START + timedelta(minutes=1), START + timedelta(minutes=5), 5)
        ]

    async def test_a_batch_after_a_silence_adds_a_conversation(self, stored: Container) -> None:
        _account, chat = await _prepare(stored)
        stored.subscribe_segmentation()
        await _ingest(stored, chat, 1, 2, 3)

        await _ingest(stored, chat, 1000, 1001)

        assert len(await _spans(stored, chat)) == 2

    async def test_a_failure_before_commit_persists_nothing(self, stored: Container) -> None:
        # The merge case: two conversations become one, so the pass updates one
        # row and deletes the other. Failing on the delete is the last instant
        # at which the transaction can still take the update with it.
        _account, chat = await _prepare(stored)
        await _ingest(stored, chat, 1, 2, 3, 500, 501)
        await stored.segment_conversations().execute(int(chat.id))
        before = await _spans(stored, chat)
        assert len(before) == 2

        widened = SegmentConversations(
            stored.unit_of_work,
            lambda uow, account_id: _AlwaysFailsToDelete(stored.conversations(uow, account_id)),
            stored.messages,
            stored.accounts,
            stored.clock,
            stored.ids,
            SegmentationRules(gap=timedelta(days=7)),
        )
        with pytest.raises(RuntimeError, match="died here"):
            await widened.execute(int(chat.id))

        assert await _spans(stored, chat) == before

    async def test_and_the_identities_did_not_move(self, stored: Container) -> None:
        _account, chat = await _prepare(stored)
        await _ingest(stored, chat, 1, 2, 3, 500, 501)
        await stored.segment_conversations().execute(int(chat.id))
        before = await _identities(stored, chat)

        widened = SegmentConversations(
            stored.unit_of_work,
            lambda uow, account_id: _AlwaysFailsToDelete(stored.conversations(uow, account_id)),
            stored.messages,
            stored.accounts,
            stored.clock,
            stored.ids,
            SegmentationRules(gap=timedelta(days=7)),
        )
        with pytest.raises(RuntimeError):
            await widened.execute(int(chat.id))

        assert await _identities(stored, chat) == before

    async def test_two_conversations_cannot_begin_at_one_instant(self, stored: Container) -> None:
        # The unique index, which is what makes overlap unrepresentable.
        account, chat = await _prepare(stored)
        await _ingest(stored, chat, 1)
        await stored.segment_conversations().execute(int(chat.id))
        (existing,) = await _spans(stored, chat)

        async with stored.unit_of_work() as uow:
            repository = stored.conversations(uow, account.id)
            with pytest.raises(ConstraintViolationError):
                await repository.add(
                    Conversation.spanning(
                        conversation_id=ConversationId(999_999),
                        account_id=account.id,
                        chat_id=chat.id,
                        started_at=existing[0],
                        ended_at=existing[1],
                        message_count=1,
                        now=NOW,
                    )
                )

    async def test_deleting_a_chat_removes_its_conversations(self, stored: Container) -> None:
        account, chat = await _prepare(stored)
        await _ingest(stored, chat, 1, 2, 3)
        await stored.segment_conversations().execute(int(chat.id))

        async with stored.unit_of_work() as uow:
            await stored.database.executor.run(
                lambda: uow.connection.execute(
                    text("DELETE FROM chats WHERE id = :id"), {"id": int(chat.id)}
                )
            )
            remaining = await stored.conversations(uow, account.id).list_from(chat.id)

        assert remaining == ()


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
    monkeypatch.setenv("TGASSIST_CONVERSATION__GAP_MINUTES", "60")

    store = InMemorySecretStore()
    monkeypatch.setattr("tgassist.application.container.build_default_secret_store", lambda: store)
    return data_dir


def _run(*command: str) -> str:
    """Invoke the CLI and return its output, failing loudly if the command did."""
    result = runner.invoke(app, list(command))
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def chat_id() -> str:
    """Create an account, a contact and a chat, and return the chat's identifier."""
    _run("account", "create", "1001", "Primary")
    added = _run("contact", "add", str(int(COUNTERPART)), "Ada")
    contact_id = added.split("Added contact ")[1].split(":")[0]
    _run("chat", "open", str(int(TELEGRAM_CHAT)), "--contact", contact_id)
    rows = [line for line in _run("chat", "list").splitlines() if "private" in line]
    assert rows
    return rows[0].split()[0]


def _ingest_cli(chat: str, *offsets: int) -> None:
    """Ingest messages a minute apart from a fixed start, through the CLI."""
    for offset in offsets:
        moment = (START + timedelta(minutes=offset)).isoformat()
        _run(
            "message",
            "ingest",
            chat,
            f"message {offset}",
            "--sent-at",
            moment,
            "--telegram-id",
            str(offset),
        )


def _conversation_ids(chat: str) -> list[str]:
    """Return the identifiers `conversation list` printed, newest first."""
    listing = _run("conversation", "list", chat)
    return [line.split()[0] for line in listing.splitlines() if "message(s)" in line]


@pytest.mark.usefixtures("cli_env")
class TestConversationCommands:
    """The flow the goal describes: rebuild, list, show, ingest more, rebuild."""

    def test_rebuild_reports_what_it_computed(self, chat_id: str) -> None:
        _ingest_cli(chat_id, 1, 2, 3)

        result = runner.invoke(app, ["conversation", "rebuild", chat_id])

        assert result.exit_code == 0, result.output
        assert "3 message(s) in 1 conversation(s)" in result.output

    def test_rebuilding_twice_changes_nothing(self, chat_id: str) -> None:
        _ingest_cli(chat_id, 1, 2, 3)
        _run("conversation", "rebuild", chat_id)

        result = runner.invoke(app, ["conversation", "rebuild", chat_id])

        assert "Nothing changed" in result.output

    def test_list_shows_each_conversation(self, chat_id: str) -> None:
        _ingest_cli(chat_id, 1, 2, 3, 500, 501)
        _run("conversation", "rebuild", chat_id)

        result = runner.invoke(app, ["conversation", "list", chat_id])

        assert result.exit_code == 0, result.output
        assert "2 conversation(s)" in result.output

    def test_show_prints_the_messages_it_covers(self, chat_id: str) -> None:
        _ingest_cli(chat_id, 1, 2, 3)
        _run("conversation", "rebuild", chat_id)
        identifier = _conversation_ids(chat_id)[0]

        result = runner.invoke(app, ["conversation", "show", identifier])

        assert result.exit_code == 0, result.output
        assert "3 message(s)" in result.output
        assert "message 1" in result.output
        assert "message 3" in result.output

    def test_an_unknown_conversation_is_reported(self, chat_id: str) -> None:
        assert chat_id  # the account and chat exist; the conversation does not

        result = runner.invoke(app, ["conversation", "show", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_more_messages_change_only_the_affected_conversation(self, chat_id: str) -> None:
        _ingest_cli(chat_id, 1, 2, 3, 500, 501)
        _run("conversation", "rebuild", chat_id)
        before = _conversation_ids(chat_id)

        _ingest_cli(chat_id, 502)
        _run("conversation", "rebuild", chat_id)

        assert _conversation_ids(chat_id) == before
        assert "3 message(s)" in _run("conversation", "show", before[0])

    def test_a_new_episode_appears_without_disturbing_the_others(self, chat_id: str) -> None:
        _ingest_cli(chat_id, 1, 2, 3)
        _run("conversation", "rebuild", chat_id)
        before = _conversation_ids(chat_id)

        _ingest_cli(chat_id, 900)
        _run("conversation", "rebuild", chat_id)

        after = _conversation_ids(chat_id)
        assert len(after) == 2
        assert before[0] in after

    def test_a_chat_with_no_conversations_says_what_to_run(self, chat_id: str) -> None:
        result = runner.invoke(app, ["conversation", "list", chat_id])

        assert "conversation rebuild" in result.output


# ---------------------------------------------------------------------------
# The message repository's new read
# ---------------------------------------------------------------------------


class TestListSince:
    async def test_it_returns_messages_oldest_first(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in (3, 1, 2)))

        found = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_since(CHAT)

        assert [int(m.id) for m in found] == [1, 2, 3]

    async def test_it_bounds_the_window(self, harness: _Harness) -> None:
        await harness.store(*(message(n) for n in range(1, 6)))

        found = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_since(
            CHAT, message(3).sent_at
        )

        assert [int(m.id) for m in found] == [3, 4, 5]

    async def test_an_empty_chat_returns_nothing(self, harness: _Harness) -> None:
        assert await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_since(CHAT) == ()

    async def test_another_chat_is_not_returned(self, harness: _Harness) -> None:
        await harness._add_chat(OTHER_CHAT, OTHER_CONTACT, TelegramChatId(6000))
        await harness.store(message(1), message(2, chat=OTHER_CHAT))

        found = await harness.messages(harness.unit_of_work(), ACCOUNT_A).list_since(CHAT)

        assert [int(m.id) for m in found] == [1]
