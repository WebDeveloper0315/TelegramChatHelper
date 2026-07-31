"""The suggestion repository, run against both implementations.

What is asserted here is the shape of a review queue: what is in it, what is not,
who can see it, and the single mutation that empties it one row at a time.

The obligation that matters most is the last: **exactly one decision**, enforced
by a conditional write rather than by a check, so two decisions racing cannot
both win (ADR-062).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.suggestion_repository import (
    InMemorySuggestionRepository,
    InMemorySuggestionStore,
)
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
)
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    ContactId,
    SuggestionId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.suggestion import ProposalType, Suggestion, SuggestionStatus
from tgassist.domain.ports.suggestion_repository import SuggestionRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAiCallRepository,
    SqlAlchemyUnitOfWork,
    SqlChatRepository,
    SqlContactRepository,
    SqliteDatabase,
    SqlSuggestionRepository,
)

EPOCH = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
DECIDED_AT = EPOCH + timedelta(hours=2)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CHAT_A = ChatId(11)
CHAT_A2 = ChatId(12)
CHAT_B = ChatId(22)
ABSENT_CHAT = ChatId(999)
CALL_A = AiCallId(401)
CALL_B = AiCallId(402)
ABSENT_CALL = AiCallId(999)

PROMPT = PromptVersion(prompt_id="chat_suggestion", version="1.0.0")

MODEL = AiModel(
    vendor=AiVendor.FAKE,
    identifier="fake-local-1",
    data_boundary=DataBoundary.LOCAL,
    input_cost_per_million=Decimal(0),
    output_cost_per_million=Decimal(0),
)


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own a suggestion."""
    return Account.create(
        account_id=account_id,
        telegram_user_id=TelegramUserId(1000 + int(account_id)),
        display_name=f"account-{int(account_id)}",
        now=EPOCH,
        is_active=is_active,
    )


def make_contact(contact_id: ContactId, account_id: AccountId) -> Contact:
    """Build the contact a private chat needs."""
    return Contact.create(
        contact_id=contact_id,
        account_id=account_id,
        telegram_user_id=TelegramUserId(2000 + int(contact_id)),
        display_name=f"person-{int(contact_id)}",
        now=EPOCH,
    )


def make_chat(chat_id: ChatId, account_id: AccountId, contact_id: ContactId) -> Chat:
    """Build a private chat for suggestions to be about."""
    return Chat.private_with(
        chat_id=chat_id,
        account_id=account_id,
        telegram_chat_id=TelegramChatId(5000 + int(chat_id)),
        contact_id=contact_id,
        now=EPOCH,
    )


def make_call(call_id: AiCallId, account_id: AccountId, chat_id: ChatId) -> AiCall:
    """Build the recorded call a suggestion's provenance points at."""
    return AiCall.record(
        call_id=call_id,
        account_id=account_id,
        chat_id=chat_id,
        model=MODEL,
        prompt=PROMPT,
        task_kind="suggest_reply",
        outcome=AiOutcome.SUCCESS,
        latency_ms=42,
        now=EPOCH,
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        finish_reason=FinishReason.STOP,
        response="{}",
    )


def make_suggestion(  # noqa: PLR0913 - one argument per field a test varies
    suggestion_id: int,
    account_id: AccountId,
    *,
    chat_id: ChatId = CHAT_A,
    ai_call_id: AiCallId = CALL_A,
    title: str = "Reply about the move",
    description: str = "Glad it feels like home. Want a few book ideas?",
    offset_minutes: int = 0,
) -> Suggestion:
    """Build a pending suggestion."""
    return Suggestion.draft(
        suggestion_id=SuggestionId(suggestion_id),
        account_id=account_id,
        chat_id=chat_id,
        ai_call_id=ai_call_id,
        proposal_type=ProposalType.REPLY_DRAFT,
        title=title,
        description=description,
        payload={"confidence": 0.8, "used_memory_keys": []},
        now=EPOCH + timedelta(minutes=offset_minutes),
    )


