"""The memory repository, run against both implementations.

What is asserted here is ownership, the composite key to ``contacts``, soft
deletion, and the three unique indexes that carry this slice's guarantees:

* one memory per accepted proposal, so "acceptance creates exactly one memory"
  is a constraint rather than a rule;
* one live fact per person, so accepting the same fact twice is impossible;
* the same, for facts from conversations with no single counterpart -- which
  needs its own index, because SQL treats NULLs as distinct.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.memory_repository import InMemoryMemoryRepository, InMemoryMemoryStore
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
    MemoryId,
    MemoryProposalId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.memory import (
    Confidence,
    Evidence,
    Importance,
    Memory,
    MemoryCategory,
    MemoryKey,
    MemoryProposal,
    MemorySource,
)
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_repository import MemoryRepository
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
    SqlMemoryRepository,
)

EPOCH = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
LATER = EPOCH + timedelta(days=1)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CONTACT_A = ContactId(101)
CONTACT_A2 = ContactId(103)
CONTACT_B = ContactId(102)
ABSENT_CONTACT = ContactId(999)

CHAT_A = ChatId(11)
CHAT_B = ChatId(22)
CONVERSATION_A = ConversationId(301)
CONVERSATION_B = ConversationId(302)
CALL_A = AiCallId(401)
CALL_B = AiCallId(402)

#: Proposals for memories to have come from. One per memory a test builds, so
#: the unique index on ``proposal_id`` is exercised by the tests that mean to
#: and never tripped by the ones that do not.
PROPOSALS_A = tuple(range(501, 511))
PROPOSALS_B = (601,)

PROMPT = PromptVersion(prompt_id="memory_extract", version="1.0.0")

MODEL = AiModel(
    vendor=AiVendor.FAKE,
    identifier="fake-local-1",
    data_boundary=DataBoundary.LOCAL,
)


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own a memory."""
    return Account.create(
        account_id=account_id,
        telegram_user_id=TelegramUserId(1000 + int(account_id)),
        display_name=f"account-{int(account_id)}",
        now=EPOCH,
        is_active=is_active,
    )


def make_contact(contact_id: ContactId, account_id: AccountId) -> Contact:
    """Build a contact a memory can be about."""
    return Contact.create(
        contact_id=contact_id,
        account_id=account_id,
        telegram_user_id=TelegramUserId(2000 + int(contact_id)),
        display_name=f"person-{int(contact_id)}",
        now=EPOCH,
    )


def make_chat(chat_id: ChatId, account_id: AccountId, contact_id: ContactId) -> Chat:
    """Build the private chat a conversation belongs to."""
    return Chat.private_with(
        chat_id=chat_id,
        account_id=account_id,
        telegram_chat_id=TelegramChatId(5000 + int(chat_id)),
        contact_id=contact_id,
        now=EPOCH,
    )


def make_conversation(
    conversation_id: ConversationId, chat_id: ChatId, account_id: AccountId
) -> Conversation:
    """Build the conversation a fact was read from."""
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
    """Build the recorded call a memory's provenance points at."""
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


def make_proposal(
    proposal_id: int, account_id: AccountId, conversation_id: ConversationId, call_id: AiCallId
) -> MemoryProposal:
    """Build the proposal a memory came from."""
    return MemoryProposal.propose(
        proposal_id=MemoryProposalId(proposal_id),
        account_id=account_id,
        conversation_id=conversation_id,
        ai_call_id=call_id,
        category=MemoryCategory.OTHER,
        value=f"proposal {proposal_id}",
        confidence=Confidence(0.9),
        evidence=Evidence("something somebody said"),
        prompt=PROMPT,
        now=EPOCH,
    )


