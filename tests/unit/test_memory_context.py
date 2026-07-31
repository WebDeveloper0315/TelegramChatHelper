"""Assembling a context: the pipeline, the scope and the accounting.

The selector is tested exhaustively next door; this file is about the three
things only the use case can get wrong:

* **contact scope** -- retrieval never crosses contacts, and a chat with no
  single counterpart sees only the facts about nobody in particular;
* **accounting** -- ``BuildMemoryContext`` counts what it selects and
  ``GetMemoryContext`` does not, in the same transaction as the read;
* **honesty** -- a truncated candidate set, an over-budget omission and an empty
  context are three different things and the report says which.

No model is called anywhere here. Retrieval happens before generation and can be
inspected on its own, which is the whole point of the slice (ADR-060).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock, InMemorySecretStore, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.chat_repository import InMemoryChatRepository, InMemoryChatStore
from tests.fakes.event_bus import RecordingEventBus
from tests.fakes.memory_repository import InMemoryMemoryRepository, InMemoryMemoryStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.memory_context import (
    BuildMemoryContext,
    GetMemoryContext,
)
from tgassist.domain.errors import RecordNotFoundError
from tgassist.domain.events import MemoriesRetrieved
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import AiProcessingMode, Chat, ChatType
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    ContactId,
    ConversationId,
    MemoryId,
    MemoryProposalId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.memory import (
    Confidence,
    Importance,
    Memory,
    MemoryCategory,
    MemoryKey,
    MemorySource,
)
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_repository import MemoryRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.domain.services.memory_selection import (
    OmissionReason,
    SelectionRules,
    memory_tokens,
)
from tgassist.presentation.cli.app import app

runner = CliRunner()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CONTACT = ContactId(101)
OTHER_CONTACT = ContactId(102)
CHAT = ChatId(11)
OTHER_CHAT = ChatId(12)
GROUP_CHAT = ChatId(13)


def make_memory(  # noqa: PLR0913 - one argument per field a test varies
    memory_id: int,
    *,
    contact_id: ContactId | None = CONTACT,
    category: MemoryCategory = MemoryCategory.OTHER,
    value: str = "a fact",
    confidence: float = 0.5,
    importance: float = 0.5,
    offset_minutes: int = 0,
) -> Memory:
    """Build a live memory."""
    return Memory(
        id=MemoryId(memory_id),
        account_id=ACCOUNT_A,
        contact_id=contact_id,
        category=category,
        key=MemoryKey.of(f"{value} {memory_id}"),
        value=value,
        confidence=Confidence(confidence),
        source=MemorySource.AI_APPROVED,
        proposal_id=MemoryProposalId(500 + memory_id),
        conversation_id=ConversationId(301),
        ai_call_id=AiCallId(401),
        created_at=NOW + timedelta(minutes=offset_minutes),
        importance=Importance(importance),
    )


class _Harness:
    """A retrieval environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(OTHER_CONTACT): int(ACCOUNT_A)},
        )
        self.memory_store = InMemoryMemoryStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(OTHER_CONTACT): int(ACCOUNT_A)},
        )
        self.clock = AdvanceableClock(LATER)
        self.ids = SequentialIdGenerator(start=900)
        self.events = RecordingEventBus()
        self.rules = SelectionRules()
        self.max_candidates = 500
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()
        self.memories_factory: Any = self.memories

    def unit_of_work(self) -> InMemoryUnitOfWork:
        uow = self._factory()
        self.units.append(uow)
        return uow

    def accounts(self, _uow: UnitOfWork) -> InMemoryAccountRepository:
        return self.accounts_repository

    def chats(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryChatRepository:
        return InMemoryChatRepository(self.chat_store, account_id)

    def memories(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryMemoryRepository:
        return InMemoryMemoryRepository(self.memory_store, account_id)

    async def setup(self) -> None:
        """Create an account, two private chats and a group chat."""
        await self.accounts_repository.add(
            Account.create(
                account_id=ACCOUNT_A,
                telegram_user_id=TelegramUserId(1001),
                display_name="me",
                now=NOW,
                is_active=True,
            )
        )
        for chat_id, contact_id in ((CHAT, CONTACT), (OTHER_CHAT, OTHER_CONTACT)):
            await self.chats(self.unit_of_work(), ACCOUNT_A).add(
                Chat.private_with(
                    chat_id=chat_id,
                    account_id=ACCOUNT_A,
                    telegram_chat_id=TelegramChatId(5000 + int(chat_id)),
                    contact_id=contact_id,
                    now=NOW,
                    ai_processing_mode=AiProcessingMode.LOCAL_ONLY,
                )
            )
        await self.chats(self.unit_of_work(), ACCOUNT_A).add(
            Chat.group_titled(
                chat_id=GROUP_CHAT,
                account_id=ACCOUNT_A,
                telegram_chat_id=TelegramChatId(-5013),
                chat_type=ChatType.GROUP,
                title="Book club",
                now=NOW,
            )
        )

    async def remember(self, *memories: Memory) -> None:
        """Store memories directly, bypassing review -- this is not about that."""
        repository = self.memories(self.unit_of_work(), ACCOUNT_A)
        for memory in memories:
            await repository.add(memory)

    def build(self) -> BuildMemoryContext:
        return BuildMemoryContext(
            self.unit_of_work,
            self.memories_factory,
            self.chats,
            self.accounts,
            self.clock,
            self.rules,
            self.max_candidates,
            self.events,
        )

    def read(self) -> GetMemoryContext:
        return GetMemoryContext(
            self.unit_of_work,
            self.memories_factory,
            self.chats,
            self.accounts,
            self.clock,
            self.rules,
            self.max_candidates,
            self.events,
        )

    async def stored(self, memory_id: int) -> Memory | None:
        return await self.memories(self.unit_of_work(), ACCOUNT_A).get(MemoryId(memory_id))


@pytest.fixture
async def harness() -> _Harness:
    """One account, two private chats and a group chat."""
    built = _Harness()
    await built.setup()
    return built


# ---------------------------------------------------------------------------
# Building a context
# ---------------------------------------------------------------------------


class TestBuildingAContext:
    async def test_it_selects_a_contact_s_memories(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1), make_memory(2, value="another fact"))

        context = await harness.read().execute(int(CHAT))

        assert len(context.memories) == 2
        assert context.contact_id == CONTACT

    async def test_an_unknown_person_has_an_empty_context(self, harness: _Harness) -> None:
        context = await harness.read().execute(int(CHAT))

        assert context.is_empty
        assert context.tokens == 0
        assert context.selection.candidates == 0

    async def test_it_ranks_what_it_selects(self, harness: _Harness) -> None:
        await harness.remember(
            make_memory(1, category=MemoryCategory.INTEREST),
            make_memory(2, category=MemoryCategory.CONSTRAINT, value="do not mention it"),
        )

        context = await harness.read().execute(int(CHAT))

        assert [int(m.id) for m in context.memories] == [2, 1]

    async def test_it_reports_what_the_context_costs(self, harness: _Harness) -> None:
        memory = make_memory(1, value="12345678")
        await harness.remember(memory)

        context = await harness.read().execute(int(CHAT))

        assert context.tokens == memory_tokens(memory)

    async def test_it_explains_every_selection(self, harness: _Harness) -> None:
        # A selection nobody can read is one nobody can disagree with, and
        # disagreeing with it is how the ranking gets better.
        await harness.remember(make_memory(1, category=MemoryCategory.PLAN))

        context = await harness.read().execute(int(CHAT))

        why = context.why(context.memories[0])
        assert "category plan" in why
        assert "importance" in why
        assert "confidence" in why

    async def test_a_forgotten_memory_never_reaches_a_context(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1))
        await harness.memories(harness.unit_of_work(), ACCOUNT_A).delete(MemoryId(1), LATER)

        context = await harness.read().execute(int(CHAT))

        assert context.is_empty

    async def test_an_unknown_chat_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No chat"):
            await harness.read().execute(9999)


