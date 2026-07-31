"""The AI call repository, run against both implementations.

What is asserted here is the append-only shape, ownership, the composite foreign
key to ``chats`` and what happens when the chat goes away, scope isolation,
newest-first ordering, and the one thing this table is unusual in: that a cost
survives a round trip **exactly**, because it is stored as text rather than as a
floating-point number (ADR-057).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.ai_call_repository import InMemoryAiCallRepository, InMemoryAiCallStore
from tgassist.domain.errors import ConstraintViolationError, DomainValidationError
from tgassist.domain.model.account import Account
from tgassist.domain.model.ai import (
    AiCall,
    AiModel,
    AiOutcome,
    AiVendor,
    DataBoundary,
    FinishReason,
    PromptVersion,
    TokenUsage,
    digest_of,
)
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    ContactId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.ai_call_repository import AiCallRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAiCallRepository,
    SqlAlchemyUnitOfWork,
    SqlChatRepository,
    SqlContactRepository,
    SqliteDatabase,
)

EPOCH = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CHAT_A = ChatId(11)
CHAT_B = ChatId(22)
ABSENT_CHAT = ChatId(999)

PROMPT = PromptVersion(prompt_id="extract-memory", version="2")

PRICED = AiModel(
    vendor=AiVendor.ANTHROPIC,
    identifier="claude-sonnet-5",
    data_boundary=DataBoundary.EXTERNAL,
    input_cost_per_million=Decimal(3),
    output_cost_per_million=Decimal(15),
)
UNPRICED = AiModel(
    vendor=AiVendor.FAKE,
    identifier="fake-local-1",
    data_boundary=DataBoundary.LOCAL,
)


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own a call."""
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
    """Build a private chat for calls to refer to."""
    return Chat.private_with(
        chat_id=chat_id,
        account_id=account_id,
        telegram_chat_id=TelegramChatId(5000 + int(chat_id)),
        contact_id=ContactId(100 + int(account_id)),
        now=EPOCH,
    )


def make_call(  # noqa: PLR0913 - one argument per field a test varies
    call_id: int,
    account_id: AccountId,
    *,
    chat_id: ChatId | None = None,
    model: AiModel = PRICED,
    outcome: AiOutcome = AiOutcome.SUCCESS,
    finish_reason: FinishReason | None = FinishReason.STOP,
    usage: TokenUsage | None = None,
    response: str | None = "an answer",
    keep_response: bool = False,
    offset_minutes: int = 0,
) -> AiCall:
    """Build a recorded call."""
    return AiCall.record(
        call_id=AiCallId(call_id),
        account_id=account_id,
        chat_id=chat_id,
        model=model,
        prompt=PROMPT,
        task_kind="contract",
        outcome=outcome,
        latency_ms=120,
        now=EPOCH + timedelta(minutes=offset_minutes),
        usage=usage if usage is not None else TokenUsage(input_tokens=90, output_tokens=10),
        finish_reason=finish_reason,
        response=response,
        keep_response=keep_response,
    )