def make_memory(  # noqa: PLR0913 - one argument per field a test varies
    memory_id: int,
    account_id: AccountId,
    *,
    contact_id: ContactId | None = CONTACT_A,
    category: MemoryCategory = MemoryCategory.LOCATION,
    value: str = "Lives in Lisbon",
    confidence: float = 0.9,
    source: MemorySource = MemorySource.AI_APPROVED,
    proposal_id: int | None = None,
    offset_minutes: int = 0,
    importance: float = 0.5,
    deleted: bool = False,
) -> Memory:
    """Build a memory.

    Provenance is derived from the identifier unless a test names it, so two
    memories never accidentally claim one proposal -- the unique index that
    guarantees "one memory per accepted proposal" is then exercised only by the
    tests that mean to exercise it.

    A ``USER`` memory has no provenance at all, which is not an omission: a
    person who typed a fact *is* its provenance (``DOMAIN_MODEL.md`` §5.9).
    """
    if source is MemorySource.USER:
        proposal, conversation, call = None, None, None
    else:
        pool = PROPOSALS_B if account_id == ACCOUNT_B else PROPOSALS_A
        chosen = proposal_id if proposal_id is not None else pool[memory_id % len(pool)]
        proposal = MemoryProposalId(chosen)
        conversation = CONVERSATION_B if account_id == ACCOUNT_B else CONVERSATION_A
        call = CALL_B if account_id == ACCOUNT_B else CALL_A

    return Memory(
        id=MemoryId(memory_id),
        account_id=account_id,
        contact_id=contact_id,
        category=category,
        key=MemoryKey.of(value),
        value=value,
        confidence=Confidence(confidence),
        source=source,
        proposal_id=proposal,
        conversation_id=conversation,
        ai_call_id=call,
        created_at=EPOCH + timedelta(minutes=offset_minutes),
        importance=Importance(importance),
        deleted_at=LATER if deleted else None,
    )