class TestContactScope:
    async def test_retrieval_never_crosses_contacts(self, harness: _Harness) -> None:
        # The rule the whole feature rests on.
        await harness.remember(
            make_memory(1, contact_id=CONTACT, value="about one"),
            make_memory(2, contact_id=OTHER_CONTACT, value="about the other"),
        )

        context = await harness.read().execute(int(CHAT))

        assert [int(m.id) for m in context.memories] == [1]

    async def test_a_group_chat_sees_only_contactless_memories(self, harness: _Harness) -> None:
        # Facts about nobody in particular -- not everybody's.
        await harness.remember(
            make_memory(1, contact_id=CONTACT, value="about one"),
            make_memory(2, contact_id=None, value="about the group"),
        )

        context = await harness.read().execute(int(GROUP_CHAT))

        assert [int(m.id) for m in context.memories] == [2]
        assert context.contact_id is None

    async def test_a_private_chat_does_not_see_contactless_memories(
        self, harness: _Harness
    ) -> None:
        # The partition is strict in both directions: a fact from a group
        # conversation is not about this person.
        await harness.remember(make_memory(1, contact_id=None))

        context = await harness.read().execute(int(CHAT))

        assert context.is_empty

    async def test_a_group_chat_with_nothing_known_is_empty(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1, contact_id=CONTACT))

        context = await harness.read().execute(int(GROUP_CHAT))

        assert context.is_empty