@dataclass
class SuggestionSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_chat: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[SuggestionSubject]:
    """The SQL repository against a migrated database, one world per account."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "suggestions.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    accounts = SqlAccountRepository(uow)
    await accounts.add(make_account(ACCOUNT_A, is_active=True))
    await accounts.add(make_account(ACCOUNT_B))

    for chat_id, contact_id, account_id in (
        (CHAT_A, ContactId(101), ACCOUNT_A),
        (CHAT_A2, ContactId(103), ACCOUNT_A),
        (CHAT_B, ContactId(102), ACCOUNT_B),
    ):
        await SqlContactRepository(uow, account_id).add(make_contact(contact_id, account_id))
        await SqlChatRepository(uow, account_id).add(make_chat(chat_id, account_id, contact_id))
    for call_id, account_id, chat_id in (
        (CALL_A, ACCOUNT_A, CHAT_A),
        (CALL_B, ACCOUNT_B, CHAT_B),
    ):
        await SqlAiCallRepository(uow, account_id).add(make_call(call_id, account_id, chat_id))

    async def delete_chat(chat_id: ChatId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM chats WHERE id = :id"), {"id": int(chat_id)}
            )
        )

    try:
        yield SuggestionSubject(
            for_account=lambda account_id: SqlSuggestionRepository(uow, account_id),
            delete_chat=delete_chat,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> SuggestionSubject:
    """The in-memory repository against a shared store with the same world."""
    store = InMemorySuggestionStore(
        known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
        chats={
            int(CHAT_A): int(ACCOUNT_A),
            int(CHAT_A2): int(ACCOUNT_A),
            int(CHAT_B): int(ACCOUNT_B),
        },
        calls={int(CALL_A): int(ACCOUNT_A), int(CALL_B): int(ACCOUNT_B)},
    )

    async def delete_chat(chat_id: ChatId) -> None:
        store.delete_chat(int(chat_id))

    return SuggestionSubject(
        for_account=lambda account_id: InMemorySuggestionRepository(store, account_id),
        delete_chat=delete_chat,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> SuggestionSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: SuggestionSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: SuggestionSubject, account_id: AccountId) -> SuggestionRepository:
    """Build a repository scoped to an account."""
    built: SuggestionRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestSuggestionRepositoryContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: SuggestionSubject) -> None:
        assert isinstance(repo(subject, ACCOUNT_A), SuggestionRepository)

    def test_exposes_its_scope(self, subject: SuggestionSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    def test_the_port_offers_exactly_one_mutation(self) -> None:
        # No update, no delete -- and no send, schedule or execute. Accepting a
        # suggestion records agreement, and the absence of an operation is how
        # that is guaranteed rather than a rule somebody keeps (ADR-062).
        #
        # Asserted against the *port* rather than an instance: the port is what
        # a caller may use, and the SQL base class carries query helpers that
        # are not domain operations.
        operations = {
            name
            for name in vars(SuggestionRepository)
            if not name.startswith("_") and name != "account_id"
        }

        assert operations == {"add", "get", "list_pending", "list_by_chat", "decide"}

    def test_neither_implementation_can_act_on_a_suggestion(
        self, subject: SuggestionSubject
    ) -> None:
        # Belt and braces on the same guarantee, in the shape a reader worries
        # about: nothing here sends, schedules or executes anything.
        suggestions = repo(subject, ACCOUNT_A)

        for forbidden in ("update", "delete", "send", "schedule", "run"):
            assert not hasattr(suggestions, forbidden)

    async def test_an_absent_suggestion_returns_none(self, subject: SuggestionSubject) -> None:
        assert await repo(subject, ACCOUNT_A).get(SuggestionId(1)) is None

    async def test_a_stored_suggestion_reads_back_exactly(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        suggestion = make_suggestion(1, ACCOUNT_A)

        await suggestions.add(suggestion)

        assert await suggestions.get(suggestion.id) == suggestion

    async def test_reads_are_snapshots_not_live_views(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))

        first = await suggestions.get(SuggestionId(1))
        second = await suggestions.get(SuggestionId(1))

        assert first == second
        assert first is not second

    async def test_a_second_suggestion_with_one_identifier_is_refused(
        self, subject: SuggestionSubject
    ) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await suggestions.add(make_suggestion(1, ACCOUNT_A, title="Something else"))

    async def test_a_suggestion_arrives_pending(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))

        found = await suggestions.get(SuggestionId(1))
        assert found is not None
        assert found.status is SuggestionStatus.PENDING
        assert found.decided_at is None

    async def test_the_payload_survives_verbatim(self, subject: SuggestionSubject) -> None:
        # Stored as it was serialised. A re-serialisation could produce
        # different bytes for the same suggestion.
        suggestions = repo(subject, ACCOUNT_A)
        suggestion = make_suggestion(1, ACCOUNT_A)
        await suggestions.add(suggestion)

        found = await suggestions.get(SuggestionId(1))
        assert found is not None
        assert found.payload == suggestion.payload
        assert found.details() == {"confidence": 0.8, "used_memory_keys": []}


class TestTheQueue:
    """``list_pending``: what is still awaiting a decision."""

    async def test_a_new_suggestion_is_in_the_queue(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))

        page = await suggestions.list_pending(PageRequest(limit=10))

        assert [int(s.id) for s in page.items] == [1]

    async def test_a_decided_one_leaves_it(self, subject: SuggestionSubject) -> None:
        # A queue is by definition what has not been decided.
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))
        await suggestions.decide(SuggestionId(1), SuggestionStatus.ACCEPTED, DECIDED_AT)

        page = await suggestions.list_pending(PageRequest(limit=10))

        assert not page.items

    async def test_a_dismissed_one_leaves_it_too(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))
        await suggestions.decide(SuggestionId(1), SuggestionStatus.DISMISSED, DECIDED_AT)

        assert not (await suggestions.list_pending(PageRequest(limit=10))).items

    async def test_the_queue_is_newest_first(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A, offset_minutes=0))
        await suggestions.add(make_suggestion(2, ACCOUNT_A, offset_minutes=10))
        await suggestions.add(make_suggestion(3, ACCOUNT_A, offset_minutes=5))

        page = await suggestions.list_pending(PageRequest(limit=10))

        assert [int(s.id) for s in page.items] == [2, 3, 1]

    async def test_another_accounts_queue_is_invisible(self, subject: SuggestionSubject) -> None:
        await repo(subject, ACCOUNT_B).add(
            make_suggestion(1, ACCOUNT_B, chat_id=CHAT_B, ai_call_id=CALL_B)
        )
        await repo(subject, ACCOUNT_A).add(make_suggestion(2, ACCOUNT_A))

        page = await repo(subject, ACCOUNT_A).list_pending(PageRequest(limit=10))

        assert [int(s.id) for s in page.items] == [2]

    async def test_a_second_page_continues_the_first(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        for index in range(3):
            await suggestions.add(make_suggestion(index + 1, ACCOUNT_A, offset_minutes=index))
        first = await suggestions.list_pending(PageRequest(limit=2))

        second = await suggestions.list_pending(PageRequest(limit=2, cursor=first.next_cursor))

        assert [int(s.id) for s in second.items] == [1]

    async def test_an_empty_account_has_an_empty_queue(self, subject: SuggestionSubject) -> None:
        assert not (await repo(subject, ACCOUNT_A).list_pending(PageRequest(limit=10))).items


class TestByChat:
    """``list_by_chat``: one conversation's history, decisions included."""

    async def test_it_returns_that_chat_s_suggestions(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A, chat_id=CHAT_A))
        await suggestions.add(make_suggestion(2, ACCOUNT_A, chat_id=CHAT_A2))

        page = await suggestions.list_by_chat(CHAT_A, PageRequest(limit=10))

        assert [int(s.id) for s in page.items] == [1]

    async def test_decided_suggestions_are_included(self, subject: SuggestionSubject) -> None:
        # A listing that hid the dismissals would make a generator that is
        # usually wrong look like one that is rarely used.
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))
        await suggestions.decide(SuggestionId(1), SuggestionStatus.DISMISSED, DECIDED_AT)

        page = await suggestions.list_by_chat(CHAT_A, PageRequest(limit=10))

        assert [int(s.id) for s in page.items] == [1]

    async def test_it_is_newest_first(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A, offset_minutes=0))
        await suggestions.add(make_suggestion(2, ACCOUNT_A, offset_minutes=10))

        page = await suggestions.list_by_chat(CHAT_A, PageRequest(limit=10))

        assert [int(s.id) for s in page.items] == [2, 1]

    async def test_another_accounts_chat_is_invisible(self, subject: SuggestionSubject) -> None:
        await repo(subject, ACCOUNT_B).add(
            make_suggestion(1, ACCOUNT_B, chat_id=CHAT_B, ai_call_id=CALL_B)
        )

        page = await repo(subject, ACCOUNT_A).list_by_chat(CHAT_B, PageRequest(limit=10))

        assert not page.items

    async def test_an_untouched_chat_has_nothing(self, subject: SuggestionSubject) -> None:
        assert not (
            await repo(subject, ACCOUNT_A).list_by_chat(CHAT_A2, PageRequest(limit=10))
        ).items