@dataclass
class MemorySubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_contact: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[MemorySubject]:
    """The SQL repository against a migrated database and a whole world.

    Provenance columns are real foreign keys, so a memory can only be stored if
    the proposal, conversation and AI call it cites exist. Building them here
    rather than storing memories without provenance is what lets the entity keep
    its rule that anything a model touched must be traceable.
    """
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "memories.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    accounts = SqlAccountRepository(uow)
    await accounts.add(make_account(ACCOUNT_A, is_active=True))
    await accounts.add(make_account(ACCOUNT_B))
    await SqlContactRepository(uow, ACCOUNT_A).add(make_contact(CONTACT_A, ACCOUNT_A))
    await SqlContactRepository(uow, ACCOUNT_A).add(make_contact(CONTACT_A2, ACCOUNT_A))
    await SqlContactRepository(uow, ACCOUNT_B).add(make_contact(CONTACT_B, ACCOUNT_B))

    for chat_id, contact_id, conversation_id, call_id, account_id, pool in (
        (CHAT_A, CONTACT_A, CONVERSATION_A, CALL_A, ACCOUNT_A, PROPOSALS_A),
        (CHAT_B, CONTACT_B, CONVERSATION_B, CALL_B, ACCOUNT_B, PROPOSALS_B),
    ):
        await SqlChatRepository(uow, account_id).add(make_chat(chat_id, account_id, contact_id))
        await SqlConversationRepository(uow, account_id).add(
            make_conversation(conversation_id, chat_id, account_id)
        )
        await SqlAiCallRepository(uow, account_id).add(make_call(call_id, account_id, chat_id))
        proposals = SqlMemoryProposalRepository(uow, account_id)
        for proposal_id in pool:
            await proposals.add(make_proposal(proposal_id, account_id, conversation_id, call_id))

    async def delete_contact(contact_id: ContactId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM contacts WHERE id = :id"), {"id": int(contact_id)}
            )
        )

    try:
        yield MemorySubject(
            for_account=lambda account_id: SqlMemoryRepository(uow, account_id),
            delete_contact=delete_contact,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> MemorySubject:
    """The in-memory repository against a shared store with the same world."""
    store = InMemoryMemoryStore(
        known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
        contacts={
            int(CONTACT_A): int(ACCOUNT_A),
            int(CONTACT_A2): int(ACCOUNT_A),
            int(CONTACT_B): int(ACCOUNT_B),
        },
    )

    async def delete_contact(contact_id: ContactId) -> None:
        store.delete_contact(int(contact_id))

    return MemorySubject(
        for_account=lambda account_id: InMemoryMemoryRepository(store, account_id),
        delete_contact=delete_contact,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> MemorySubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: MemorySubject = request.getfixturevalue(name)
    return resolved


def repo(subject: MemorySubject, account_id: AccountId) -> MemoryRepository:
    """Build a repository scoped to an account."""
    built: MemoryRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestMemoryRepositoryContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: MemorySubject) -> None:
        assert isinstance(repo(subject, ACCOUNT_A), MemoryRepository)

    def test_exposes_its_scope(self, subject: MemorySubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    def test_offers_no_way_to_change_a_memory(self, subject: MemorySubject) -> None:
        # A memory is immutable. Correcting one means deleting it and accepting
        # a new proposal, so that an edit cannot keep the provenance while
        # changing the fact (ADR-059).
        assert not hasattr(repo(subject, ACCOUNT_A), "update")

    async def test_an_absent_memory_returns_none(self, subject: MemorySubject) -> None:
        assert await repo(subject, ACCOUNT_A).get(MemoryId(1)) is None

    async def test_a_stored_memory_reads_back_exactly(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        memory = make_memory(1, ACCOUNT_A)

        await memories.add(memory)

        assert await memories.get(memory.id) == memory

    async def test_reads_are_snapshots_not_live_views(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))

        first = await memories.get(MemoryId(1))
        second = await memories.get(MemoryId(1))

        assert first == second
        assert first is not second

    async def test_a_second_memory_with_one_identifier_is_refused(
        self, subject: MemorySubject
    ) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await memories.add(make_memory(1, ACCOUNT_A, value="Something else"))

    async def test_the_key_survives(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, value="Lives in Lisbon."))

        found = await memories.get(MemoryId(1))
        assert found is not None
        assert found.key == MemoryKey("lives in lisbon")
        assert found.value == "Lives in Lisbon."

    @pytest.mark.parametrize("source", list(MemorySource))
    async def test_every_source_can_be_stored(
        self, subject: MemorySubject, source: MemorySource
    ) -> None:
        # The enumeration and the check constraint have to agree, and the only
        # way to know they do is to write each one.
        memories = repo(subject, ACCOUNT_A)
        provenance = None if source is MemorySource.USER else 501

        await memories.add(make_memory(1, ACCOUNT_A, source=source, proposal_id=provenance))

        found = await memories.get(MemoryId(1))
        assert found is not None
        assert found.source is source


class TestOnePerProposal:
    """Acceptance creates exactly one memory, and the index says so."""

    async def test_a_second_memory_for_one_proposal_is_refused(
        self, subject: MemorySubject
    ) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, proposal_id=PROPOSALS_A[0]))

        with pytest.raises(ConstraintViolationError):
            await memories.add(
                make_memory(2, ACCOUNT_A, value="Something else", proposal_id=PROPOSALS_A[0])
            )

    async def test_a_memory_can_be_found_by_its_proposal(self, subject: MemorySubject) -> None:
        # What lets an already-accepted proposal be answered with the memory it
        # produced rather than a bare refusal.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, proposal_id=PROPOSALS_A[0]))

        found = await memories.get_by_proposal(MemoryProposalId(PROPOSALS_A[0]))

        assert found is not None
        assert int(found.id) == 1

    async def test_an_unaccepted_proposal_has_no_memory(self, subject: MemorySubject) -> None:
        assert (
            await repo(subject, ACCOUNT_A).get_by_proposal(MemoryProposalId(PROPOSALS_A[0])) is None
        )

    async def test_several_memories_without_provenance_are_permitted(
        self, subject: MemorySubject
    ) -> None:
        # The index is partial, on ``proposal_id IS NOT NULL``. A memory whose
        # proposal was deleted keeps existing, and several such rows must remain
        # permitted -- as must the memories a person typed themselves, once
        # there is a way to type one.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, source=MemorySource.USER))

        await memories.add(
            make_memory(2, ACCOUNT_A, value="Works at a studio", source=MemorySource.USER)
        )

        assert await memories.get(MemoryId(2)) is not None


