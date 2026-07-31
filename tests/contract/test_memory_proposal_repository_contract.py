"""The memory proposal repository, run against both implementations.

What is asserted here is the append-only shape, ownership, the two composite
foreign keys, what happens when a conversation or an AI call is deleted, scope
isolation, newest-first ordering, and the constraint the whole "re-running
extraction is free" property rests on: one fact per conversation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.memory_proposal_repository import (
    InMemoryMemoryProposalRepository,
    InMemoryMemoryProposalStore,
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
from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    ContactId,
    ConversationId,
    MemoryProposalId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.memory import (
    Confidence,
    Evidence,
    MemoryCategory,
    MemoryProposal,
    ProposalStatus,
)
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_proposal_repository import MemoryProposalRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAiCallRepository,
    SqlAlchemyUnitOfWork,
    SqlChatRepository,
    SqlContactRepository,
    SqlConversationRepository,
    SqliteDatabase,
    SqlMemoryProposalRepository,
)

EPOCH = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CHAT_A = ChatId(11)
CHAT_B = ChatId(22)
CONVERSATION_A = ConversationId(101)
CONVERSATION_B = ConversationId(202)
ABSENT_CONVERSATION = ConversationId(999)
CALL_A = AiCallId(1001)
CALL_B = AiCallId(2002)
ABSENT_CALL = AiCallId(9999)

PROMPT = PromptVersion(prompt_id="memory_extract", version="1.0.0")

#: When a decision is made in these tests. After the epoch, because a proposal
#: cannot be decided before it was made.
DECIDED_AT = EPOCH + timedelta(hours=2)

MODEL = AiModel(
    vendor=AiVendor.FAKE,
    identifier="fake-local-1",
    data_boundary=DataBoundary.LOCAL,
    input_cost_per_million=Decimal(0),
    output_cost_per_million=Decimal(0),
)


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own a proposal."""
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
    """Build a private chat to hold a conversation."""
    return Chat.private_with(
        chat_id=chat_id,
        account_id=account_id,
        telegram_chat_id=TelegramChatId(5000 + int(chat_id)),
        contact_id=ContactId(100 + int(account_id)),
        now=EPOCH,
    )


def make_conversation(
    conversation_id: ConversationId, chat_id: ChatId, account_id: AccountId
) -> Conversation:
    """Build a conversation for proposals to cite."""
    return Conversation.spanning(
        conversation_id=conversation_id,
        account_id=account_id,
        chat_id=chat_id,
        started_at=EPOCH,
        ended_at=EPOCH + timedelta(minutes=30),
        message_count=4,
        now=EPOCH,
    )


def make_call(call_id: AiCallId, account_id: AccountId, chat_id: ChatId) -> AiCall:
    """Build the recorded call a proposal's provenance points at."""
    return AiCall.record(
        call_id=call_id,
        account_id=account_id,
        chat_id=chat_id,
        model=MODEL,
        prompt=PROMPT,
        task_kind="extract_memories",
        outcome=AiOutcome.SUCCESS,
        latency_ms=42,
        now=EPOCH,
        usage=TokenUsage(input_tokens=100, output_tokens=20),
        finish_reason=FinishReason.STOP,
        response="{}",
    )


def make_proposal(  # noqa: PLR0913 - one argument per field a test varies
    proposal_id: int,
    account_id: AccountId,
    *,
    conversation_id: ConversationId = CONVERSATION_A,
    ai_call_id: AiCallId = CALL_A,
    category: MemoryCategory = MemoryCategory.LOCATION,
    value: str = "Lives in Lisbon",
    confidence: float = 0.9,
    evidence: str = "I moved to Lisbon last month",
    offset_minutes: int = 0,
) -> MemoryProposal:
    """Build a pending proposal."""
    return MemoryProposal.propose(
        proposal_id=MemoryProposalId(proposal_id),
        account_id=account_id,
        conversation_id=conversation_id,
        ai_call_id=ai_call_id,
        category=category,
        value=value,
        confidence=Confidence(confidence),
        evidence=Evidence(evidence),
        prompt=PROMPT,
        now=EPOCH + timedelta(minutes=offset_minutes),
    )