class TestDeciding:
    """The one mutation: pending to terminal, once."""

    async def test_a_pending_suggestion_can_be_accepted(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))

        assert await suggestions.decide(SuggestionId(1), SuggestionStatus.ACCEPTED, DECIDED_AT)

        found = await suggestions.get(SuggestionId(1))
        assert found is not None
        assert found.status is SuggestionStatus.ACCEPTED
        assert found.decided_at == DECIDED_AT

    async def test_and_dismissed(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))

        assert await suggestions.decide(SuggestionId(1), SuggestionStatus.DISMISSED, DECIDED_AT)

        found = await suggestions.get(SuggestionId(1))
        assert found is not None
        assert found.status is SuggestionStatus.DISMISSED

    async def test_a_second_decision_changes_nothing(self, subject: SuggestionSubject) -> None:
        # The guarantee that survives concurrency: the write names ``pending``
        # in its condition, so two decisions racing cannot both win.
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))
        await suggestions.decide(SuggestionId(1), SuggestionStatus.ACCEPTED, DECIDED_AT)

        assert not await suggestions.decide(
            SuggestionId(1), SuggestionStatus.DISMISSED, DECIDED_AT + timedelta(days=1)
        )

        found = await suggestions.get(SuggestionId(1))
        assert found is not None
        assert found.status is SuggestionStatus.ACCEPTED
        assert found.decided_at == DECIDED_AT

    async def test_deciding_an_absent_suggestion_is_false(self, subject: SuggestionSubject) -> None:
        assert not await repo(subject, ACCOUNT_A).decide(
            SuggestionId(999), SuggestionStatus.ACCEPTED, DECIDED_AT
        )

    async def test_another_accounts_suggestion_cannot_be_decided(
        self, subject: SuggestionSubject
    ) -> None:
        await repo(subject, ACCOUNT_B).add(
            make_suggestion(1, ACCOUNT_B, chat_id=CHAT_B, ai_call_id=CALL_B)
        )

        assert not await repo(subject, ACCOUNT_A).decide(
            SuggestionId(1), SuggestionStatus.ACCEPTED, DECIDED_AT
        )

    async def test_a_decided_suggestion_round_trips(self, subject: SuggestionSubject) -> None:
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))
        await suggestions.decide(SuggestionId(1), SuggestionStatus.ACCEPTED, DECIDED_AT)

        found = await suggestions.get(SuggestionId(1))

        assert found == make_suggestion(1, ACCOUNT_A).decided(SuggestionStatus.ACCEPTED, DECIDED_AT)