class TestOneFactPerPerson:
    """The uniqueness that makes accepting the same fact twice impossible."""

    async def test_the_same_fact_about_one_person_twice_is_refused(
        self, subject: MemorySubject
    ) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await memories.add(make_memory(2, ACCOUNT_A))

    async def test_differing_only_in_punctuation_is_the_same_fact(
        self, subject: MemorySubject
    ) -> None:
        # The whole reason the key is normalised rather than the raw value.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, value="Lives in Lisbon"))

        with pytest.raises(ConstraintViolationError):
            await memories.add(make_memory(2, ACCOUNT_A, value="lives in  Lisbon!"))

    async def test_the_same_fact_about_two_people_is_permitted(
        self, subject: MemorySubject
    ) -> None:
        # Two contacts can both live in Lisbon. Uniqueness that ignored the
        # person would refuse the second as a duplicate.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, contact_id=CONTACT_A))

        await memories.add(make_memory(2, ACCOUNT_A, contact_id=CONTACT_A2))

        assert await memories.get(MemoryId(2)) is not None

    async def test_a_different_category_is_a_different_fact(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))

        await memories.add(make_memory(2, ACCOUNT_A, category=MemoryCategory.PLAN))

        assert await memories.get(MemoryId(2)) is not None

    async def test_a_contradiction_is_not_a_duplicate(self, subject: MemorySubject) -> None:
        # The limitation, asserted so it is a decision rather than a surprise:
        # the key deduplicates, it does not detect a contradiction. Both are
        # stored, and resolving them is conflict detection (ADR-059).
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, value="Lives in Lisbon"))

        await memories.add(make_memory(2, ACCOUNT_A, value="Lives in Porto"))

        page = await memories.list_active(PageRequest(limit=10))
        assert len(page.items) == 2

    async def test_the_same_fact_from_two_group_conversations_is_refused(
        self, subject: MemorySubject
    ) -> None:
        # Needs its own index: SQL treats NULLs as distinct, so without it two
        # identical contactless facts would both be stored.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, contact_id=None))

        with pytest.raises(ConstraintViolationError):
            await memories.add(make_memory(2, ACCOUNT_A, contact_id=None))

    async def test_the_same_fact_in_another_account_is_permitted(
        self, subject: MemorySubject
    ) -> None:
        await repo(subject, ACCOUNT_A).add(make_memory(1, ACCOUNT_A))

        await repo(subject, ACCOUNT_B).add(make_memory(2, ACCOUNT_B, contact_id=CONTACT_B))

        assert await repo(subject, ACCOUNT_B).get(MemoryId(2)) is not None


class TestSoftDeletion:
    """Forgetting, and what it frees."""

    async def test_deleting_hides_a_memory_from_the_listing(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))

        assert await memories.delete(MemoryId(1), LATER)

        assert not (await memories.list_active(PageRequest(limit=10))).items

    async def test_but_it_can_still_be_looked_up(self, subject: MemorySubject) -> None:
        # "Show me what you deleted" is a question a person is entitled to ask
        # of their own data.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))
        await memories.delete(MemoryId(1), LATER)

        found = await memories.get(MemoryId(1))

        assert found is not None
        assert not found.is_active
        assert found.deleted_at == LATER

    async def test_deleting_twice_changes_nothing(self, subject: MemorySubject) -> None:
        # The moment a fact was forgotten is the first one, and forgetting twice
        # is not an error.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))
        await memories.delete(MemoryId(1), LATER)

        assert not await memories.delete(MemoryId(1), LATER + timedelta(days=1))

        found = await memories.get(MemoryId(1))
        assert found is not None
        assert found.deleted_at == LATER

    async def test_deleting_an_absent_memory_is_false(self, subject: MemorySubject) -> None:
        assert not await repo(subject, ACCOUNT_A).delete(MemoryId(999), LATER)

    async def test_another_accounts_memory_cannot_be_deleted(self, subject: MemorySubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_memory(1, ACCOUNT_B, contact_id=CONTACT_B))

        assert not await repo(subject, ACCOUNT_A).delete(MemoryId(1), LATER)

    async def test_deleting_frees_the_fact_to_be_remembered_again(
        self, subject: MemorySubject
    ) -> None:
        # The only route to a correction, since nothing edits a memory.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))
        await memories.delete(MemoryId(1), LATER)

        await memories.add(make_memory(2, ACCOUNT_A))

        assert len((await memories.list_active(PageRequest(limit=10))).items) == 1