@dataclass
class ProposalSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_conversation: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[ProposalSubject]:
    """The SQL repository against a migrated database, one world per account."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "proposals.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    accounts = SqlAccountRepository(uow)
    await accounts.add(make_account(ACCOUNT_A, is_active=True))
    await accounts.add(make_account(ACCOUNT_B))
    for chat_id, conversation_id, call_id, account_id in (
        (CHAT_A, CONVERSATION_A, CALL_A, ACCOUNT_A),
        (CHAT_B, CONVERSATION_B, CALL_B, ACCOUNT_B),
    ):
        await SqlContactRepository(uow, account_id).add(make_contact(account_id))
        await SqlChatRepository(uow, account_id).add(make_chat(chat_id, account_id))
        await SqlConversationRepository(uow, account_id).add(
            make_conversation(conversation_id, chat_id, account_id)
        )
        await SqlAiCallRepository(uow, account_id).add(make_call(call_id, account_id, chat_id))

    async def delete_conversation(conversation_id: ConversationId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM conversations WHERE id = :id"), {"id": int(conversation_id)}
            )
        )

    try:
        yield ProposalSubject(
            for_account=lambda account_id: SqlMemoryProposalRepository(uow, account_id),
            delete_conversation=delete_conversation,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> ProposalSubject:
    """The in-memory repository against a shared store with the same world."""
    store = InMemoryMemoryProposalStore(
        known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
        conversations={int(CONVERSATION_A): int(ACCOUNT_A), int(CONVERSATION_B): int(ACCOUNT_B)},
        calls={int(CALL_A): int(ACCOUNT_A), int(CALL_B): int(ACCOUNT_B)},
    )

    async def delete_conversation(conversation_id: ConversationId) -> None:
        store.delete_conversation(int(conversation_id))

    return ProposalSubject(
        for_account=lambda account_id: InMemoryMemoryProposalRepository(store, account_id),
        delete_conversation=delete_conversation,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> ProposalSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: ProposalSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: ProposalSubject, account_id: AccountId) -> MemoryProposalRepository:
    """Build a repository scoped to an account."""
    built: MemoryProposalRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestMemoryProposalRepositoryContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: ProposalSubject) -> None:
        assert isinstance(repo(subject, ACCOUNT_A), MemoryProposalRepository)

    def test_exposes_its_scope(self, subject: ProposalSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    def test_offers_no_way_to_change_or_remove_a_proposal(self, subject: ProposalSubject) -> None:
        # With no update path, *pending* is the only state a stored proposal can
        # be in -- so "accepted and rejected are terminal" needs no rule to
        # enforce it (ADR-058).
        proposals = repo(subject, ACCOUNT_A)

        assert not hasattr(proposals, "update")
        assert not hasattr(proposals, "delete")

    async def test_an_absent_proposal_returns_none(self, subject: ProposalSubject) -> None:
        assert await repo(subject, ACCOUNT_A).get(MemoryProposalId(1)) is None

    async def test_a_stored_proposal_reads_back_exactly(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        proposal = make_proposal(1, ACCOUNT_A)

        await proposals.add(proposal)

        assert await proposals.get(proposal.id) == proposal

    async def test_reads_are_snapshots_not_live_views(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        first = await proposals.get(MemoryProposalId(1))
        second = await proposals.get(MemoryProposalId(1))

        assert first == second
        assert first is not second

    async def test_a_second_proposal_with_one_identifier_is_refused(
        self, subject: ProposalSubject
    ) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await proposals.add(make_proposal(1, ACCOUNT_A, value="Something else"))


class TestWhatIsStored:
    """The fields a review decision is made from."""

    async def test_a_proposal_arrives_pending(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.status is ProposalStatus.PENDING

    async def test_the_evidence_survives_verbatim(self, subject: ProposalSubject) -> None:
        # The whole of what makes accepting a proposal a decision rather than a
        # leap. A round trip that trimmed it would break the check.
        quote = "  I moved to Lisbon last month, for a job.  "
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A, evidence=quote))

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.evidence.quote == quote

    async def test_the_confidence_survives(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A, confidence=0.75))

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.confidence == Confidence(0.75)

    async def test_the_prompt_version_survives(self, subject: ProposalSubject) -> None:
        # Duplicated from the AI call deliberately: "which proposals came from
        # the prompt we changed last week" is asked of this table.
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.prompt == PROMPT

    async def test_the_provenance_survives(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.ai_call_id == CALL_A
        assert found.conversation_id == CONVERSATION_A

    @pytest.mark.parametrize("category", list(MemoryCategory))
    async def test_every_category_can_be_stored(
        self, subject: ProposalSubject, category: MemoryCategory
    ) -> None:
        # The enumeration and the check constraint have to agree, and the only
        # way to know they do is to write each one.
        proposals = repo(subject, ACCOUNT_A)

        await proposals.add(make_proposal(1, ACCOUNT_A, category=category))

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.category is category


class TestNoDuplicates:
    """One fact per conversation, which is what makes re-extraction free."""

    async def test_the_same_fact_twice_is_refused(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await proposals.add(make_proposal(2, ACCOUNT_A))

    async def test_the_same_value_in_a_different_category_is_permitted(
        self, subject: ProposalSubject
    ) -> None:
        # "Lisbon" as a location and as a plan are different claims.
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A, category=MemoryCategory.LOCATION))

        await proposals.add(make_proposal(2, ACCOUNT_A, category=MemoryCategory.PLAN))

        assert len(await proposals.list_for_conversation(CONVERSATION_A)) == 2

    async def test_the_same_fact_in_another_account_is_permitted(
        self, subject: ProposalSubject
    ) -> None:
        await repo(subject, ACCOUNT_A).add(make_proposal(1, ACCOUNT_A))

        await repo(subject, ACCOUNT_B).add(
            make_proposal(2, ACCOUNT_B, conversation_id=CONVERSATION_B, ai_call_id=CALL_B)
        )

        assert await repo(subject, ACCOUNT_B).get(MemoryProposalId(2)) is not None


class TestOwnership:
    """Scope, the composite foreign keys, and what outlives a conversation."""

    async def test_another_accounts_proposal_cannot_be_added(
        self, subject: ProposalSubject
    ) -> None:
        with pytest.raises(DomainValidationError):
            await repo(subject, ACCOUNT_A).add(
                make_proposal(1, ACCOUNT_B, conversation_id=CONVERSATION_B, ai_call_id=CALL_B)
            )

    async def test_another_accounts_proposal_is_invisible(self, subject: ProposalSubject) -> None:
        await repo(subject, ACCOUNT_B).add(
            make_proposal(1, ACCOUNT_B, conversation_id=CONVERSATION_B, ai_call_id=CALL_B)
        )

        assert await repo(subject, ACCOUNT_A).get(MemoryProposalId(1)) is None

    async def test_another_accounts_conversation_is_refused(self, subject: ProposalSubject) -> None:
        # The composite key is what makes a proposal citing another account's
        # conversation structurally impossible (ADR-043).
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(
                make_proposal(1, ACCOUNT_A, conversation_id=CONVERSATION_B)
            )

    async def test_another_accounts_ai_call_is_refused(self, subject: ProposalSubject) -> None:
        # Provenance that could point into another account's audit trail would
        # be worse than none.
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_proposal(1, ACCOUNT_A, ai_call_id=CALL_B))

    async def test_a_conversation_that_does_not_exist_is_refused(
        self, subject: ProposalSubject
    ) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(
                make_proposal(1, ACCOUNT_A, conversation_id=ABSENT_CONVERSATION)
            )

    async def test_an_ai_call_that_does_not_exist_is_refused(
        self, subject: ProposalSubject
    ) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_proposal(1, ACCOUNT_A, ai_call_id=ABSENT_CALL))

    async def test_deleting_a_conversation_deletes_its_proposals(
        self, subject: ProposalSubject
    ) -> None:
        # A claim about a conversation that no longer exists is residue of it.
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        await subject.delete_conversation(CONVERSATION_A)  # type: ignore[operator]

        assert await proposals.get(MemoryProposalId(1)) is None


class TestListing:
    """Newest first, because that is the order a review queue is read in."""

    async def test_proposals_come_back_newest_first(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A, value="one", offset_minutes=0))
        await proposals.add(make_proposal(2, ACCOUNT_A, value="two", offset_minutes=10))
        await proposals.add(make_proposal(3, ACCOUNT_A, value="three", offset_minutes=5))

        page = await proposals.list_recent(PageRequest(limit=10))

        assert [int(p.id) for p in page.items] == [2, 3, 1]

    async def test_another_accounts_proposals_are_not_listed(
        self, subject: ProposalSubject
    ) -> None:
        await repo(subject, ACCOUNT_B).add(
            make_proposal(1, ACCOUNT_B, conversation_id=CONVERSATION_B, ai_call_id=CALL_B)
        )
        await repo(subject, ACCOUNT_A).add(make_proposal(2, ACCOUNT_A))

        page = await repo(subject, ACCOUNT_A).list_recent(PageRequest(limit=10))

        assert [int(p.id) for p in page.items] == [2]

    async def test_a_second_page_continues_the_first(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        for index in range(3):
            await proposals.add(
                make_proposal(index + 1, ACCOUNT_A, value=f"fact {index}", offset_minutes=index)
            )
        first = await proposals.list_recent(PageRequest(limit=2))

        second = await proposals.list_recent(PageRequest(limit=2, cursor=first.next_cursor))

        assert [int(p.id) for p in second.items] == [1]

    async def test_an_empty_account_lists_nothing(self, subject: ProposalSubject) -> None:
        page = await repo(subject, ACCOUNT_A).list_recent(PageRequest(limit=10))

        assert not page.items


class TestListingForOneConversation:
    """The duplicate check's read."""

    async def test_it_returns_that_conversation_s_proposals(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        found = await proposals.list_for_conversation(CONVERSATION_A)

        assert [int(p.id) for p in found] == [1]

    async def test_it_returns_nothing_for_an_untouched_conversation(
        self, subject: ProposalSubject
    ) -> None:
        assert await repo(subject, ACCOUNT_A).list_for_conversation(CONVERSATION_A) == ()

    async def test_another_accounts_proposals_are_not_included(
        self, subject: ProposalSubject
    ) -> None:
        await repo(subject, ACCOUNT_B).add(
            make_proposal(1, ACCOUNT_B, conversation_id=CONVERSATION_B, ai_call_id=CALL_B)
        )

        assert await repo(subject, ACCOUNT_A).list_for_conversation(CONVERSATION_B) == ()

    async def test_the_order_is_stable(self, subject: ProposalSubject) -> None:
        # What lets a duplicate check produce the same answer twice.
        proposals = repo(subject, ACCOUNT_A)
        for index in range(3):
            await proposals.add(make_proposal(index + 1, ACCOUNT_A, value=f"fact {index}"))

        first = await proposals.list_for_conversation(CONVERSATION_A)
        second = await proposals.list_for_conversation(CONVERSATION_A)

        assert [int(p.id) for p in first] == [int(p.id) for p in second] == [1, 2, 3]


class TestDeciding:
    """The one mutation: pending to terminal, once (ADR-059)."""

    async def test_a_pending_proposal_can_be_decided(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        assert await proposals.decide(MemoryProposalId(1), ProposalStatus.ACCEPTED, DECIDED_AT)

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.status is ProposalStatus.ACCEPTED
        assert found.decided_at == DECIDED_AT

    async def test_rejecting_records_the_same_way(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))

        assert await proposals.decide(MemoryProposalId(1), ProposalStatus.REJECTED, DECIDED_AT)

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.status is ProposalStatus.REJECTED

    async def test_a_second_decision_changes_nothing(self, subject: ProposalSubject) -> None:
        # The guarantee that survives concurrency: the write names ``pending``
        # in its condition, so two decisions racing cannot both win.
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))
        await proposals.decide(MemoryProposalId(1), ProposalStatus.ACCEPTED, DECIDED_AT)

        assert not await proposals.decide(
            MemoryProposalId(1), ProposalStatus.REJECTED, DECIDED_AT + timedelta(days=1)
        )

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found.status is ProposalStatus.ACCEPTED
        assert found.decided_at == DECIDED_AT

    async def test_deciding_an_absent_proposal_is_false(self, subject: ProposalSubject) -> None:
        assert not await repo(subject, ACCOUNT_A).decide(
            MemoryProposalId(999), ProposalStatus.ACCEPTED, DECIDED_AT
        )

    async def test_another_accounts_proposal_cannot_be_decided(
        self, subject: ProposalSubject
    ) -> None:
        await repo(subject, ACCOUNT_B).add(
            make_proposal(1, ACCOUNT_B, conversation_id=CONVERSATION_B, ai_call_id=CALL_B)
        )

        assert not await repo(subject, ACCOUNT_A).decide(
            MemoryProposalId(1), ProposalStatus.ACCEPTED, DECIDED_AT
        )

    async def test_a_decided_proposal_is_still_listed(self, subject: ProposalSubject) -> None:
        # Kept rather than removed, so the extractor does not offer the same
        # fact again (DOMAIN_MODEL section 5.10).
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))
        await proposals.decide(MemoryProposalId(1), ProposalStatus.REJECTED, DECIDED_AT)

        assert len(await proposals.list_for_conversation(CONVERSATION_A)) == 1

    async def test_a_decided_proposal_round_trips(self, subject: ProposalSubject) -> None:
        proposals = repo(subject, ACCOUNT_A)
        await proposals.add(make_proposal(1, ACCOUNT_A))
        await proposals.decide(MemoryProposalId(1), ProposalStatus.ACCEPTED, DECIDED_AT)

        found = await proposals.get(MemoryProposalId(1))
        assert found is not None
        assert found == make_proposal(1, ACCOUNT_A).decided(ProposalStatus.ACCEPTED, DECIDED_AT)