class TestTheBudget:
    async def test_what_does_not_fit_is_reported(self, harness: _Harness) -> None:
        harness.rules = SelectionRules(token_budget=12)
        await harness.remember(
            make_memory(1, category=MemoryCategory.CONSTRAINT, value="12345678"),
            make_memory(2, category=MemoryCategory.PLAN, value="x" * 100),
        )

        context = await harness.read().execute(int(CHAT))

        assert [int(m.id) for m in context.memories] == [1]
        assert context.omitted[0].reason is OmissionReason.OVER_BUDGET

    async def test_the_cap_is_reported_separately(self, harness: _Harness) -> None:
        # "Too much to say" and "too little room" are different problems.
        harness.rules = SelectionRules(max_memories=1)
        await harness.remember(make_memory(1), make_memory(2, value="another"))

        context = await harness.read().execute(int(CHAT))

        assert context.omitted[0].reason is OmissionReason.OVER_LIMIT

    async def test_a_truncated_candidate_set_is_reported(self, harness: _Harness) -> None:
        # A context that omitted something it never looked at is a different
        # thing from one that ranked it last.
        harness.max_candidates = 2
        await harness.remember(
            make_memory(1, value="one"), make_memory(2, value="two"), make_memory(3, value="three")
        )

        context = await harness.read().execute(int(CHAT))

        assert context.truncated
        assert context.selection.candidates == 2

    async def test_an_untruncated_one_is_not(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1))

        assert not (await harness.read().execute(int(CHAT))).truncated


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


class TestRetrievalAccounting:
    async def test_building_records_the_retrieval(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1))

        context = await harness.build().execute(int(CHAT))

        assert context.recorded
        stored = await harness.stored(1)
        assert stored is not None
        assert stored.retrieval_count == 1
        assert stored.last_retrieved_at == LATER

    async def test_reading_records_nothing(self, harness: _Harness) -> None:
        # Looking at what would be sent is not using it, and an inspection that
        # inflated the counters would corrupt the measurement it exposes.
        await harness.remember(make_memory(1))

        context = await harness.read().execute(int(CHAT))

        assert not context.recorded
        stored = await harness.stored(1)
        assert stored is not None
        assert stored.retrieval_count == 0

    async def test_both_choose_the_same_memories(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1), make_memory(2, value="another"))

        built = await harness.build().execute(int(CHAT))
        read = await harness.read().execute(int(CHAT))

        assert [int(m.id) for m in built.memories] == [int(m.id) for m in read.memories]

    async def test_only_what_was_selected_is_counted(self, harness: _Harness) -> None:
        harness.rules = SelectionRules(max_memories=1)
        await harness.remember(
            make_memory(1, category=MemoryCategory.CONSTRAINT),
            make_memory(2, category=MemoryCategory.INTEREST, value="another"),
        )

        await harness.build().execute(int(CHAT))

        omitted = await harness.stored(2)
        assert omitted is not None
        assert omitted.retrieval_count == 0

    async def test_counting_accumulates_across_contexts(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1))

        await harness.build().execute(int(CHAT))
        await harness.build().execute(int(CHAT))

        stored = await harness.stored(1)
        assert stored is not None
        assert stored.retrieval_count == 2

    async def test_an_empty_context_writes_nothing(self, harness: _Harness) -> None:
        context = await harness.build().execute(int(CHAT))

        assert not context.recorded
        assert not any(unit.is_committed for unit in harness.units)

    async def test_the_accounting_is_the_reading_transaction(self, harness: _Harness) -> None:
        # One transaction covers the read and the write, so a context and the
        # record of it existing cannot disagree.
        await harness.remember(make_memory(1))
        before = sum(1 for unit in harness.units if unit.is_committed)

        await harness.build().execute(int(CHAT))

        assert sum(1 for unit in harness.units if unit.is_committed) == before + 1

    async def test_reading_commits_nothing(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1))
        before = sum(1 for unit in harness.units if unit.is_committed)

        await harness.read().execute(int(CHAT))

        assert sum(1 for unit in harness.units if unit.is_committed) == before

    def test_the_two_use_cases_say_which_they_are(self, harness: _Harness) -> None:
        assert harness.build().records_retrieval
        assert not harness.read().records_retrieval


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class _FailsBeforeCommit(MemoryRepository):
    """A memory repository that dies at a chosen point in retrieval."""

    def __init__(self, inner: MemoryRepository, *, on_read: bool = False) -> None:
        self._inner = inner
        self._on_read = on_read

    @property
    def account_id(self) -> AccountId:
        return self._inner.account_id

    async def add(self, memory: Memory) -> None:
        await self._inner.add(memory)

    async def get(self, memory_id: MemoryId) -> Memory | None:
        return await self._inner.get(memory_id)

    async def get_by_proposal(self, proposal_id: MemoryProposalId) -> Memory | None:
        return await self._inner.get_by_proposal(proposal_id)

    async def list_active(self, request: PageRequest) -> Any:
        return await self._inner.list_active(request)

    async def list_for_contact(
        self, contact_id: ContactId | None, *, limit: int
    ) -> tuple[Memory, ...]:
        if self._on_read:
            msg = "died here"
            raise RuntimeError(msg)
        return await self._inner.list_for_contact(contact_id, limit=limit)

    async def mark_retrieved(self, memory_ids: Sequence[MemoryId], now: datetime) -> int:
        # Writes first, then dies: the counters are in the open transaction when
        # the failure lands, which is the state the rollback test needs.
        await self._inner.mark_retrieved(memory_ids, now)
        msg = "died here"
        raise RuntimeError(msg)

    async def delete(self, memory_id: MemoryId, now: datetime) -> bool:
        return await self._inner.delete(memory_id, now)


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