class TestOwnership:
    """Scope, the composite key, and what happens to a purged contact."""

    async def test_another_accounts_memory_cannot_be_added(self, subject: MemorySubject) -> None:
        with pytest.raises(DomainValidationError):
            await repo(subject, ACCOUNT_A).add(make_memory(1, ACCOUNT_B, contact_id=CONTACT_B))

    async def test_another_accounts_memory_is_invisible(self, subject: MemorySubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_memory(1, ACCOUNT_B, contact_id=CONTACT_B))

        assert await repo(subject, ACCOUNT_A).get(MemoryId(1)) is None

    async def test_another_accounts_contact_is_refused(self, subject: MemorySubject) -> None:
        # The composite key is what makes a memory about another account's
        # contact structurally impossible (ADR-043).
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_memory(1, ACCOUNT_A, contact_id=CONTACT_B))

    async def test_a_contact_that_does_not_exist_is_refused(self, subject: MemorySubject) -> None:
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ACCOUNT_A).add(make_memory(1, ACCOUNT_A, contact_id=ABSENT_CONTACT))

    async def test_purging_a_contact_removes_what_is_known_about_them(
        self, subject: MemorySubject
    ) -> None:
        # PRIVACY.md section 7: a contact purge removes everything referencing
        # them. A memory about a deleted person is exactly the residue that
        # commitment exists to prevent.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))

        await subject.delete_contact(CONTACT_A)  # type: ignore[operator]

        assert await memories.get(MemoryId(1)) is None


