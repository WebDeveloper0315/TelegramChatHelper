"""Memory extraction: the first complete AI feature.

The pipeline is conversation -> prompt -> model -> validation -> proposal ->
queue, and every test here is about one of the joins between those, or about a
way the model can be wrong.

Three properties matter more than the rest:

* **Nothing is remembered.** Every fact lands in a queue as ``pending``, and
  there is no code path in this milestone that changes that.
* **Nothing is trusted.** A validated answer still passes three deterministic
  filters -- grounded evidence, confidence, not already proposed -- before it is
  stored.
* **Nothing is invented by the model except the four fields it is asked for.**
  Identifier, timestamp, provenance, prompt version and status all come from the
  application.

No test here reaches a network or a model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock, InMemorySecretStore, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.ai_call_repository import InMemoryAiCallRepository, InMemoryAiCallStore
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
from tests.fakes.message_repository import InMemoryMessageRepository, InMemoryMessageStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.ai import ExecuteAiTask, StructuredAiTask
from tgassist.application.use_cases.memory import (
    EXTRACT_PROMPT,
    NOTHING_PROPOSED,
    TASK_KIND,
    ExtractionPolicy,
    ExtractMemories,
)
from tgassist.application.use_cases.message import IncomingMessage
from tgassist.domain.errors import (
    AiForbiddenError,
    AiTimeoutError,
    RecordNotFoundError,
    SchemaViolationError,
)
from tgassist.domain.events import MemoryProposalsCreated
from tgassist.domain.model.account import Account
from tgassist.domain.model.ai import (
    AiCall,
    AiOutcome,
    FinishReason,
    PromptVersion,
    TokenUsage,
)
from tgassist.domain.model.chat import AiProcessingMode, Chat
from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    ContactId,
    ConversationId,
    MemoryProposalId,
    MessageId,
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.memory import (
    Confidence,
    Evidence,
    MemoryCategory,
    MemoryProposal,
    ProposalStatus,
)
from tgassist.domain.model.message import Message, MessageType, SenderKind
from tgassist.domain.model.prompt import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.memory_proposal_repository import MemoryProposalRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.ai.scripted import (
    CLOUD_MODEL,
    LOCAL_MODEL,
    ScriptedAiProvider,
)
from tgassist.infrastructure.prompts import FilePromptRegistry
from tgassist.presentation.cli.app import app

runner = CliRunner()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CHAT = ChatId(11)
CLOSED_CHAT = ChatId(12)
CONTACT = ContactId(101)
OTHER_CONTACT = ContactId(102)
CONVERSATION = ConversationId(201)
CLOSED_CONVERSATION = ConversationId(202)

#: What the conversation the model reads actually says. Every quotation a test
#: calls "grounded" is a substring of one of these.
TRANSCRIPT = [
    (SenderKind.CONTACT, "I moved to Lisbon last month for a job at a design studio."),
    (SenderKind.OPERATOR, "That sounds like a big change. How is it going?"),
    (SenderKind.CONTACT, "Good. My sister visits in September, I am counting the days."),
]


def proposal_payload(**overrides: Any) -> dict[str, Any]:
    """Build one entry of the answer the model is supposed to give."""
    entry: dict[str, Any] = {
        "category": "location",
        "value": "Lives in Lisbon",
        "confidence": 0.9,
        "evidence": "I moved to Lisbon last month",
    }
    entry.update(overrides)
    return entry


def answer(*entries: dict[str, Any]) -> str:
    """Build a whole answer."""
    return json.dumps({"proposals": list(entries)})


# ---------------------------------------------------------------------------
# The aggregate
# ---------------------------------------------------------------------------


PROMPT = PromptVersion(prompt_id="memory_extract", version="1.0.0")


def make_proposal(**overrides: Any) -> MemoryProposal:
    """Build a proposal directly."""
    values: dict[str, Any] = {
        "proposal_id": MemoryProposalId(1),
        "account_id": ACCOUNT_A,
        "conversation_id": CONVERSATION,
        "ai_call_id": AiCallId(501),
        "category": MemoryCategory.LOCATION,
        "value": "Lives in Lisbon",
        "confidence": Confidence(0.9),
        "evidence": Evidence("I moved to Lisbon last month"),
        "prompt": PROMPT,
        "now": NOW,
    }
    values.update(overrides)
    return MemoryProposal.propose(**values)


class TestTheAggregate:
    def test_a_proposal_starts_pending(self) -> None:
        assert make_proposal().status is ProposalStatus.PENDING

    def test_there_is_no_way_to_create_a_decided_one(self) -> None:
        # The factory takes no status. A proposal that could be created decided
        # would be a decision nobody made.
        with pytest.raises(TypeError):
            make_proposal(status=ProposalStatus.ACCEPTED)

    def test_pending_is_not_terminal(self) -> None:
        assert not ProposalStatus.PENDING.is_terminal

    def test_accepted_and_rejected_are(self) -> None:
        assert ProposalStatus.ACCEPTED.is_terminal
        assert ProposalStatus.REJECTED.is_terminal

    def test_the_aggregate_has_exactly_one_transition(self) -> None:
        # Expressed in the shape rather than in a rule: a proposal is created
        # and decided, and there is nothing else it becomes. Nothing returns one
        # to pending, so a decision cannot be undone (ADR-059).
        changing = [
            name
            for name in dir(MemoryProposal)
            if not name.startswith("_") and callable(getattr(MemoryProposal, name, None))
        ]

        assert changing == ["decided", "propose"]

    def test_an_empty_value_is_refused(self) -> None:
        with pytest.raises(Exception, match="propose something"):
            make_proposal(value="   ")

    def test_evidence_is_required(self) -> None:
        # A proposal without a source is a claim nobody can check.
        with pytest.raises(Exception, match="quote what it was read from"):
            Evidence("  ")

    def test_a_confidence_outside_the_range_is_refused(self) -> None:
        # Not a low confidence: a model that did not answer the question asked.
        with pytest.raises(Exception, match="between 0 and 1"):
            Confidence(1.5)

    def test_a_boolean_is_not_a_confidence(self) -> None:
        with pytest.raises(Exception, match="must be a number"):
            Confidence(True)

    def test_a_confidence_renders_for_a_listing(self) -> None:
        assert str(Confidence(0.875)) == "0.88"

    def test_a_proposal_renders_as_category_and_value(self) -> None:
        assert str(make_proposal()) == "location: Lives in Lisbon"


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class _Harness:
    """An extraction environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(OTHER_CONTACT): int(ACCOUNT_A)},
        )
        self.conversation_store = InMemoryConversationStore(
            chats={int(CHAT): int(ACCOUNT_A), int(CLOSED_CHAT): int(ACCOUNT_A)}
        )
        self.message_store = InMemoryMessageStore(
            chats={int(CHAT): int(ACCOUNT_A), int(CLOSED_CHAT): int(ACCOUNT_A)}
        )
        self.call_store = InMemoryAiCallStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            chats={int(CHAT): int(ACCOUNT_A), int(CLOSED_CHAT): int(ACCOUNT_A)},
        )
        self.proposal_store = InMemoryMemoryProposalStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            conversations={
                int(CONVERSATION): int(ACCOUNT_A),
                int(CLOSED_CONVERSATION): int(ACCOUNT_A),
            },
            calls=None,
        )
        self.clock = AdvanceableClock(NOW)
        self.ids = SequentialIdGenerator(start=900)
        self.provider = ScriptedAiProvider()
        self.events = RecordingEventBus()
        self.policy = ExtractionPolicy()
        self.registry = FilePromptRegistry()
        self.registry.load()
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()
        self.proposals_factory: Any = self.proposals

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

    def messages(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryMessageRepository:
        return InMemoryMessageRepository(self.message_store, account_id)

    def calls(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryAiCallRepository:
        return InMemoryAiCallRepository(self.call_store, account_id)

    def proposals(
        self, _uow: UnitOfWork, account_id: AccountId
    ) -> InMemoryMemoryProposalRepository:
        return InMemoryMemoryProposalRepository(self.proposal_store, account_id)

    async def setup(self) -> None:
        """Create an account, two chats, and one conversation with messages."""
        await self.accounts_repository.add(
            Account.create(
                account_id=ACCOUNT_A,
                telegram_user_id=TelegramUserId(1001),
                display_name="me",
                now=NOW,
                is_active=True,
            )
        )
        for chat_id, contact_id, mode in (
            (CHAT, CONTACT, AiProcessingMode.LOCAL_ONLY),
            (CLOSED_CHAT, OTHER_CONTACT, AiProcessingMode.DISABLED),
        ):
            await self.chats(self.unit_of_work(), ACCOUNT_A).add(
                Chat.private_with(
                    chat_id=chat_id,
                    account_id=ACCOUNT_A,
                    telegram_chat_id=TelegramChatId(5000 + int(chat_id)),
                    contact_id=contact_id,
                    now=NOW,
                    ai_processing_mode=mode,
                )
            )

        for index, (sender, body) in enumerate(TRANSCRIPT):
            await self.messages(self.unit_of_work(), ACCOUNT_A).add(
                Message.record(
                    message_id=MessageId(300 + index),
                    account_id=ACCOUNT_A,
                    chat_id=CHAT,
                    sender_kind=sender,
                    sent_at=NOW + timedelta(minutes=index),
                    ingested_at=NOW,
                    text=body,
                    message_type=MessageType.TEXT,
                    telegram_message_id=TelegramMessageId(10 + index),
                )
            )

        for conversation_id, chat_id in (
            (CONVERSATION, CHAT),
            (CLOSED_CONVERSATION, CLOSED_CHAT),
        ):
            await self.conversations(self.unit_of_work(), ACCOUNT_A).add(
                Conversation.spanning(
                    conversation_id=conversation_id,
                    account_id=ACCOUNT_A,
                    chat_id=chat_id,
                    started_at=NOW,
                    ended_at=NOW + timedelta(minutes=len(TRANSCRIPT)),
                    message_count=len(TRANSCRIPT),
                    now=NOW,
                )
            )

    def task(self) -> ExecuteAiTask:
        return ExecuteAiTask(
            self.unit_of_work,
            self.calls,
            self.chats,
            self.accounts,
            self.provider,
            self.clock,
            self.ids,
        )

    def extractor(self) -> ExtractMemories:
        return ExtractMemories(
            self.unit_of_work,
            self.proposals_factory,
            self.conversations,
            self.messages,
            self.accounts,
            StructuredAiTask(self.task()),
            self.registry,
            self.clock,
            self.ids,
            self.policy,
            self.events,
        )

    async def queued(self) -> tuple[MemoryProposal, ...]:
        """Return everything in the review queue, oldest first."""
        return await self.proposals(self.unit_of_work(), ACCOUNT_A).list_for_conversation(
            CONVERSATION
        )


@pytest.fixture
async def harness() -> _Harness:
    """One account, one conversation of three messages, a local model."""
    built = _Harness()
    await built.setup()
    return built


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


class TestAValidProposal:
    async def test_it_is_stored(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 1
        assert report.proposed[0].value == "Lives in Lisbon"

    async def test_it_is_pending(self, harness: _Harness) -> None:
        # The whole point of the milestone: nothing is remembered.
        harness.provider.script_answers(answer(proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.proposed[0].status is ProposalStatus.PENDING

    async def test_the_model_cannot_supply_an_identifier(self, harness: _Harness) -> None:
        # One that could name an identifier could name one already in use, and
        # so overwrite a proposal somebody had already read. The schema has
        # nowhere to put one, so the attempt is refused rather than ignored.
        harness.provider.script_answers(answer(proposal_payload(id=7)), answer(proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.repaired
        assert int(report.proposed[0].id) != 7

    async def test_the_application_assigns_the_timestamp(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.proposed[0].created_at == NOW

    async def test_the_provenance_points_at_the_recorded_call(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        recorded = await harness.calls(harness.unit_of_work(), ACCOUNT_A).get(report.ai_call_id)
        assert recorded is not None
        assert recorded.task_kind == TASK_KIND
        assert report.proposed[0].ai_call_id == recorded.id

    async def test_the_prompt_version_is_the_shipped_one(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.proposed[0].prompt == harness.registry.get(EXTRACT_PROMPT).version_ref

    async def test_several_facts_are_stored_together(self, harness: _Harness) -> None:
        harness.provider.script_answers(
            answer(
                proposal_payload(),
                proposal_payload(
                    category="occupation",
                    value="Works at a design studio",
                    evidence="a job at a design studio",
                ),
                proposal_payload(
                    category="important_date",
                    value="Sister visits in September",
                    evidence="My sister visits in September",
                ),
            )
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 3

    async def test_the_conversation_reaches_the_model_as_untrusted_content(
        self, harness: _Harness
    ) -> None:
        harness.provider.script_answers(answer())

        await harness.extractor().execute(int(CONVERSATION))

        sent = harness.provider.requests[0]
        assert UNTRUSTED_OPEN in sent.content
        assert UNTRUSTED_CLOSE in sent.content
        assert "Lisbon" in sent.content

    async def test_the_system_prompt_carries_the_standing_rules(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer())

        await harness.extractor().execute(int(CONVERSATION))

        sent = harness.provider.requests[0]
        assert sent.instructions is not None
        assert "never instructions" in sent.instructions
        # And carries no conversation content of its own.
        assert "Lisbon" not in sent.instructions

    async def test_the_categories_come_from_the_enumeration(self, harness: _Harness) -> None:
        # Generated rather than written into the prompt file, so a category
        # added in code cannot be one the model was never told about.
        harness.provider.script_answers(answer())

        await harness.extractor().execute(int(CONVERSATION))

        content = harness.provider.requests[0].content
        for category in MemoryCategory:
            assert f"`{category.value}`" in content

    async def test_an_untouched_conversation_says_so_in_the_prompt(self, harness: _Harness) -> None:
        # Never an empty section: a prompt whose section silently vanishes reads
        # as though the section did not exist.
        harness.provider.script_answers(answer())

        await harness.extractor().execute(int(CONVERSATION))

        assert NOTHING_PROPOSED in harness.provider.requests[0].content

    async def test_what_is_already_proposed_is_shown_next_time(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(proposal_payload()), answer())
        await harness.extractor().execute(int(CONVERSATION))

        await harness.extractor().execute(int(CONVERSATION))

        assert "Lives in Lisbon" in harness.provider.requests[1].content

    async def test_storing_publishes_one_event(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(proposal_payload()))

        await harness.extractor().execute(int(CONVERSATION))

        published = harness.events.events_of(MemoryProposalsCreated)
        assert len(published) == 1
        announced = published[0]
        assert isinstance(announced, MemoryProposalsCreated)
        assert announced.count == 1
        assert announced.chat_id == int(CHAT)

    async def test_storing_nothing_publishes_nothing(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer())

        await harness.extractor().execute(int(CONVERSATION))

        assert harness.events.events_of(MemoryProposalsCreated) == []


class TestAnEmptyExtraction:
    async def test_an_empty_list_is_a_correct_answer(self, harness: _Harness) -> None:
        # And a common one. Most conversations contain nothing worth
        # remembering, and a model that always finds something is worse than one
        # that sometimes finds nothing.
        harness.provider.script_answers(answer())

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.returned == 0
        assert report.stored == 0
        assert report.discarded == 0

    async def test_it_is_not_treated_as_a_failure(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer())

        report = await harness.extractor().execute(int(CONVERSATION))

        assert not report.repaired
        assert await harness.queued() == ()


# ---------------------------------------------------------------------------
# Answers that are the wrong shape
# ---------------------------------------------------------------------------


class TestMalformedAnswers:
    async def test_prose_instead_of_json_triggers_a_repair(self, harness: _Harness) -> None:
        harness.provider.script_answers("I think she lives in Lisbon.", answer(proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.repaired
        assert report.stored == 1

    async def test_the_repair_names_what_was_wrong(self, harness: _Harness) -> None:
        harness.provider.script_answers("not json at all", answer())

        await harness.extractor().execute(int(CONVERSATION))

        assert "not valid JSON" in harness.provider.requests[1].content

    async def test_the_repair_returns_the_answer_to_the_model(self, harness: _Harness) -> None:
        harness.provider.script_answers("she lives in Lisbon", answer())

        await harness.extractor().execute(int(CONVERSATION))

        assert "she lives in Lisbon" in harness.provider.requests[1].content

    async def test_a_missing_field_triggers_a_repair(self, harness: _Harness) -> None:
        broken = proposal_payload()
        del broken["evidence"]
        harness.provider.script_answers(answer(broken), answer(proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.repaired
        assert report.stored == 1

    async def test_a_missing_field_is_named_in_the_repair(self, harness: _Harness) -> None:
        broken = proposal_payload()
        del broken["evidence"]
        harness.provider.script_answers(answer(broken), answer())

        await harness.extractor().execute(int(CONVERSATION))

        assert "evidence is required" in harness.provider.requests[1].content

    async def test_a_field_the_model_may_not_supply_triggers_a_repair(
        self, harness: _Harness
    ) -> None:
        # A model cannot set its own status, identifier or provenance, because
        # the schema has nowhere to put them.
        harness.provider.script_answers(
            answer(proposal_payload(status="accepted")), answer(proposal_payload())
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.repaired
        assert report.proposed[0].status is ProposalStatus.PENDING

    async def test_a_confidence_out_of_range_triggers_a_repair(self, harness: _Harness) -> None:
        harness.provider.script_answers(
            answer(proposal_payload(confidence=5)), answer(proposal_payload())
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.repaired

    async def test_an_unknown_category_triggers_a_repair(self, harness: _Harness) -> None:
        harness.provider.script_answers(
            answer(proposal_payload(category="favourite_biscuit")), answer(proposal_payload())
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.repaired

    async def test_a_second_failure_raises(self, harness: _Harness) -> None:
        # Exactly one repair. A second is a different failure -- the model has
        # now been told twice -- and costs money to arrive at the same place.
        harness.provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError, match="after one repair"):
            await harness.extractor().execute(int(CONVERSATION))

    async def test_a_second_failure_makes_exactly_two_calls(self, harness: _Harness) -> None:
        harness.provider.script_answers("nonsense", "still nonsense", answer())

        with pytest.raises(SchemaViolationError):
            await harness.extractor().execute(int(CONVERSATION))

        assert harness.provider.calls == 2

    async def test_a_second_failure_stores_nothing(self, harness: _Harness) -> None:
        harness.provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError):
            await harness.extractor().execute(int(CONVERSATION))

        assert await harness.queued() == ()

    async def test_a_second_failure_still_records_both_calls(self, harness: _Harness) -> None:
        # A failed extraction still cost two calls, and instrumentation that
        # hid them would hide the expensive case.
        harness.provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError):
            await harness.extractor().execute(int(CONVERSATION))

        page = await harness.calls(harness.unit_of_work(), ACCOUNT_A).list_recent(
            PageRequest(limit=10)
        )
        assert len(page.items) == 2
        assert all(call.outcome is AiOutcome.SUCCESS for call in page.items)

    async def test_the_error_carries_the_violations_but_not_the_answer(
        self, harness: _Harness
    ) -> None:
        # Model output about a conversation is conversation content.
        harness.provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError) as excinfo:
            await harness.extractor().execute(int(CONVERSATION))

        assert excinfo.value.context["violations"]
        assert "still nonsense" not in str(excinfo.value.context)

    async def test_a_fenced_answer_is_not_a_failure(self, harness: _Harness) -> None:
        # Models add a code fence even when told not to, and spending a repair
        # attempt on punctuation would be a waste of a call.
        harness.provider.script_answers(f"```json\n{answer(proposal_payload())}\n```")

        report = await harness.extractor().execute(int(CONVERSATION))

        assert not report.repaired
        assert report.stored == 1


# ---------------------------------------------------------------------------
# Answers that are the right shape and still not trustworthy
# ---------------------------------------------------------------------------


class TestTheThreeFilters:
    async def test_an_invented_quotation_is_discarded(self, harness: _Harness) -> None:
        # The failure that matters most: a fluent, plausible fact about somebody
        # that nobody ever said. A model cannot quote what nobody said.
        harness.provider.script_answers(
            answer(
                proposal_payload(
                    category="relationship",
                    value="Has a brother named Tom",
                    evidence="my brother Tom is visiting",
                )
            )
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.ungrounded == 1
        assert report.stored == 0

    async def test_a_quotation_is_matched_forgivingly_on_whitespace(
        self, harness: _Harness
    ) -> None:
        harness.provider.script_answers(
            answer(proposal_payload(evidence="I  moved   to Lisbon\n last month"))
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 1

    async def test_and_on_case(self, harness: _Harness) -> None:
        harness.provider.script_answers(
            answer(proposal_payload(evidence="i moved to lisbon last month"))
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 1

    async def test_but_not_on_words(self, harness: _Harness) -> None:
        # Anything more forgiving would start accepting nearly-right
        # quotations, which is exactly what an invented fact produces.
        harness.provider.script_answers(
            answer(proposal_payload(evidence="I moved to Lisbon two months ago"))
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.ungrounded == 1

    async def test_a_low_confidence_proposal_is_discarded(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(proposal_payload(confidence=0.2)))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.low_confidence == 1
        assert report.stored == 0

    async def test_the_threshold_is_configurable(self, harness: _Harness) -> None:
        harness.policy = ExtractionPolicy(min_confidence=0.1)
        harness.provider.script_answers(answer(proposal_payload(confidence=0.2)))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 1

    async def test_a_proposal_exactly_at_the_threshold_is_kept(self, harness: _Harness) -> None:
        harness.policy = ExtractionPolicy(min_confidence=0.5)
        harness.provider.script_answers(answer(proposal_payload(confidence=0.5)))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 1

    async def test_an_already_proposed_fact_is_discarded(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(proposal_payload()), answer(proposal_payload()))
        await harness.extractor().execute(int(CONVERSATION))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.duplicates == 1
        assert report.stored == 0

    async def test_a_fact_proposed_twice_in_one_answer_is_stored_once(
        self, harness: _Harness
    ) -> None:
        # A model asked for distinct facts sometimes returns two.
        harness.provider.script_answers(answer(proposal_payload(), proposal_payload()))

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 1
        assert report.duplicates == 1

    async def test_the_same_fact_in_another_category_is_not_a_duplicate(
        self, harness: _Harness
    ) -> None:
        harness.provider.script_answers(
            answer(proposal_payload(), proposal_payload(category="plan"))
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 2

    async def test_a_run_is_capped(self, harness: _Harness) -> None:
        # A review queue is only useful while it is short enough to read.
        harness.policy = ExtractionPolicy(max_proposals=2)
        harness.provider.script_answers(
            answer(
                proposal_payload(value="one"),
                proposal_payload(value="two"),
                proposal_payload(value="three"),
            )
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert report.stored == 2
        assert report.over_cap == 1

    async def test_every_discard_is_counted(self, harness: _Harness) -> None:
        # A run that returned nothing and a run that discarded eight ungrounded
        # claims are very different events.
        harness.provider.script_answers(
            answer(
                proposal_payload(),
                proposal_payload(value="unsure", confidence=0.1),
                proposal_payload(value="invented", evidence="nobody said this"),
            )
        )

        report = await harness.extractor().execute(int(CONVERSATION))

        assert (report.returned, report.stored, report.discarded) == (3, 1, 2)
        assert (report.low_confidence, report.ungrounded) == (1, 1)


# ---------------------------------------------------------------------------
# When the model cannot be reached
# ---------------------------------------------------------------------------


class TestWhenTheModelFails:
    async def test_a_disabled_chat_refuses_extraction(self, harness: _Harness) -> None:
        # The privacy gate belongs to ExecuteAiTask, and this proves extraction
        # inherits it rather than reimplementing it.
        with pytest.raises(AiForbiddenError, match="switched off"):
            await harness.extractor().execute(int(CLOSED_CONVERSATION))

    async def test_a_refusal_reaches_no_model(self, harness: _Harness) -> None:
        with pytest.raises(AiForbiddenError):
            await harness.extractor().execute(int(CLOSED_CONVERSATION))

        assert harness.provider.calls == 0

    async def test_a_refusal_is_recorded(self, harness: _Harness) -> None:
        with pytest.raises(AiForbiddenError):
            await harness.extractor().execute(int(CLOSED_CONVERSATION))

        page = await harness.calls(harness.unit_of_work(), ACCOUNT_A).list_recent(
            PageRequest(limit=10)
        )
        assert page.items[0].outcome is AiOutcome.REFUSED

    async def test_a_refusal_stores_no_proposals(self, harness: _Harness) -> None:
        with pytest.raises(AiForbiddenError):
            await harness.extractor().execute(int(CLOSED_CONVERSATION))

        assert await harness.queued() == ()

    async def test_a_cloud_model_is_refused_for_a_local_only_chat(self, harness: _Harness) -> None:
        harness.provider = ScriptedAiProvider(model=CLOUD_MODEL)

        with pytest.raises(AiForbiddenError, match="local_only"):
            await harness.extractor().execute(int(CONVERSATION))

    async def test_a_timeout_propagates(self, harness: _Harness) -> None:
        harness.provider = ScriptedAiProvider(latency_seconds=5.0)
        harness.provider.script_answers(answer(proposal_payload()))

        with pytest.raises(AiTimeoutError):
            await _with_timeout(harness).execute(int(CONVERSATION))

    async def test_a_timeout_stores_nothing(self, harness: _Harness) -> None:
        harness.provider = ScriptedAiProvider(latency_seconds=5.0)

        with pytest.raises(AiTimeoutError):
            await _with_timeout(harness).execute(int(CONVERSATION))

        assert await harness.queued() == ()

    async def test_a_timeout_is_recorded(self, harness: _Harness) -> None:
        harness.provider = ScriptedAiProvider(latency_seconds=5.0)

        with pytest.raises(AiTimeoutError):
            await _with_timeout(harness).execute(int(CONVERSATION))

        page = await harness.calls(harness.unit_of_work(), ACCOUNT_A).list_recent(
            PageRequest(limit=10)
        )
        assert page.items[0].outcome is AiOutcome.TIMEOUT

    async def test_an_unknown_conversation_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No conversation"):
            await harness.extractor().execute(9999)

    async def test_an_unknown_conversation_reaches_no_model(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError):
            await harness.extractor().execute(9999)

        assert harness.provider.calls == 0


class _ImpatientTask(ExecuteAiTask):
    """An AI task that waits a very short time.

    A decorator rather than an argument on extraction: the timeout is
    ``ExecuteAiTask``'s to apply (ADR-057), and giving extraction one to pass
    through would put the same decision in two places.
    """

    def __init__(self, inner: ExecuteAiTask, seconds: float) -> None:
        self._inner = inner
        self._seconds = seconds

    async def execute(self, **kwargs: Any) -> Any:
        kwargs["timeout_seconds"] = self._seconds
        return await self._inner.execute(**kwargs)


def _with_timeout(harness: _Harness, seconds: float = 0.05) -> ExtractMemories:
    """Build an extractor whose model call times out almost immediately."""
    return ExtractMemories(
        harness.unit_of_work,
        harness.proposals_factory,
        harness.conversations,
        harness.messages,
        harness.accounts,
        StructuredAiTask(_ImpatientTask(harness.task(), seconds)),
        harness.registry,
        harness.clock,
        harness.ids,
        harness.policy,
        harness.events,
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class _FailsBeforeCommit(MemoryProposalRepository):
    """A repository that dies once every proposal has been written.

    The state the crash test needs: the rows are in the open transaction, and
    nothing has committed.
    """

    def __init__(self, inner: MemoryProposalRepository, *, after: int) -> None:
        self._inner = inner
        self._after = after
        self._written = 0

    @property
    def account_id(self) -> AccountId:
        return self._inner.account_id

    async def add(self, proposal: MemoryProposal) -> None:
        if self._written >= self._after:
            msg = "died here"
            raise RuntimeError(msg)
        await self._inner.add(proposal)
        self._written += 1

    async def get(self, proposal_id: MemoryProposalId) -> MemoryProposal | None:
        return await self._inner.get(proposal_id)

    async def list_recent(self, request: PageRequest) -> Any:
        return await self._inner.list_recent(request)

    async def list_for_conversation(
        self, conversation_id: ConversationId
    ) -> tuple[MemoryProposal, ...]:
        return await self._inner.list_for_conversation(conversation_id)

    async def decide(
        self, proposal_id: MemoryProposalId, status: ProposalStatus, now: datetime
    ) -> bool:
        return await self._inner.decide(proposal_id, status, now)


class TestTransactions:
    async def test_the_model_call_is_outside_every_transaction(self, harness: _Harness) -> None:
        # This application permits one transaction at a time (ADR-034), and a
        # model call takes seconds. Holding the lock across it would stop
        # everything else in the process, including the live update loop.
        harness.provider.script_answers(answer(proposal_payload()))
        open_during_call: list[bool] = []

        original = harness.provider.generate

        async def watching(request: Any) -> Any:
            open_during_call.append(any(unit.is_active for unit in harness.units))
            return await original(request)

        harness.provider.generate = watching  # type: ignore[method-assign]
        await harness.extractor().execute(int(CONVERSATION))

        assert open_during_call == [False]

    async def test_reading_commits_nothing_and_writing_commits_once(
        self, harness: _Harness
    ) -> None:
        # Two commits, and both are deliberate: ``ExecuteAiTask`` records the
        # call in its own transaction -- instrumentation that rolled back with
        # the work would lose exactly the expensive records -- and this use case
        # commits the proposals in another. The read transaction commits
        # nothing, because it wrote nothing.
        harness.provider.script_answers(answer(proposal_payload()))

        await harness.extractor().execute(int(CONVERSATION))

        assert sum(1 for unit in harness.units if unit.is_committed) == 2

    async def test_storing_nothing_commits_only_the_ai_call(self, harness: _Harness) -> None:
        # No proposals means no writing transaction at all: an empty extraction
        # is a read and a model call, and nothing else.
        harness.provider.script_answers(answer())

        await harness.extractor().execute(int(CONVERSATION))

        assert sum(1 for unit in harness.units if unit.is_committed) == 1


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


async def _prepare(container: Container) -> tuple[int, int]:
    """Create the schema, an account, a chat and one segmented conversation."""
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
                sender_kind=sender,
                sent_at=NOW + timedelta(minutes=index),
                text=body,
                message_type=MessageType.TEXT,
                telegram_message_id=10 + index,
            )
            for index, (sender, body) in enumerate(TRANSCRIPT)
        ],
    )
    page = await container.list_conversations().execute(int(chat.id))
    return int(chat.id), int(page.items[0].id)


async def _extractor(container: Container, provider: ScriptedAiProvider) -> ExtractMemories:
    """Build an extractor over a real database and a scripted model."""
    task = ExecuteAiTask(
        container.unit_of_work,
        container.ai_calls,
        container.chats,
        container.accounts,
        provider,
        container.clock,
        container.ids,
    )
    return ExtractMemories(
        container.unit_of_work,
        container.memory_proposals,
        container.conversations,
        container.messages,
        container.accounts,
        StructuredAiTask(task),
        container.prompts(),
        container.clock,
        container.ids,
        ExtractionPolicy(),
        container.events,
    )


@pytest.fixture
async def stored(container: Container) -> AsyncIterator[Container]:
    """A container over a real SQLite file."""
    try:
        yield container
    finally:
        await container.aclose()


class TestAgainstARealDatabase:
    async def test_a_proposal_round_trips(self, stored: Container) -> None:
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(proposal_payload()))

        report = await (await _extractor(stored, provider)).execute(conversation)

        found = await stored.get_memory_proposal().execute(int(report.proposed[0].id))
        assert found is not None
        assert found.value == "Lives in Lisbon"
        assert found.status is ProposalStatus.PENDING
        assert found.evidence.quote == "I moved to Lisbon last month"

    async def test_the_queue_lists_it(self, stored: Container) -> None:
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(proposal_payload()))
        await (await _extractor(stored, provider)).execute(conversation)

        page = await stored.list_memory_proposals().execute(PageRequest(limit=10))

        assert len(page.items) == 1

    async def test_running_twice_stores_nothing_new(self, stored: Container) -> None:
        # The property re-extraction rests on. The same conversation, the same
        # answer, no growth in the queue.
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(proposal_payload()), answer(proposal_payload()))
        extractor = await _extractor(stored, provider)
        await extractor.execute(conversation)

        second = await extractor.execute(conversation)

        assert second.stored == 0
        assert second.duplicates == 1
        page = await stored.list_memory_proposals().execute(PageRequest(limit=10))
        assert len(page.items) == 1

    async def test_the_unique_index_refuses_a_duplicate_the_check_missed(
        self, stored: Container
    ) -> None:
        # The backstop behind the duplicate check: the read and the write are
        # different transactions, so the index is what makes "one fact per
        # conversation" true rather than usually true.
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(proposal_payload()))
        await (await _extractor(stored, provider)).execute(conversation)

        account = await stored.get_account().execute(None)
        assert account is not None
        page = await stored.list_memory_proposals().execute(PageRequest(limit=10))
        existing = page.items[0]

        from tgassist.domain.errors import ConstraintViolationError  # noqa: PLC0415

        async with stored.unit_of_work() as uow:
            with pytest.raises(ConstraintViolationError):
                await stored.memory_proposals(uow, account.id).add(
                    MemoryProposal.propose(
                        proposal_id=MemoryProposalId(int(existing.id) + 1),
                        account_id=existing.account_id,
                        conversation_id=existing.conversation_id,
                        ai_call_id=existing.ai_call_id,
                        category=existing.category,
                        value=existing.value,
                        confidence=existing.confidence,
                        evidence=existing.evidence,
                        prompt=existing.prompt,
                        now=NOW,
                    )
                )

    async def test_the_proposal_and_its_ai_call_are_both_stored(self, stored: Container) -> None:
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(proposal_payload()))

        report = await (await _extractor(stored, provider)).execute(conversation)

        call = await stored.get_ai_call().execute(int(report.ai_call_id))
        assert call is not None
        assert call.task_kind == TASK_KIND

    async def test_an_exception_before_commit_persists_nothing(self, stored: Container) -> None:
        # The crash test. Two proposals are written into the open transaction
        # and the third attempt dies; the queue must be empty afterwards, not
        # two-thirds full.
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(
            answer(
                proposal_payload(),
                proposal_payload(
                    category="occupation",
                    value="Works at a design studio",
                    evidence="a job at a design studio",
                ),
                proposal_payload(
                    category="important_date",
                    value="Sister visits in September",
                    evidence="My sister visits in September",
                ),
            )
        )
        task = ExecuteAiTask(
            stored.unit_of_work,
            stored.ai_calls,
            stored.chats,
            stored.accounts,
            provider,
            stored.clock,
            stored.ids,
        )
        extractor = ExtractMemories(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(
                stored.memory_proposals(uow, account_id), after=2
            ),
            stored.conversations,
            stored.messages,
            stored.accounts,
            StructuredAiTask(task),
            stored.prompts(),
            stored.clock,
            stored.ids,
            ExtractionPolicy(),
            stored.events,
        )

        with pytest.raises(RuntimeError, match="died here"):
            await extractor.execute(conversation)

        page = await stored.list_memory_proposals().execute(PageRequest(limit=10))
        assert not page.items

    async def test_but_the_ai_call_survives_the_rollback(self, stored: Container) -> None:
        # Deliberate. The call happened and was paid for, in its own
        # transaction, and instrumentation that rolled back with the work would
        # lose exactly the expensive records.
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(proposal_payload()))
        task = ExecuteAiTask(
            stored.unit_of_work,
            stored.ai_calls,
            stored.chats,
            stored.accounts,
            provider,
            stored.clock,
            stored.ids,
        )
        extractor = ExtractMemories(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(
                stored.memory_proposals(uow, account_id), after=0
            ),
            stored.conversations,
            stored.messages,
            stored.accounts,
            StructuredAiTask(task),
            stored.prompts(),
            stored.clock,
            stored.ids,
            ExtractionPolicy(),
            stored.events,
        )

        with pytest.raises(RuntimeError):
            await extractor.execute(conversation)

        calls = await stored.list_ai_calls().execute(PageRequest(limit=10))
        assert len(calls.items) == 1

    async def test_a_failed_run_leaves_the_next_one_free_to_store(self, stored: Container) -> None:
        # The proof the rollback left nothing behind: a healthy run afterwards
        # stores the fact rather than calling it a duplicate.
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(proposal_payload()), answer(proposal_payload()))
        task = ExecuteAiTask(
            stored.unit_of_work,
            stored.ai_calls,
            stored.chats,
            stored.accounts,
            provider,
            stored.clock,
            stored.ids,
        )
        crashing = ExtractMemories(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(
                stored.memory_proposals(uow, account_id), after=0
            ),
            stored.conversations,
            stored.messages,
            stored.accounts,
            StructuredAiTask(task),
            stored.prompts(),
            stored.clock,
            stored.ids,
            ExtractionPolicy(),
            stored.events,
        )
        with pytest.raises(RuntimeError):
            await crashing.execute(conversation)

        report = await (await _extractor(stored, provider)).execute(conversation)

        assert report.stored == 1

    async def test_deterministic_replay(self, stored: Container) -> None:
        # The same conversation and the same answer produce the same proposals,
        # every time. Nothing in the pipeline outside the model samples
        # randomness.
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        payload = answer(
            proposal_payload(),
            proposal_payload(
                category="occupation",
                value="Works at a design studio",
                evidence="a job at a design studio",
            ),
        )
        provider.script_answers(payload, payload)
        extractor = await _extractor(stored, provider)

        first = await extractor.execute(conversation)
        page = await stored.list_memory_proposals().execute(PageRequest(limit=10))
        second = await extractor.execute(conversation)

        assert [(p.category, p.value, p.confidence) for p in first.proposed] == [
            (p.category, p.value, p.confidence) for p in reversed(page.items)
        ]
        assert second.stored == 0
        assert first.returned == 2

    async def test_the_same_request_is_sent_every_time(self, stored: Container) -> None:
        # The other half of replay: what the model is asked does not drift
        # between runs over unchanged data.
        _chat, conversation = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(), answer())
        extractor = await _extractor(stored, provider)

        await extractor.execute(conversation)
        await extractor.execute(conversation)

        assert provider.requests[0].content == provider.requests[1].content
        assert provider.requests[0].temperature == 0.0


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


@pytest.fixture
def conversation() -> str:
    """Create an account, a chat, three messages and one conversation."""
    return _cli_conversation()


@pytest.mark.usefixtures("cli_env", "conversation")
class TestMemoryCommands:
    """The queue, end to end."""

    def test_proposals_says_when_there_are_none(self) -> None:
        result = runner.invoke(app, ["memory", "proposals"])

        assert result.exit_code == 0, result.output
        assert "No proposals" in result.output

    def test_extract_reports_an_answer_it_cannot_read(self, conversation: str) -> None:
        # The whole pipeline, end to end, against the shipped fake -- which
        # answers prose rather than JSON. So this exercises validation, the one
        # repair, and the refusal to guess, and then says so plainly rather
        # than storing something it invented.
        result = runner.invoke(app, ["memory", "extract", conversation])

        assert result.exit_code != 0
        assert "could not be read" in result.output
        assert "AI_SCHEMA_VIOLATION" in result.output

    def test_and_stores_nothing_when_it_cannot(self, conversation: str) -> None:
        runner.invoke(app, ["memory", "extract", conversation])

        assert "No proposals" in _run_cli("memory", "proposals")

    def test_but_the_calls_are_still_recorded(self, conversation: str) -> None:
        # Two of them: the attempt and the repair. A failed extraction still
        # cost what it cost.
        runner.invoke(app, ["memory", "extract", conversation])

        assert "2 call(s)" in _run_cli("ai", "list")

    def test_show_reports_an_unknown_proposal(self) -> None:
        result = runner.invoke(app, ["memory", "proposal", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_extract_reports_an_unknown_conversation(self) -> None:
        result = runner.invoke(app, ["memory", "extract", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestTheQueueCommands:
    """Reading a stored proposal back through the command line.

    Seeded through the application rather than through a model: the shipped
    scripted provider answers prose, not JSON, and what is under test here is
    the queue's commands rather than extraction.
    """

    @pytest.fixture
    def seeded(self, cli_env: Path) -> None:  # noqa: ARG002 - the fixture sets the env
        """Create a conversation and store one proposal in it."""
        asyncio.run(_seed_proposal(int(_cli_conversation())))

    @pytest.mark.usefixtures("seeded")
    def test_a_stored_proposal_is_listed(self) -> None:
        listing = _run_cli("memory", "proposals")

        assert "Lives in Lisbon" in listing
        assert "pending" in listing

    @pytest.mark.usefixtures("seeded")
    def test_it_can_be_shown_with_its_evidence(self) -> None:
        # The evidence is the point of the command. A proposal you cannot check
        # is one you can only guess about.
        identifier = _run_cli("memory", "proposals").splitlines()[0].split()[0]

        shown = _run_cli("memory", "proposal", identifier)

        assert "status       pending" in shown
        assert "Read from:" in shown
        assert "I moved to Lisbon last month" in shown

    @pytest.mark.usefixtures("seeded")
    def test_it_names_its_provenance(self) -> None:
        identifier = _run_cli("memory", "proposals").splitlines()[0].split()[0]

        shown = _run_cli("memory", "proposal", identifier)

        assert "prompt       memory_extract@1.0.0" in shown
        assert "ai call" in shown


def _cli_conversation() -> str:
    """Create an account, a chat and a conversation through the CLI."""
    _run_cli("account", "create", "1001", "Primary")
    _run_cli("contact", "add", "2002", "Ada")
    contact_id = _run_cli("contact", "list").splitlines()[0].split()[0]
    _run_cli("chat", "open", "5000", "--contact", contact_id)
    chat_id = _run_cli("chat", "list").splitlines()[0].split()[0]
    for index, (sender, body) in enumerate(TRANSCRIPT):
        _run_cli(
            "message",
            "ingest",
            chat_id,
            body,
            "--from",
            sender.value,
            "--sent-at",
            (NOW + timedelta(minutes=index)).isoformat(),
        )
    return _run_cli("conversation", "list", chat_id).splitlines()[0].split()[0]


async def _seed_proposal(conversation_id: int) -> None:
    """Store one AI call and one proposal, the way extraction would."""
    container = Container.create()
    try:
        await container.start()
        account = await container.get_account().execute(None)
        assert account is not None
        found = await container.get_conversation().execute(conversation_id)
        assert found is not None
        conversation, _messages = found

        async with container.unit_of_work() as uow:
            call = AiCall.record(
                call_id=AiCallId(container.ids.new_id()),
                account_id=account.id,
                chat_id=conversation.chat_id,
                model=LOCAL_MODEL,
                prompt=PROMPT,
                task_kind=TASK_KIND,
                outcome=AiOutcome.SUCCESS,
                latency_ms=12,
                now=datetime.now(UTC),
                usage=TokenUsage(input_tokens=100, output_tokens=20),
                finish_reason=FinishReason.STOP,
                response="{}",
            )
            await container.ai_calls(uow, account.id).add(call)
            await container.memory_proposals(uow, account.id).add(
                MemoryProposal.propose(
                    proposal_id=MemoryProposalId(container.ids.new_id()),
                    account_id=account.id,
                    conversation_id=conversation.id,
                    ai_call_id=call.id,
                    category=MemoryCategory.LOCATION,
                    value="Lives in Lisbon",
                    confidence=Confidence(0.9),
                    evidence=Evidence("I moved to Lisbon last month"),
                    prompt=PROMPT,
                    now=datetime.now(UTC),
                )
            )
            await uow.commit()
    finally:
        await container.aclose()