async def _prepare(container: Container) -> tuple[int, int]:
    """Create an account, a contact, a chat and three memories about them."""
    await container.start()
    await container.create_account().execute(
        CreateAccountRequest(telegram_user_id=1001, display_name="me")
    )
    contact = await container.create_contact().execute(telegram_user_id=2002, display_name="Ada")
    chat = await container.open_private_chat().execute(
        contact_id=int(contact.id),
        telegram_chat_id=5000,
        ai_processing_mode=AiProcessingMode.LOCAL_ONLY,
    )
    account = await container.get_account().execute(None)
    assert account is not None

    async with container.unit_of_work() as uow:
        repository = container.memories(uow, account.id)
        for index, (category, value) in enumerate(
            (
                (MemoryCategory.INTEREST, "Reads science fiction"),
                (MemoryCategory.CONSTRAINT, "Do not mention the old job"),
                (MemoryCategory.LOCATION, "Lives in Lisbon"),
            ),
            start=1,
        ):
            await repository.add(
                Memory(
                    id=MemoryId(container.ids.new_id()),
                    account_id=account.id,
                    contact_id=contact.id,
                    category=category,
                    key=MemoryKey.of(value),
                    value=value,
                    confidence=Confidence(0.8),
                    source=MemorySource.USER,
                    proposal_id=None,
                    conversation_id=None,
                    ai_call_id=None,
                    created_at=NOW + timedelta(minutes=index),
                    importance=Importance.normal(),
                )
            )
        await uow.commit()
    return int(chat.id), int(contact.id)


@pytest.fixture
async def stored(container: Container) -> AsyncIterator[Container]:
    """A container over a real SQLite file."""
    try:
        yield container
    finally:
        await container.aclose()