class TestListing:
    """Newest first, and live only."""

    async def test_memories_come_back_newest_first(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, value="one"))
        await memories.add(make_memory(2, ACCOUNT_A, value="two", offset_minutes=10))
        await memories.add(make_memory(3, ACCOUNT_A, value="three", offset_minutes=5))

        page = await memories.list_active(PageRequest(limit=10))

        assert [int(m.id) for m in page.items] == [2, 3, 1]

    async def test_another_accounts_memories_are_not_listed(self, subject: MemorySubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_memory(1, ACCOUNT_B, contact_id=CONTACT_B))
        await repo(subject, ACCOUNT_A).add(make_memory(2, ACCOUNT_A))

        page = await repo(subject, ACCOUNT_A).list_active(PageRequest(limit=10))

        assert [int(m.id) for m in page.items] == [2]

    async def test_a_second_page_continues_the_first(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        for index in range(3):
            await memories.add(
                make_memory(
                    index + 1,
                    ACCOUNT_A,
                    value=f"fact {index}",
                    offset_minutes=index,
                )
            )
        first = await memories.list_active(PageRequest(limit=2))

        second = await memories.list_active(PageRequest(limit=2, cursor=first.next_cursor))

        assert [int(m.id) for m in second.items] == [1]

    async def test_an_empty_account_lists_nothing(self, subject: MemorySubject) -> None:
        page = await repo(subject, ACCOUNT_A).list_active(PageRequest(limit=10))

        assert not page.items


class TestRetrievalReads:
    """``list_for_contact``: the query retrieval actually issues."""

    async def test_it_returns_that_contact_s_memories(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, contact_id=CONTACT_A))

        found = await memories.list_for_contact(CONTACT_A, limit=10)

        assert [int(m.id) for m in found] == [1]

    async def test_it_never_crosses_contacts(self, subject: MemorySubject) -> None:
        # The rule the whole feature rests on: a memory about one person cannot
        # reach a conversation with another.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, contact_id=CONTACT_A))
        await memories.add(make_memory(2, ACCOUNT_A, contact_id=CONTACT_A2))

        found = await memories.list_for_contact(CONTACT_A, limit=10)

        assert [int(m.id) for m in found] == [1]

    async def test_a_contactless_read_returns_only_contactless_memories(
        self, subject: MemorySubject
    ) -> None:
        # A group chat sees facts about nobody in particular -- not everybody's.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, contact_id=CONTACT_A))
        await memories.add(make_memory(2, ACCOUNT_A, contact_id=None))

        found = await memories.list_for_contact(None, limit=10)

        assert [int(m.id) for m in found] == [2]

    async def test_and_a_named_read_excludes_the_contactless_ones(
        self, subject: MemorySubject
    ) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, contact_id=None))

        assert await memories.list_for_contact(CONTACT_A, limit=10) == ()

    async def test_another_accounts_memories_are_invisible(self, subject: MemorySubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_memory(1, ACCOUNT_B, contact_id=CONTACT_B))

        assert await repo(subject, ACCOUNT_A).list_for_contact(CONTACT_B, limit=10) == ()

    async def test_forgotten_memories_are_ignored(self, subject: MemorySubject) -> None:
        # Something the user told the application to forget is not something it
        # knows, so it cannot reach a prompt.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))
        await memories.delete(MemoryId(1), LATER)

        assert await memories.list_for_contact(CONTACT_A, limit=10) == ()

    async def test_it_returns_newest_first(self, subject: MemorySubject) -> None:
        # The order a candidate cap takes from when it bites. Ranking reorders
        # afterwards, so this decides *which* memories survive truncation.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, value="one", offset_minutes=0))
        await memories.add(make_memory(2, ACCOUNT_A, value="two", offset_minutes=10))
        await memories.add(make_memory(3, ACCOUNT_A, value="three", offset_minutes=5))

        found = await memories.list_for_contact(CONTACT_A, limit=10)

        assert [int(m.id) for m in found] == [2, 3, 1]

    async def test_the_limit_is_honoured(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        for index in range(1, 4):
            await memories.add(
                make_memory(index, ACCOUNT_A, value=f"fact {index}", offset_minutes=index)
            )

        assert len(await memories.list_for_contact(CONTACT_A, limit=2)) == 2

    async def test_an_unknown_contact_returns_nothing(self, subject: MemorySubject) -> None:
        assert await repo(subject, ACCOUNT_A).list_for_contact(ABSENT_CONTACT, limit=10) == ()


class TestRetrievalAccounting:
    """``mark_retrieved``: the one write retrieval performs."""

    async def test_it_counts_a_retrieval(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))

        assert await memories.mark_retrieved([MemoryId(1)], LATER) == 1

        found = await memories.get(MemoryId(1))
        assert found is not None
        assert found.retrieval_count == 1
        assert found.last_retrieved_at == LATER

    async def test_counting_accumulates(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))
        await memories.mark_retrieved([MemoryId(1)], LATER)

        await memories.mark_retrieved([MemoryId(1)], LATER + timedelta(days=1))

        found = await memories.get(MemoryId(1))
        assert found is not None
        assert found.retrieval_count == 2
        assert found.last_retrieved_at == LATER + timedelta(days=1)

    async def test_it_marks_a_whole_selection_at_once(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        for index in range(1, 4):
            await memories.add(make_memory(index, ACCOUNT_A, value=f"fact {index}"))

        marked = await memories.mark_retrieved([MemoryId(1), MemoryId(2), MemoryId(3)], LATER)

        assert marked == 3

    async def test_marking_nothing_writes_nothing(self, subject: MemorySubject) -> None:
        assert await repo(subject, ACCOUNT_A).mark_retrieved([], LATER) == 0

    async def test_it_does_not_touch_what_was_not_selected(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, value="one"))
        await memories.add(make_memory(2, ACCOUNT_A, value="two"))

        await memories.mark_retrieved([MemoryId(1)], LATER)

        untouched = await memories.get(MemoryId(2))
        assert untouched is not None
        assert untouched.retrieval_count == 0
        assert untouched.last_retrieved_at is None

    async def test_another_accounts_memory_cannot_be_marked(self, subject: MemorySubject) -> None:
        await repo(subject, ACCOUNT_B).add(make_memory(1, ACCOUNT_B, contact_id=CONTACT_B))

        assert await repo(subject, ACCOUNT_A).mark_retrieved([MemoryId(1)], LATER) == 0

    async def test_a_forgotten_memory_cannot_be_marked(self, subject: MemorySubject) -> None:
        # It cannot be selected either, so this is belt and braces -- but a
        # count that moved on a deleted row would make the deletion look
        # ineffective in the one report that reads these numbers.
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))
        await memories.delete(MemoryId(1), LATER)

        assert await memories.mark_retrieved([MemoryId(1)], LATER) == 0

    async def test_an_absent_memory_is_not_an_error(self, subject: MemorySubject) -> None:
        # Something deleted between the read and the write is not a failure:
        # the context was still built from what was true when it was read.
        assert await repo(subject, ACCOUNT_A).mark_retrieved([MemoryId(999)], LATER) == 0

    async def test_the_history_survives_a_round_trip(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A))
        await memories.mark_retrieved([MemoryId(1)], LATER)

        found = await memories.get(MemoryId(1))
        assert found is not None
        assert found.was_retrieved

    async def test_importance_survives_a_round_trip(self, subject: MemorySubject) -> None:
        memories = repo(subject, ACCOUNT_A)
        await memories.add(make_memory(1, ACCOUNT_A, importance=0.75))

        found = await memories.get(MemoryId(1))
        assert found is not None
        assert found.importance == Importance(0.75)
        assert found.importance.label == "high"
