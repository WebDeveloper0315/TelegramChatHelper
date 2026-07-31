"""Reviewing proposals, and what a decision creates.

The half of the lifecycle a person performs. Every test here is about one of
three things:

* **a decision is made once** -- accepted and rejected are terminal, and no
  route reopens either;
* **acceptance creates exactly one memory**, in the same transaction as the
  decision;
* **rejection creates none**, and keeps the proposal so the fact is not offered
  again.

No model is called anywhere in this file. Deciding needs no AI, which is the
point of having separated the two.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
from tests.fakes.event_bus import RecordingEventBus
from tests.fakes.memory_proposal_repository import (
    InMemoryMemoryProposalRepository,
    InMemoryMemoryProposalStore,
)
from tests.fakes.memory_repository import InMemoryMemoryRepository, InMemoryMemoryStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.memory_review import (
    AcceptMemoryProposal,
    DeleteMemory,
    GetMemory,
    ListMemories,
    RejectMemoryProposal,
)
from tgassist.application.use_cases.message import IncomingMessage
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    InvalidStateTransitionError,
    RecordNotFoundError,
)
from tgassist.domain.events import (
    MemoryCreated,
    MemoryProposalAccepted,
    MemoryProposalRejected,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.ai import (
    AiCall,
    AiOutcome,
    FinishReason,
    PromptVersion,
    TokenUsage,
)
from tgassist.domain.model.chat import AiProcessingMode, Chat, ChatType
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
    Memory,
    MemoryCategory,
    MemoryKey,
    MemoryProposal,
    MemorySource,
    ProposalStatus,
)
from tgassist.domain.model.message import MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_repository import MemoryRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.ai.scripted import LOCAL_MODEL
from tgassist.presentation.cli.app import app

runner = CliRunner()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CONTACT = ContactId(101)
GROUP_CONTACT = ContactId(102)
CHAT = ChatId(11)
GROUP_CHAT = ChatId(12)
CONVERSATION = ConversationId(201)
GROUP_CONVERSATION = ConversationId(202)
#: A second conversation in the same private chat. Proposals are unique per
#: conversation, so proposing the same fact twice needs two of them.
LATER_CONVERSATION = ConversationId(203)
CALL = AiCallId(301)

PROMPT = PromptVersion(prompt_id="memory_extract", version="1.0.0")


# ---------------------------------------------------------------------------
# The aggregates
# ---------------------------------------------------------------------------


def make_proposal(**overrides: Any) -> MemoryProposal:
    """Build a pending proposal."""
    values: dict[str, Any] = {
        "proposal_id": MemoryProposalId(1),
        "account_id": ACCOUNT_A,
        "conversation_id": CONVERSATION,
        "ai_call_id": CALL,
        "category": MemoryCategory.LOCATION,
        "value": "Lives in Lisbon",
        "confidence": Confidence(0.9),
        "evidence": Evidence("I moved to Lisbon last month"),
        "prompt": PROMPT,
        "now": NOW,
    }
    values.update(overrides)
    return MemoryProposal.propose(**values)


class TestTheDecision:
    """The one transition either aggregate has."""

    def test_a_pending_proposal_can_be_accepted(self) -> None:
        decided = make_proposal().decided(ProposalStatus.ACCEPTED, LATER)

        assert decided.status is ProposalStatus.ACCEPTED
        assert decided.decided_at == LATER

    def test_and_rejected(self) -> None:
        assert make_proposal().decided(ProposalStatus.REJECTED, LATER).status is (
            ProposalStatus.REJECTED
        )

    def test_deciding_twice_is_refused(self) -> None:
        decided = make_proposal().decided(ProposalStatus.ACCEPTED, LATER)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            decided.decided(ProposalStatus.REJECTED, LATER)

    def test_a_rejected_proposal_cannot_be_accepted(self) -> None:
        rejected = make_proposal().decided(ProposalStatus.REJECTED, LATER)

        with pytest.raises(InvalidStateTransitionError, match="already rejected"):
            rejected.decided(ProposalStatus.ACCEPTED, LATER)

    def test_there_is_no_way_back_to_pending(self) -> None:
        # No undo, expressed in the shape rather than in a rule: pending is not
        # a decision, so the only method that changes a status refuses it.
        with pytest.raises(InvalidStateTransitionError, match="not a decision"):
            make_proposal().decided(ProposalStatus.PENDING, LATER)

    def test_a_decision_cannot_precede_the_proposal(self) -> None:
        with pytest.raises(DomainValidationError, match="before it was made"):
            make_proposal().decided(ProposalStatus.ACCEPTED, NOW - timedelta(hours=1))

    def test_a_decided_proposal_must_say_when(self) -> None:
        # One fact, one representation: "has this been decided" has the same
        # answer whichever field is asked.
        from dataclasses import replace  # noqa: PLC0415 - local to this assertion

        with pytest.raises(DomainValidationError, match="records when it was decided"):
            replace(make_proposal(), status=ProposalStatus.ACCEPTED)

    def test_a_pending_one_must_not(self) -> None:
        from dataclasses import replace  # noqa: PLC0415

        with pytest.raises(DomainValidationError, match="has not been decided"):
            replace(make_proposal(), decided_at=LATER)


class TestTheMemory:
    """What acceptance produces."""

    def _approved(self, **overrides: Any) -> Memory:
        return Memory.approved(
            memory_id=MemoryId(500),
            proposal=make_proposal(**overrides.pop("proposal", {})),
            contact_id=overrides.pop("contact_id", CONTACT),
            now=overrides.pop("now", LATER),
        )

    def test_it_carries_the_fact_the_person_approved(self) -> None:
        memory = self._approved()

        assert memory.value == "Lives in Lisbon"
        assert memory.category is MemoryCategory.LOCATION

    def test_it_derives_its_own_key(self) -> None:
        # Never the model's. A key is an identity, and identity belongs to the
        # application (ADR-059).
        assert self._approved().key == MemoryKey("lives in lisbon")

    def test_it_keeps_the_confidence_as_reported(self) -> None:
        # A person accepting a fact says it is worth keeping, not that the
        # model was certain. Raising it to 1.0 would lose the second claim.
        assert self._approved().confidence == Confidence(0.9)

    def test_it_records_how_it_came_to_be_believed(self) -> None:
        assert self._approved().source is MemorySource.AI_APPROVED

    def test_it_carries_the_whole_provenance(self) -> None:
        memory = self._approved()

        assert memory.proposal_id == MemoryProposalId(1)
        assert memory.conversation_id == CONVERSATION
        assert memory.ai_call_id == CALL

    def test_it_is_created_when_it_was_accepted(self) -> None:
        # Not when it was proposed: this is the moment a person made it true.
        assert self._approved().created_at == LATER

    def test_it_starts_active(self) -> None:
        assert self._approved().is_active

    def test_it_has_no_edit_method(self) -> None:
        # Correcting a memory means deleting it and accepting a new proposal.
        # An edit in place would keep the provenance while changing the fact.
        changing = [
            name
            for name in dir(Memory)
            if not name.startswith("_") and callable(getattr(Memory, name, None))
        ]

        assert changing == ["approved"]

    def test_a_partial_trail_is_refused(self) -> None:
        # Provenance is all or nothing. A memory can lose its whole origin when
        # the chat it came from is deleted, but half a trail is a state nothing
        # can produce and nothing could interpret (ADR-059).
        from dataclasses import replace  # noqa: PLC0415

        with pytest.raises(DomainValidationError, match="complete or absent"):
            replace(self._approved(), ai_call_id=None)

    def test_a_memory_may_outlive_its_whole_trail(self) -> None:
        # What the chat-deletion cascade produces: user-approved knowledge that
        # does not stop being known because the exchange it came from was
        # removed.
        from dataclasses import replace  # noqa: PLC0415

        orphaned = replace(
            self._approved(), proposal_id=None, conversation_id=None, ai_call_id=None
        )

        assert orphaned.value == "Lives in Lisbon"
        assert orphaned.source is MemorySource.AI_APPROVED

    def test_a_typed_memory_needs_none(self) -> None:
        # A person who typed a fact is its provenance.
        typed = Memory(
            id=MemoryId(1),
            account_id=ACCOUNT_A,
            contact_id=CONTACT,
            category=MemoryCategory.OTHER,
            key=MemoryKey.of("Likes tea"),
            value="Likes tea",
            confidence=Confidence(1.0),
            source=MemorySource.USER,
            proposal_id=None,
            conversation_id=None,
            ai_call_id=None,
            created_at=NOW,
        )

        assert typed.source is MemorySource.USER

    def test_a_fact_with_no_identifiable_content_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="Nothing identifiable"):
            MemoryKey.of("!!! ???")


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class _Harness:
    """A review environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(GROUP_CONTACT): int(ACCOUNT_A)},
        )
        self.conversation_store = InMemoryConversationStore(
            chats={int(CHAT): int(ACCOUNT_A), int(GROUP_CHAT): int(ACCOUNT_A)}
        )
        self.proposal_store = InMemoryMemoryProposalStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            conversations={
                int(CONVERSATION): int(ACCOUNT_A),
                int(LATER_CONVERSATION): int(ACCOUNT_A),
                int(GROUP_CONVERSATION): int(ACCOUNT_A),
            },
        )
        self.memory_store = InMemoryMemoryStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(GROUP_CONTACT): int(ACCOUNT_A)},
        )
        self.clock = AdvanceableClock(LATER)
        self.ids = SequentialIdGenerator(start=900)
        self.events = RecordingEventBus()
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()
        self.proposals_factory: Any = self.proposals
        self.memories_factory: Any = self.memories

    def unit_of_work(self) -> InMemoryUnitOfWork:
        uow = self._factory()
        self.units.append(uow)
        return uow

    def accounts(self, _uow: UnitOfWork) -> InMemoryAccountRepository:
        return self.accounts_repository

    def chats(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryChatRepository:
        return InMemoryChatRepository(self.chat_store, account_id)

    def conversations(
        self, _uow: UnitOfWork, account_id: AccountId
    ) -> InMemoryConversationRepository:
        return InMemoryConversationRepository(self.conversation_store, account_id)

    def proposals(
        self, _uow: UnitOfWork, account_id: AccountId
    ) -> InMemoryMemoryProposalRepository:
        return InMemoryMemoryProposalRepository(self.proposal_store, account_id)

    def memories(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryMemoryRepository:
        return InMemoryMemoryRepository(self.memory_store, account_id)

    async def setup(self) -> MemoryProposal:
        """Create an account, a private chat, a group chat and one proposal."""
        await self.accounts_repository.add(
            Account.create(
                account_id=ACCOUNT_A,
                telegram_user_id=TelegramUserId(1001),
                display_name="me",
                now=NOW,
                is_active=True,
            )
        )
        await self.chats(self.unit_of_work(), ACCOUNT_A).add(
            Chat.private_with(
                chat_id=CHAT,
                account_id=ACCOUNT_A,
                telegram_chat_id=TelegramChatId(5011),
                contact_id=CONTACT,
                now=NOW,
                ai_processing_mode=AiProcessingMode.LOCAL_ONLY,
            )
        )
        await self.chats(self.unit_of_work(), ACCOUNT_A).add(
            Chat.group_titled(
                chat_id=GROUP_CHAT,
                account_id=ACCOUNT_A,
                telegram_chat_id=TelegramChatId(-5012),
                chat_type=ChatType.GROUP,
                title="Book club",
                now=NOW,
            )
        )
        for index, (conversation_id, chat_id) in enumerate(
            ((CONVERSATION, CHAT), (LATER_CONVERSATION, CHAT), (GROUP_CONVERSATION, GROUP_CHAT))
        ):
            started_at = NOW + timedelta(days=index)
            await self.conversations(self.unit_of_work(), ACCOUNT_A).add(
                Conversation.spanning(
                    conversation_id=conversation_id,
                    account_id=ACCOUNT_A,
                    chat_id=chat_id,
                    started_at=started_at,
                    ended_at=started_at + timedelta(minutes=10),
                    message_count=3,
                    now=NOW,
                )
            )

        proposal = make_proposal()
        await self.proposals(self.unit_of_work(), ACCOUNT_A).add(proposal)
        return proposal

    async def propose(self, **overrides: Any) -> MemoryProposal:
        """Add another proposal."""
        proposal = make_proposal(**overrides)
        await self.proposals(self.unit_of_work(), ACCOUNT_A).add(proposal)
        return proposal

    def accept(self) -> AcceptMemoryProposal:
        return AcceptMemoryProposal(
            self.unit_of_work,
            self.proposals_factory,
            self.memories_factory,
            self.conversations,
            self.chats,
            self.accounts,
            self.clock,
            self.ids,
            self.events,
        )

    def reject(self) -> RejectMemoryProposal:
        return RejectMemoryProposal(
            self.unit_of_work, self.proposals_factory, self.accounts, self.clock, self.events
        )

    def forget(self) -> DeleteMemory:
        return DeleteMemory(self.unit_of_work, self.memories_factory, self.accounts, self.clock)

    def read(self) -> GetMemory:
        return GetMemory(self.unit_of_work, self.memories_factory, self.accounts)

    def listing(self) -> ListMemories:
        return ListMemories(self.unit_of_work, self.memories_factory, self.accounts)

    async def stored_proposal(self, proposal_id: int = 1) -> MemoryProposal | None:
        return await self.proposals(self.unit_of_work(), ACCOUNT_A).get(
            MemoryProposalId(proposal_id)
        )


@pytest.fixture
async def harness() -> _Harness:
    """One account, two chats, one pending proposal."""
    built = _Harness()
    await built.setup()
    return built


# ---------------------------------------------------------------------------
# Accepting
# ---------------------------------------------------------------------------


class TestAccepting:
    async def test_it_creates_a_memory(self, harness: _Harness) -> None:
        result = await harness.accept().execute(1)

        assert result.memory.value == "Lives in Lisbon"
        assert result.memory.source is MemorySource.AI_APPROVED

    async def test_the_proposal_becomes_accepted(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        stored = await harness.stored_proposal()
        assert stored is not None
        assert stored.status is ProposalStatus.ACCEPTED
        assert stored.decided_at == LATER

    async def test_the_memory_names_the_person_it_is_about(self, harness: _Harness) -> None:
        # Resolved from the conversation's chat, not stored on the proposal: a
        # proposal is about a conversation, and a conversation knows its chat.
        result = await harness.accept().execute(1)

        assert result.memory.contact_id == CONTACT

    async def test_a_group_conversation_produces_a_memory_about_nobody(
        self, harness: _Harness
    ) -> None:
        # The only reason a memory's contact is nullable.
        await harness.propose(
            proposal_id=MemoryProposalId(2),
            conversation_id=GROUP_CONVERSATION,
            value="Meets on Thursdays",
        )

        result = await harness.accept().execute(2)

        assert result.memory.contact_id is None

    async def test_the_memory_gets_its_own_identifier(self, harness: _Harness) -> None:
        # A memory and its proposal are different things with different
        # lifetimes; one identifier for both would confuse them everywhere.
        result = await harness.accept().execute(1)

        assert int(result.memory.id) != int(result.proposal.id)

    async def test_it_is_one_transaction(self, harness: _Harness) -> None:
        # The decision and its consequence are the same event. A committed
        # acceptance with no memory would be a fact the user cannot find.
        await harness.accept().execute(1)

        assert sum(1 for unit in harness.units if unit.is_committed) == 1

    async def test_it_publishes_both_events(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        assert len(harness.events.events_of(MemoryProposalAccepted)) == 1
        assert len(harness.events.events_of(MemoryCreated)) == 1

    async def test_the_events_name_the_memory(self, harness: _Harness) -> None:
        result = await harness.accept().execute(1)

        (accepted,) = harness.events.events_of(MemoryProposalAccepted)
        assert isinstance(accepted, MemoryProposalAccepted)
        assert accepted.memory_id == int(result.memory.id)

    async def test_the_memory_is_listed(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        page = await harness.listing().execute(PageRequest(limit=10))

        assert len(page.items) == 1

    async def test_an_unknown_proposal_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No memory proposal"):
            await harness.accept().execute(9999)

    async def test_another_accounts_proposal_is_invisible(self, harness: _Harness) -> None:
        await harness.accounts_repository.add(
            Account.create(
                account_id=ACCOUNT_B,
                telegram_user_id=TelegramUserId(1002),
                display_name="them",
                now=NOW,
            )
        )

        with pytest.raises(RecordNotFoundError):
            await harness.accept().execute(1, account_id=ACCOUNT_B)


class TestAcceptingTwice:
    async def test_the_second_acceptance_is_refused(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            await harness.accept().execute(1)

    async def test_and_names_the_memory_it_already_produced(self, harness: _Harness) -> None:
        # More useful than a bare refusal: somebody accepting twice has usually
        # forgotten they did it once.
        result = await harness.accept().execute(1)

        with pytest.raises(InvalidStateTransitionError) as excinfo:
            await harness.accept().execute(1)

        assert excinfo.value.context["memory_id"] == int(result.memory.id)

    async def test_no_second_memory_is_created(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        with pytest.raises(InvalidStateTransitionError):
            await harness.accept().execute(1)

        page = await harness.listing().execute(PageRequest(limit=10))
        assert len(page.items) == 1

    async def test_a_rejected_proposal_cannot_be_accepted(self, harness: _Harness) -> None:
        await harness.reject().execute(1)

        with pytest.raises(InvalidStateTransitionError, match="already rejected"):
            await harness.accept().execute(1)

    async def test_and_nothing_is_remembered(self, harness: _Harness) -> None:
        await harness.reject().execute(1)
        with pytest.raises(InvalidStateTransitionError):
            await harness.accept().execute(1)

        page = await harness.listing().execute(PageRequest(limit=10))
        assert not page.items


class TestAcceptingADuplicateFact:
    async def test_the_same_fact_about_one_person_twice_is_refused(self, harness: _Harness) -> None:
        # Two proposals from two conversations with the same person, saying the
        # same thing in different words. The derived key makes them one fact.
        await harness.accept().execute(1)
        await harness.propose(
            proposal_id=MemoryProposalId(2),
            conversation_id=LATER_CONVERSATION,
            value="lives in  LISBON!",
        )

        with pytest.raises(ConstraintViolationError):
            await harness.accept().execute(2)

    async def test_a_refused_memory_creates_nothing(self, harness: _Harness) -> None:
        # That the *decision* is also undone needs a real transaction to
        # observe -- the in-memory repositories write through immediately -- so
        # it is asserted against SQLite below.
        await harness.accept().execute(1)
        await harness.propose(
            proposal_id=MemoryProposalId(2),
            conversation_id=LATER_CONVERSATION,
            value="Lives in Lisbon.",
        )

        with pytest.raises(ConstraintViolationError):
            await harness.accept().execute(2)

        page = await harness.listing().execute(PageRequest(limit=10))
        assert len(page.items) == 1

    async def test_a_contradiction_is_not_a_duplicate(self, harness: _Harness) -> None:
        # The limitation stated as a test: the key deduplicates, it does not
        # detect a contradiction (ADR-059).
        await harness.accept().execute(1)
        await harness.propose(
            proposal_id=MemoryProposalId(2),
            conversation_id=LATER_CONVERSATION,
            value="Lives in Porto",
        )

        await harness.accept().execute(2)

        page = await harness.listing().execute(PageRequest(limit=10))
        assert len(page.items) == 2


# ---------------------------------------------------------------------------
# Rejecting
# ---------------------------------------------------------------------------


class TestRejecting:
    async def test_it_creates_nothing(self, harness: _Harness) -> None:
        await harness.reject().execute(1)

        page = await harness.listing().execute(PageRequest(limit=10))
        assert not page.items

    async def test_the_proposal_becomes_rejected(self, harness: _Harness) -> None:
        rejected = await harness.reject().execute(1)

        assert rejected.status is ProposalStatus.REJECTED
        stored = await harness.stored_proposal()
        assert stored is not None
        assert stored.status is ProposalStatus.REJECTED

    async def test_the_proposal_is_kept(self, harness: _Harness) -> None:
        # Kept rather than deleted, so extraction does not offer the same fact
        # again (DOMAIN_MODEL section 5.10).
        await harness.reject().execute(1)

        assert await harness.stored_proposal() is not None

    async def test_rejecting_twice_is_refused(self, harness: _Harness) -> None:
        await harness.reject().execute(1)

        with pytest.raises(InvalidStateTransitionError, match="already rejected"):
            await harness.reject().execute(1)

    async def test_an_accepted_proposal_cannot_be_rejected(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            await harness.reject().execute(1)

    async def test_and_the_memory_survives(self, harness: _Harness) -> None:
        # A failed reversal must not half-succeed.
        result = await harness.accept().execute(1)
        with pytest.raises(InvalidStateTransitionError):
            await harness.reject().execute(1)

        found = await harness.read().execute(int(result.memory.id))
        assert found is not None
        assert found.is_active

    async def test_it_publishes_a_rejection(self, harness: _Harness) -> None:
        # Rejections are published for the same reason failures are recorded:
        # an audit of only what was kept cannot show what the extractor gets
        # wrong.
        await harness.reject().execute(1)

        assert len(harness.events.events_of(MemoryProposalRejected)) == 1

    async def test_it_publishes_no_creation(self, harness: _Harness) -> None:
        await harness.reject().execute(1)

        assert harness.events.events_of(MemoryCreated) == []

    async def test_an_unknown_proposal_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No memory proposal"):
            await harness.reject().execute(9999)


# ---------------------------------------------------------------------------
# Forgetting
# ---------------------------------------------------------------------------


class TestForgetting:
    async def test_a_memory_can_be_forgotten(self, harness: _Harness) -> None:
        result = await harness.accept().execute(1)

        assert await harness.forget().execute(int(result.memory.id))

        page = await harness.listing().execute(PageRequest(limit=10))
        assert not page.items

    async def test_it_is_soft(self, harness: _Harness) -> None:
        result = await harness.accept().execute(1)
        await harness.forget().execute(int(result.memory.id))

        found = await harness.read().execute(int(result.memory.id))

        assert found is not None
        assert not found.is_active
        assert found.deleted_at == LATER

    async def test_forgetting_twice_is_not_an_error(self, harness: _Harness) -> None:
        result = await harness.accept().execute(1)
        await harness.forget().execute(int(result.memory.id))

        assert not await harness.forget().execute(int(result.memory.id))

    async def test_it_frees_the_fact_to_be_remembered_again(self, harness: _Harness) -> None:
        # The only route to a correction, since nothing edits a memory.
        result = await harness.accept().execute(1)
        await harness.forget().execute(int(result.memory.id))
        await harness.propose(proposal_id=MemoryProposalId(2), conversation_id=LATER_CONVERSATION)

        again = await harness.accept().execute(2)

        assert again.memory.key == result.memory.key

    async def test_it_does_not_reopen_the_proposal(self, harness: _Harness) -> None:
        # Forgetting a memory is not undoing a decision. The proposal stays
        # accepted, so extraction still does not offer the fact again.
        result = await harness.accept().execute(1)

        await harness.forget().execute(int(result.memory.id))

        stored = await harness.stored_proposal()
        assert stored is not None
        assert stored.status is ProposalStatus.ACCEPTED

    async def test_an_unknown_memory_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No memory"):
            await harness.forget().execute(9999)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class _FailsBeforeCommit(MemoryRepository):
    """A memory repository that dies at a chosen point.

    ``after=0`` fails before anything is written; ``after=1`` fails once the
    memory is in the open transaction. The two cover both halves of "nothing
    survives a rollback".
    """

    def __init__(self, inner: MemoryRepository, *, after: int) -> None:
        self._inner = inner
        self._after = after
        self._written = 0

    @property
    def account_id(self) -> AccountId:
        return self._inner.account_id

    async def add(self, memory: Memory) -> None:
        if self._written >= self._after:
            msg = "died here"
            raise RuntimeError(msg)
        await self._inner.add(memory)
        self._written += 1

    async def get(self, memory_id: MemoryId) -> Memory | None:
        return await self._inner.get(memory_id)

    async def get_by_proposal(self, proposal_id: MemoryProposalId) -> Memory | None:
        return await self._inner.get_by_proposal(proposal_id)

    async def list_active(self, request: PageRequest) -> Any:
        return await self._inner.list_active(request)

    async def list_for_contact(
        self, contact_id: ContactId | None, *, limit: int
    ) -> tuple[Memory, ...]:
        return await self._inner.list_for_contact(contact_id, limit=limit)

    async def mark_retrieved(self, memory_ids: Sequence[MemoryId], now: datetime) -> int:
        return await self._inner.mark_retrieved(memory_ids, now)

    async def delete(self, memory_id: MemoryId, now: datetime) -> bool:
        return await self._inner.delete(memory_id, now)


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


async def _prepare(container: Container) -> tuple[int, int]:
    """Create an account, a chat, a conversation, an AI call and a proposal."""
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
    await container.ingest_messages().execute(
        chat_id=int(chat.id),
        incoming=[
            IncomingMessage(
                sender_kind=SenderKind.CONTACT,
                sent_at=NOW,
                text="I moved to Lisbon last month.",
                message_type=MessageType.TEXT,
                telegram_message_id=10,
            )
        ],
    )
    page = await container.list_conversations().execute(int(chat.id))
    conversation = page.items[0]

    account = await container.get_account().execute(None)
    assert account is not None
    async with container.unit_of_work() as uow:
        call = AiCall.record(
            call_id=AiCallId(container.ids.new_id()),
            account_id=account.id,
            chat_id=chat.id,
            model=LOCAL_MODEL,
            prompt=PROMPT,
            task_kind="extract_memories",
            outcome=AiOutcome.SUCCESS,
            latency_ms=10,
            now=NOW,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            finish_reason=FinishReason.STOP,
            response="{}",
        )
        await container.ai_calls(uow, account.id).add(call)
        proposal = MemoryProposal.propose(
            proposal_id=MemoryProposalId(container.ids.new_id()),
            account_id=account.id,
            conversation_id=conversation.id,
            ai_call_id=call.id,
            category=MemoryCategory.LOCATION,
            value="Lives in Lisbon",
            confidence=Confidence(0.9),
            evidence=Evidence("I moved to Lisbon last month"),
            prompt=PROMPT,
            now=NOW,
        )
        await container.memory_proposals(uow, account.id).add(proposal)
        await uow.commit()
    return int(proposal.id), int(chat.id)


@pytest.fixture
async def stored(container: Container) -> AsyncIterator[Container]:
    """A container over a real SQLite file."""
    try:
        yield container
    finally:
        await container.aclose()


class TestAgainstARealDatabase:
    async def test_accepting_stores_a_memory(self, stored: Container) -> None:
        proposal_id, _chat = await _prepare(stored)

        result = await stored.accept_memory_proposal().execute(proposal_id)

        found = await stored.get_memory().execute(int(result.memory.id))
        assert found is not None
        assert found.value == "Lives in Lisbon"
        assert found.key == MemoryKey("lives in lisbon")
        assert found.source is MemorySource.AI_APPROVED

    async def test_and_the_proposal_is_accepted(self, stored: Container) -> None:
        proposal_id, _chat = await _prepare(stored)

        await stored.accept_memory_proposal().execute(proposal_id)

        proposal = await stored.get_memory_proposal().execute(proposal_id)
        assert proposal is not None
        assert proposal.status is ProposalStatus.ACCEPTED
        assert proposal.decided_at is not None

    async def test_accepting_twice_is_refused(self, stored: Container) -> None:
        proposal_id, _chat = await _prepare(stored)
        await stored.accept_memory_proposal().execute(proposal_id)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            await stored.accept_memory_proposal().execute(proposal_id)

        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert len(page.items) == 1

    async def test_rejecting_stores_nothing(self, stored: Container) -> None:
        proposal_id, _chat = await _prepare(stored)

        await stored.reject_memory_proposal().execute(proposal_id)

        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert not page.items

    async def test_an_exception_before_commit_persists_nothing(self, stored: Container) -> None:
        # The crash test. The failure lands before the memory is written, and
        # the proposal must stay pending: a decision that survived a rollback
        # would be a fact the user believes they kept and cannot find.
        proposal_id, _chat = await _prepare(stored)
        accept = AcceptMemoryProposal(
            stored.unit_of_work,
            stored.memory_proposals,
            lambda uow, account_id: _FailsBeforeCommit(stored.memories(uow, account_id), after=0),
            stored.conversations,
            stored.chats,
            stored.accounts,
            stored.clock,
            stored.ids,
            stored.events,
        )

        with pytest.raises(RuntimeError, match="died here"):
            await accept.execute(proposal_id)

        proposal = await stored.get_memory_proposal().execute(proposal_id)
        assert proposal is not None
        assert proposal.is_pending
        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert not page.items

    async def test_an_exception_after_the_memory_is_written_persists_nothing(
        self, stored: Container
    ) -> None:
        # The other half: the memory *and* the decision are both in the open
        # transaction when the failure lands, and neither survives.
        proposal_id, _chat = await _prepare(stored)
        accept = AcceptMemoryProposal(
            stored.unit_of_work,
            stored.memory_proposals,
            lambda uow, account_id: _FailsAfterCommitPoint(stored.memories(uow, account_id)),
            stored.conversations,
            stored.chats,
            stored.accounts,
            stored.clock,
            stored.ids,
            stored.events,
        )

        with pytest.raises(RuntimeError, match="died here"):
            await accept.execute(proposal_id)

        proposal = await stored.get_memory_proposal().execute(proposal_id)
        assert proposal is not None
        assert proposal.is_pending
        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert not page.items

    async def test_a_failed_acceptance_can_be_retried(self, stored: Container) -> None:
        # The proof the rollback left nothing behind.
        proposal_id, _chat = await _prepare(stored)
        accept = AcceptMemoryProposal(
            stored.unit_of_work,
            stored.memory_proposals,
            lambda uow, account_id: _FailsBeforeCommit(stored.memories(uow, account_id), after=0),
            stored.conversations,
            stored.chats,
            stored.accounts,
            stored.clock,
            stored.ids,
            stored.events,
        )
        with pytest.raises(RuntimeError):
            await accept.execute(proposal_id)

        result = await stored.accept_memory_proposal().execute(proposal_id)

        assert result.memory.value == "Lives in Lisbon"

    async def test_a_duplicate_key_is_refused_by_the_database(self, stored: Container) -> None:
        proposal_id, _chat = await _prepare(stored)
        await stored.accept_memory_proposal().execute(proposal_id)

        account = await stored.get_account().execute(None)
        assert account is not None
        page = await stored.list_memories().execute(PageRequest(limit=1))
        existing = page.items[0]

        async with stored.unit_of_work() as uow:
            with pytest.raises(ConstraintViolationError) as excinfo:
                await stored.memories(uow, account.id).add(
                    Memory(
                        id=MemoryId(stored.ids.new_id()),
                        account_id=existing.account_id,
                        contact_id=existing.contact_id,
                        category=existing.category,
                        key=existing.key,
                        value=existing.value,
                        confidence=existing.confidence,
                        source=MemorySource.USER,
                        proposal_id=None,
                        conversation_id=None,
                        ai_call_id=None,
                        created_at=datetime.now(UTC),
                    )
                )
        assert excinfo.value.user_message == "That fact is already remembered."

    async def test_deleting_a_chat_keeps_the_memory(self, stored: Container) -> None:
        # The one place provenance is SET NULL rather than CASCADE. A memory is
        # user-approved knowledge, and it does not stop being known because the
        # conversation it came from was deleted -- what is lost is the trail.
        proposal_id, chat_id = await _prepare(stored)
        result = await stored.accept_memory_proposal().execute(proposal_id)

        async with stored.unit_of_work() as uow:
            await uow.database.executor.run(
                lambda: uow.connection.execute(
                    text("DELETE FROM chats WHERE id = :id"), {"id": chat_id}
                )
            )
            await uow.commit()

        found = await stored.get_memory().execute(int(result.memory.id))
        assert found is not None
        assert found.value == "Lives in Lisbon"
        assert found.proposal_id is None
        assert found.conversation_id is None
        assert found.ai_call_id is None

    async def test_forgetting_then_accepting_again_works(self, stored: Container) -> None:
        proposal_id, _chat = await _prepare(stored)
        result = await stored.accept_memory_proposal().execute(proposal_id)

        assert await stored.delete_memory().execute(int(result.memory.id))

        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert not page.items
        again = await stored.get_memory().execute(int(result.memory.id))
        assert again is not None
        assert not again.is_active


class _FailsAfterCommitPoint(MemoryRepository):
    """A memory repository that writes and then dies."""

    def __init__(self, inner: MemoryRepository) -> None:
        self._inner = inner

    @property
    def account_id(self) -> AccountId:
        return self._inner.account_id

    async def add(self, memory: Memory) -> None:
        await self._inner.add(memory)
        msg = "died here"
        raise RuntimeError(msg)

    async def get(self, memory_id: MemoryId) -> Memory | None:
        return await self._inner.get(memory_id)

    async def get_by_proposal(self, proposal_id: MemoryProposalId) -> Memory | None:
        return await self._inner.get_by_proposal(proposal_id)

    async def list_active(self, request: PageRequest) -> Any:
        return await self._inner.list_active(request)

    async def list_for_contact(
        self, contact_id: ContactId | None, *, limit: int
    ) -> tuple[Memory, ...]:
        return await self._inner.list_for_contact(contact_id, limit=limit)

    async def mark_retrieved(self, memory_ids: Sequence[MemoryId], now: datetime) -> int:
        return await self._inner.mark_retrieved(memory_ids, now)

    async def delete(self, memory_id: MemoryId, now: datetime) -> bool:
        return await self._inner.delete(memory_id, now)


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


async def _seed(conversation_id: int | None = None) -> int:  # noqa: ARG001 - symmetry
    """Create the world through the container, and return a proposal id."""
    container = Container.create()
    try:
        proposal_id, _chat = await _prepare(container)
        return proposal_id
    finally:
        await container.aclose()


@pytest.mark.usefixtures("cli_env")
class TestReviewCommands:
    """The queue, end to end."""

    @pytest.fixture
    def proposal(self) -> str:
        """Seed a pending proposal and return its identifier."""
        import asyncio  # noqa: PLC0415

        return str(asyncio.run(_seed()))

    def test_accept_remembers_it(self, proposal: str) -> None:
        result = runner.invoke(app, ["memory", "accept", proposal])

        assert result.exit_code == 0, result.output
        assert "Remembered as memory" in result.output
        assert "lives in lisbon" in result.output

    def test_and_it_appears_in_the_listing(self, proposal: str) -> None:
        _run_cli("memory", "accept", proposal)

        assert "Lives in Lisbon" in _run_cli("memory", "list")

    def test_accepting_twice_is_refused(self, proposal: str) -> None:
        _run_cli("memory", "accept", proposal)

        result = runner.invoke(app, ["memory", "accept", proposal])

        assert result.exit_code != 0
        assert "already accepted" in result.output

    def test_reject_remembers_nothing(self, proposal: str) -> None:
        result = runner.invoke(app, ["memory", "reject", proposal])

        assert result.exit_code == 0, result.output
        assert "Nothing was remembered" in result.output
        assert "Nothing remembered yet" in _run_cli("memory", "list")

    def test_rejecting_twice_is_refused(self, proposal: str) -> None:
        _run_cli("memory", "reject", proposal)

        result = runner.invoke(app, ["memory", "reject", proposal])

        assert result.exit_code != 0
        assert "already been decided" in result.output or "already rejected" in result.output

    def test_a_rejected_proposal_cannot_be_accepted(self, proposal: str) -> None:
        _run_cli("memory", "reject", proposal)

        result = runner.invoke(app, ["memory", "accept", proposal])

        assert result.exit_code != 0
        assert "already rejected" in result.output

    def test_show_prints_the_provenance(self, proposal: str) -> None:
        _run_cli("memory", "accept", proposal)
        identifier = _run_cli("memory", "list").splitlines()[0].split()[0]

        shown = _run_cli("memory", "show", identifier)

        assert "source       ai_approved" in shown
        assert "key          lives in lisbon" in shown
        assert f"proposal     {proposal}" in shown

    def test_forget_removes_it_from_the_listing(self, proposal: str) -> None:
        _run_cli("memory", "accept", proposal)
        identifier = _run_cli("memory", "list").splitlines()[0].split()[0]

        assert "Forgot memory" in _run_cli("memory", "forget", identifier)

        assert "Nothing remembered yet" in _run_cli("memory", "list")

    def test_forgetting_twice_says_so(self, proposal: str) -> None:
        _run_cli("memory", "accept", proposal)
        identifier = _run_cli("memory", "list").splitlines()[0].split()[0]
        _run_cli("memory", "forget", identifier)

        assert "had already been forgotten" in _run_cli("memory", "forget", identifier)

    def test_a_forgotten_memory_can_still_be_shown(self, proposal: str) -> None:
        # "Show me what you deleted" is a question a person is entitled to ask
        # of their own data.
        _run_cli("memory", "accept", proposal)
        identifier = _run_cli("memory", "list").splitlines()[0].split()[0]
        _run_cli("memory", "forget", identifier)

        assert "(forgotten)" in _run_cli("memory", "show", identifier)

    def test_the_proposal_records_its_decision(self, proposal: str) -> None:
        _run_cli("memory", "accept", proposal)

        assert "accepted" in _run_cli("memory", "proposals")

    def test_show_reports_an_unknown_memory(self, proposal: str) -> None:  # noqa: ARG002
        result = runner.invoke(app, ["memory", "show", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_accept_reports_an_unknown_proposal(self, proposal: str) -> None:  # noqa: ARG002
        result = runner.invoke(app, ["memory", "accept", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()