class TestAgainstARealDatabase:
    async def test_a_context_is_assembled(self, stored: Container) -> None:
        chat_id, _contact = await _prepare(stored)

        context = await stored.get_memory_context().execute(chat_id)

        assert len(context.memories) == 3
        assert context.memories[0].category is MemoryCategory.CONSTRAINT

    async def test_the_ranking_survives_the_database(self, stored: Container) -> None:
        # Ranking is pure, but the values it ranks made a round trip through
        # SQLite -- a float that lost precision would reorder silently.
        chat_id, _contact = await _prepare(stored)

        context = await stored.get_memory_context().execute(chat_id)

        assert [m.category for m in context.memories] == [
            MemoryCategory.CONSTRAINT,
            MemoryCategory.LOCATION,
            MemoryCategory.INTEREST,
        ]

    async def test_building_records_the_retrieval(self, stored: Container) -> None:
        chat_id, _contact = await _prepare(stored)

        context = await stored.build_memory_context().execute(chat_id)

        for memory in context.memories:
            found = await stored.get_memory().execute(int(memory.id))
            assert found is not None
            assert found.retrieval_count == 1
            assert found.last_retrieved_at is not None

    async def test_reading_records_nothing(self, stored: Container) -> None:
        chat_id, _contact = await _prepare(stored)

        context = await stored.get_memory_context().execute(chat_id)

        for memory in context.memories:
            found = await stored.get_memory().execute(int(memory.id))
            assert found is not None
            assert found.retrieval_count == 0

    async def test_an_exception_during_the_read_persists_nothing(self, stored: Container) -> None:
        chat_id, _contact = await _prepare(stored)
        builder = BuildMemoryContext(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(
                stored.memories(uow, account_id), on_read=True
            ),
            stored.chats,
            stored.accounts,
            stored.clock,
        )

        with pytest.raises(RuntimeError, match="died here"):
            await builder.execute(chat_id)

        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert all(memory.retrieval_count == 0 for memory in page.items)

    async def test_an_exception_after_the_accounting_persists_nothing(
        self, stored: Container
    ) -> None:
        # The counters are written into the open transaction when the failure
        # lands, and none of them survives.
        chat_id, _contact = await _prepare(stored)
        builder = BuildMemoryContext(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(stored.memories(uow, account_id)),
            stored.chats,
            stored.accounts,
            stored.clock,
        )

        with pytest.raises(RuntimeError, match="died here"):
            await builder.execute(chat_id)

        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert all(memory.retrieval_count == 0 for memory in page.items)

    async def test_a_failed_retrieval_can_be_retried(self, stored: Container) -> None:
        chat_id, _contact = await _prepare(stored)
        builder = BuildMemoryContext(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(stored.memories(uow, account_id)),
            stored.chats,
            stored.accounts,
            stored.clock,
        )
        with pytest.raises(RuntimeError):
            await builder.execute(chat_id)

        context = await stored.build_memory_context().execute(chat_id)

        found = await stored.get_memory().execute(int(context.memories[0].id))
        assert found is not None
        assert found.retrieval_count == 1

    async def test_the_same_context_twice_is_the_same_context(self, stored: Container) -> None:
        # Deterministic against real data, not just in the pure selector.
        chat_id, _contact = await _prepare(stored)

        first = await stored.get_memory_context().execute(chat_id)
        second = await stored.get_memory_context().execute(chat_id)

        assert [int(m.id) for m in first.memories] == [int(m.id) for m in second.memories]
        assert first.tokens == second.tokens


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