@dataclass
class CallSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_chat: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[CallSubject]:
    """The SQL repository against a migrated database with a chat per account."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "ai_calls.db"))
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
        yield CallSubject(
            for_account=lambda account_id: SqlAiCallRepository(uow, account_id),
            delete_chat=delete_chat,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> CallSubject:
    """The in-memory repository against a shared store with a chat per account."""
    store = InMemoryAiCallStore(
        known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
        chats={int(CHAT_A): int(ACCOUNT_A), int(CHAT_B): int(ACCOUNT_B)},
    )

    async def delete_chat(chat_id: ChatId) -> None:
        store.delete_chat(chat_id)

    return CallSubject(
        for_account=lambda account_id: InMemoryAiCallRepository(store, account_id),
        delete_chat=delete_chat,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> CallSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: CallSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: CallSubject, account_id: AccountId) -> AiCallRepository:
    """Build a repository scoped to an account."""
    built: AiCallRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestAiCallRepositoryContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: CallSubject) -> None:
        assert isinstance(repo(subject, ACCOUNT_A), AiCallRepository)

    def test_exposes_its_scope(self, subject: CallSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    def test_offers_no_way_to_change_a_call(self, subject: CallSubject) -> None:
        # Append-only expressed in the interface rather than in a convention:
        # an audit record nothing can edit needs no rule saying not to.
        calls = repo(subject, ACCOUNT_A)

        assert not hasattr(calls, "update")
        assert not hasattr(calls, "delete")

    async def test_an_absent_call_returns_none(self, subject: CallSubject) -> None:
        assert await repo(subject, ACCOUNT_A).get(AiCallId(1)) is None

    async def test_a_stored_call_reads_back_exactly(self, subject: CallSubject) -> None:
        # Everything except the price list the cost was computed from -- see
        # test_the_rates_are_not_stored below.
        calls = repo(subject, ACCOUNT_A)
        recorded = make_call(1, ACCOUNT_A, chat_id=CHAT_A, model=UNPRICED)

        await calls.add(recorded)

        assert await calls.get(recorded.id) == recorded

    async def test_reads_are_snapshots_not_live_views(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A))

        first = await calls.get(AiCallId(1))
        second = await calls.get(AiCallId(1))

        assert first == second
        assert first is not second

    async def test_a_second_call_with_one_identifier_is_refused(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await calls.add(make_call(1, ACCOUNT_A, offset_minutes=5))


class TestWhatIsStored:
    """The fields this table exists to answer questions about."""

    async def test_a_cost_survives_exactly(self, subject: CallSubject) -> None:
        # Text, not a float. Fractions of a cent summed over months are
        # precisely where binary floating point drifts, and a spend report that
        # drifts is worse than none.
        calls = repo(subject, ACCOUNT_A)
        recorded = make_call(1, ACCOUNT_A, usage=TokenUsage(input_tokens=333_333, output_tokens=1))

        await calls.add(recorded)

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.cost == recorded.cost
        assert found.cost is not None
        assert found.cost.amount == recorded.cost.amount  # type: ignore[union-attr]

    async def test_an_unknown_cost_stays_unknown(self, subject: CallSubject) -> None:
        # Not zero: a local model's call was not free, its price is simply not
        # something this application knows.
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A, model=UNPRICED))

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.cost is None

    async def test_unreported_tokens_stay_unreported(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A, usage=TokenUsage()))

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.usage.input_tokens is None
        assert found.usage.total is None

    async def test_a_digest_survives_without_the_text(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A, response="Paris"))

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.response_digest == digest_of("Paris")
        assert found.response_text is None

    async def test_a_kept_response_survives(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A, response="Paris", keep_response=True))

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.response_text == "Paris"

    async def test_a_failure_survives_as_a_failure(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        await calls.add(
            make_call(
                1,
                ACCOUNT_A,
                outcome=AiOutcome.TIMEOUT,
                finish_reason=None,
                usage=TokenUsage(),
                response=None,
            )
        )

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.outcome is AiOutcome.TIMEOUT
        assert found.finish_reason is None
        assert found.response_digest is None

    async def test_the_model_that_answered_survives(self, subject: CallSubject) -> None:
        # Verbatim, because an expensive call has to be traceable to the exact
        # model that made it, and a provider may answer as a revision other
        # than the one that was asked for.
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A))

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.model.vendor is PRICED.vendor
        assert found.model.identifier == PRICED.identifier
        assert found.model.data_boundary is PRICED.data_boundary

    async def test_the_rates_are_not_stored(self, subject: CallSubject) -> None:
        # The cost is stored, so the rates that produced it have no reader, and
        # a column with no reader is one nobody keeps correct. Repricing the
        # vendor's catalogue still cannot change a past call: what it cost was
        # written down at the time.
        calls = repo(subject, ACCOUNT_A)
        recorded = make_call(1, ACCOUNT_A)

        await calls.add(recorded)

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.model.input_cost_per_million is None
        assert found.cost == recorded.cost

    async def test_a_call_about_nothing_stores_no_chat(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A, chat_id=None))

        found = await calls.get(AiCallId(1))
        assert found is not None
        assert found.chat_id is None


class TestOwnership:
    """Scope, the composite foreign key, and what outlives a chat."""

    async def test_another_accounts_call_cannot_be_added(self, subject: CallSubject) -> None:
        with pytest.raises(DomainValidationError):
            await repo(subject, ACCOUNT_A).add(make_call(1, ACCOUNT_B))

    async def test_another_accounts_call_is_invisible(self, subject: CallSubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_call(1, ACCOUNT_B, chat_id=CHAT_B))

        assert await repo(subject, ACCOUNT_A).get(AiCallId(1)) is None

    async def test_another_accounts_call_is_not_listed(self, subject: CallSubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_call(1, ACCOUNT_B, chat_id=CHAT_B))
        await repo(subject, ACCOUNT_A).add(make_call(2, ACCOUNT_A, chat_id=CHAT_A))

        page = await repo(subject, ACCOUNT_A).list_recent(PageRequest(limit=10))

        assert [int(call.id) for call in page.items] == [2]

    async def test_a_chat_from_another_account_is_refused(self, subject: CallSubject) -> None:
        # The composite key is what makes account_id and chat_id agreeing a
        # structural fact rather than a check somebody has to remember
        # (ADR-043).
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_call(1, ACCOUNT_A, chat_id=CHAT_B))

    async def test_a_chat_that_does_not_exist_is_refused(self, subject: CallSubject) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_call(1, ACCOUNT_A, chat_id=ABSENT_CHAT))

    async def test_deleting_a_chat_deletes_its_calls(self, subject: CallSubject) -> None:
        # A record derived from a chat the user deleted is residue of that chat,
        # and every other child of `chats` already cascades for that reason. The
        # cost of those calls leaves the spend history with them; that is the
        # deliberate price of not keeping residue.
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A, chat_id=CHAT_A))

        await subject.delete_chat(CHAT_A)  # type: ignore[operator]

        assert await calls.get(AiCallId(1)) is None


class TestListing:
    """Newest first, because that is the only order a cost report is read in."""

    async def test_calls_come_back_newest_first(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A, offset_minutes=0))
        await calls.add(make_call(2, ACCOUNT_A, offset_minutes=10))
        await calls.add(make_call(3, ACCOUNT_A, offset_minutes=5))

        page = await calls.list_recent(PageRequest(limit=10))

        assert [int(call.id) for call in page.items] == [2, 3, 1]

    async def test_calls_at_one_instant_have_a_stable_order(self, subject: CallSubject) -> None:
        # Two calls can share a timestamp; without a second key the page after
        # them could repeat or skip a row.
        calls = repo(subject, ACCOUNT_A)
        await calls.add(make_call(1, ACCOUNT_A))
        await calls.add(make_call(2, ACCOUNT_A))

        first = await calls.list_recent(PageRequest(limit=10))
        second = await calls.list_recent(PageRequest(limit=10))

        assert [int(c.id) for c in first.items] == [int(c.id) for c in second.items]

    async def test_a_page_reports_more_when_there_is_more(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        for index in range(3):
            await calls.add(make_call(index + 1, ACCOUNT_A, offset_minutes=index))

        page = await calls.list_recent(PageRequest(limit=2))

        assert len(page.items) == 2
        assert page.has_more

    async def test_the_second_page_continues_the_first(self, subject: CallSubject) -> None:
        calls = repo(subject, ACCOUNT_A)
        for index in range(3):
            await calls.add(make_call(index + 1, ACCOUNT_A, offset_minutes=index))
        first = await calls.list_recent(PageRequest(limit=2))

        second = await calls.list_recent(PageRequest(limit=2, cursor=first.next_cursor))

        assert [int(call.id) for call in second.items] == [1]

    async def test_an_empty_account_lists_nothing(self, subject: CallSubject) -> None:
        page = await repo(subject, ACCOUNT_A).list_recent(PageRequest(limit=10))

        assert not page.items
        assert not page.has_more