class TestOwnership:
    """Scope, the composite foreign keys, and what a deleted chat takes."""

    async def test_another_accounts_suggestion_cannot_be_added(
        self, subject: SuggestionSubject
    ) -> None:
        with pytest.raises(DomainValidationError):
            await repo(subject, ACCOUNT_A).add(
                make_suggestion(1, ACCOUNT_B, chat_id=CHAT_B, ai_call_id=CALL_B)
            )

    async def test_another_accounts_chat_is_refused(self, subject: SuggestionSubject) -> None:
        # The composite key makes a suggestion about another account's chat
        # structurally impossible (ADR-043).
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_suggestion(1, ACCOUNT_A, chat_id=CHAT_B))

    async def test_another_accounts_ai_call_is_refused(self, subject: SuggestionSubject) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_suggestion(1, ACCOUNT_A, ai_call_id=CALL_B))

    async def test_a_chat_that_does_not_exist_is_refused(self, subject: SuggestionSubject) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_suggestion(1, ACCOUNT_A, chat_id=ABSENT_CHAT))

    async def test_an_ai_call_that_does_not_exist_is_refused(
        self, subject: SuggestionSubject
    ) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(
                make_suggestion(1, ACCOUNT_A, ai_call_id=ABSENT_CALL)
            )

    async def test_deleting_a_chat_deletes_its_suggestions(
        self, subject: SuggestionSubject
    ) -> None:
        # Unlike a memory, which is approved knowledge that outlives its
        # origin, a suggestion is a draft *about* a conversation and means
        # nothing once that conversation is gone (ADR-062).
        suggestions = repo(subject, ACCOUNT_A)
        await suggestions.add(make_suggestion(1, ACCOUNT_A))

        await subject.delete_chat(CHAT_A)  # type: ignore[operator]

        assert await suggestions.get(SuggestionId(1)) is None