def _run_cli(*command: str) -> str:
    """Invoke the CLI and return its output, failing loudly if the command did."""
    result = runner.invoke(app, list(command))
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.usefixtures("cli_env")
class TestContextCommand:
    """Retrieval, end to end."""

    @pytest.fixture
    def chat(self) -> str:
        """Create a chat with three memories about its contact."""
        import asyncio  # noqa: PLC0415

        async def seed() -> int:
            container = Container.create()
            try:
                chat_id, _contact = await _prepare(container)
                return chat_id
            finally:
                await container.aclose()

        return str(asyncio.run(seed()))

    def test_it_shows_the_selected_memories(self, chat: str) -> None:
        output = _run_cli("memory", "context", chat)

        assert "Do not mention the old job" in output
        assert "3 candidate(s), 3 selected" in output

    def test_it_shows_the_ranking_order(self, chat: str) -> None:
        output = _run_cli("memory", "context", chat)

        constraint = output.index("Do not mention the old job")
        interest = output.index("Reads science fiction")
        assert constraint < interest

    def test_it_explains_each_selection(self, chat: str) -> None:
        output = _run_cli("memory", "context", chat)

        assert "category constraint (priority 0)" in output
        assert "importance 0.50" in output
        assert "tokens" in output

    def test_it_reports_the_token_usage(self, chat: str) -> None:
        assert "/800 tokens" in _run_cli("memory", "context", chat)

    def test_it_records_nothing_by_default(self, chat: str) -> None:
        _run_cli("memory", "context", chat)

        identifier = _run_cli("memory", "list").splitlines()[0].split()[0]
        assert "retrieved    0 time(s)" in _run_cli("memory", "show", identifier)

    def test_record_counts_the_retrieval(self, chat: str) -> None:
        output = _run_cli("memory", "context", chat, "--record")

        assert "Recorded as a retrieval" in output
        identifier = _run_cli("memory", "list").splitlines()[0].split()[0]
        assert "retrieved    1 time(s)" in _run_cli("memory", "show", identifier)

    def test_it_reports_omissions(self, chat: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TGASSIST_MEMORY__CONTEXT_MAX_MEMORIES", "1")

        output = _run_cli("memory", "context", chat)

        assert "2 omitted:" in output
        assert "over_limit" in output

    def test_it_reports_an_over_budget_omission(
        self, chat: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TGASSIST_MEMORY__CONTEXT_TOKEN_BUDGET", "12")

        output = _run_cli("memory", "context", chat)

        assert "over_budget" in output

    def test_a_chat_with_nothing_known_says_so(self, chat: str) -> None:  # noqa: ARG002
        _run_cli("contact", "add", "3003", "Bob")
        contacts = _run_cli("contact", "list").splitlines()
        other = contacts[0].split()[0]
        _run_cli("chat", "open", "6000", "--contact", other)
        chats = _run_cli("chat", "list").splitlines()
        empty_chat = chats[0].split()[0]

        output = _run_cli("memory", "context", empty_chat)

        assert "Nothing to tell a model" in output

    def test_an_unknown_chat_is_reported(self, chat: str) -> None:  # noqa: ARG002
        result = runner.invoke(app, ["memory", "context", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestTheRetrievalEvent:
    """``MemoriesRetrieved``: published by the recording path only."""

    async def test_building_publishes_it(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1), make_memory(2, value="another"))

        await harness.build().execute(int(CHAT))

        (published,) = harness.events.events_of(MemoriesRetrieved)
        assert isinstance(published, MemoriesRetrieved)
        assert published.count == 2
        assert published.chat_id == int(CHAT)
        assert published.contact_id == int(CONTACT)

    async def test_it_carries_what_was_considered(self, harness: _Harness) -> None:
        # "One of two" and "one of ninety" are different things to know about a
        # ranking, and a subscriber should not need a second query to tell them
        # apart.
        harness.rules = SelectionRules(max_memories=1)
        await harness.remember(make_memory(1), make_memory(2, value="another"))

        await harness.build().execute(int(CHAT))

        (published,) = harness.events.events_of(MemoriesRetrieved)
        assert isinstance(published, MemoriesRetrieved)
        assert (published.count, published.candidates) == (1, 2)

    async def test_it_carries_the_token_estimate(self, harness: _Harness) -> None:
        memory = make_memory(1, value="12345678")
        await harness.remember(memory)

        await harness.build().execute(int(CHAT))

        (published,) = harness.events.events_of(MemoriesRetrieved)
        assert isinstance(published, MemoriesRetrieved)
        assert published.tokens == memory_tokens(memory)

    async def test_inspecting_publishes_nothing(self, harness: _Harness) -> None:
        # An inspection is not a retrieval, and one that announced itself would
        # make the events disagree with the counters (ADR-060).
        await harness.remember(make_memory(1))

        await harness.read().execute(int(CHAT))

        assert harness.events.events_of(MemoriesRetrieved) == []

    async def test_an_empty_context_publishes_nothing(self, harness: _Harness) -> None:
        # "No memories were used" is not a fact worth waking a subscriber for,
        # and nothing was recorded either.
        await harness.build().execute(int(CHAT))

        assert harness.events.events_of(MemoriesRetrieved) == []

    async def test_a_group_chat_publishes_no_contact(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1, contact_id=None))

        await harness.build().execute(int(GROUP_CHAT))

        (published,) = harness.events.events_of(MemoriesRetrieved)
        assert isinstance(published, MemoriesRetrieved)
        assert published.contact_id is None

    async def test_it_is_published_once_per_retrieval(self, harness: _Harness) -> None:
        await harness.remember(make_memory(1))

        await harness.build().execute(int(CHAT))
        await harness.build().execute(int(CHAT))

        assert len(harness.events.events_of(MemoriesRetrieved)) == 2
