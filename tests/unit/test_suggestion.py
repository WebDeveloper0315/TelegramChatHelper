"""Generating a suggestion: the whole chain, and what it refuses to do.

Everything since Slice 9a meets here, so this file is about the joins rather
than the parts -- each of which is tested next door:

* the **pipeline** runs in order and each stage gets what the previous produced;
* **attribution** is checked against what was actually supplied, so a fabricated
  citation is caught rather than displayed;
* the **failures** behave: malformed output, a repair that works, a repair that
  does not, a timeout, and a chat that cannot be reached at all;
* **nothing is sent**, and nothing is written except the audit record every AI
  call writes.

No model is reached. Every provider here answers from a script.
"""

from __future__ import annotations

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
from tests.fakes.memory_repository import InMemoryMemoryRepository, InMemoryMemoryStore
from tests.fakes.message_repository import InMemoryMessageRepository, InMemoryMessageStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.ai import ExecuteAiTask, StructuredAiTask
from tgassist.application.use_cases.memory_context import (
    BuildMemoryContext,
    GetMemoryContext,
)
from tgassist.application.use_cases.message import IncomingMessage
from tgassist.application.use_cases.suggestion import (
    TASK_KIND,
    BuildPromptContext,
    GenerateConversationSuggestion,
)
from tgassist.domain.errors import (
    AiForbiddenError,
    AiTimeoutError,
    RecordNotFoundError,
    SchemaViolationError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.ai import AiOutcome
from tgassist.domain.model.chat import AiProcessingMode, Chat
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    ContactId,
    ConversationId,
    MemoryId,
    MemoryProposalId,
    MessageId,
    TelegramChatId,
    TelegramMessageId,
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
from tgassist.domain.model.message import Message, MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.domain.services.context_assembly import AssemblyRules
from tgassist.domain.services.memory_selection import SelectionRules
from tgassist.infrastructure.ai.scripted import CLOUD_MODEL, ScriptedAiProvider
from tgassist.infrastructure.prompts import FilePromptRegistry
from tgassist.presentation.cli.app import app

runner = CliRunner()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CONTACT = ContactId(101)
OTHER_CONTACT = ContactId(102)
CHAT = ChatId(11)
CLOSED_CHAT = ChatId(12)

FACTS = [
    (MemoryCategory.CONSTRAINT, "Do not bring up her old job"),
    (MemoryCategory.INTEREST, "Reads science fiction"),
]

TRANSCRIPT = [
    (SenderKind.CONTACT, "Finally unpacked the last box."),
    (SenderKind.OPERATOR, "How does the place feel?"),
    (SenderKind.CONTACT, "Good. Any recommendations for what to read next?"),
]


def answer(**overrides: Any) -> str:
    """Build what the model is supposed to return."""
    payload: dict[str, Any] = {
        "suggestion": "Glad it feels like home. Want a few science fiction ideas?",
        "confidence": 0.8,
        "used_memory_keys": ["reads science fiction"],
    }
    payload.update(overrides)
    return json.dumps(payload)


class _Harness:
    """A suggestion environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(OTHER_CONTACT): int(ACCOUNT_A)},
        )
        self.message_store = InMemoryMessageStore(
            chats={int(CHAT): int(ACCOUNT_A), int(CLOSED_CHAT): int(ACCOUNT_A)}
        )
        self.memory_store = InMemoryMemoryStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            contacts={int(CONTACT): int(ACCOUNT_A), int(OTHER_CONTACT): int(ACCOUNT_A)},
        )
        self.call_store = InMemoryAiCallStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            chats={int(CHAT): int(ACCOUNT_A), int(CLOSED_CHAT): int(ACCOUNT_A)},
        )
        self.clock = AdvanceableClock(NOW + timedelta(hours=1))
        self.ids = SequentialIdGenerator(start=900)
        self.provider = ScriptedAiProvider()
        self.registry = FilePromptRegistry()
        self.registry.load()
        self.selection_rules = SelectionRules()
        self.assembly_rules = AssemblyRules()
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

    def memories(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryMemoryRepository:
        return InMemoryMemoryRepository(self.memory_store, account_id)

    def calls(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryAiCallRepository:
        return InMemoryAiCallRepository(self.call_store, account_id)

    async def setup(self, *, with_messages: bool = True) -> None:
        """Create an account, two chats, some memories and a conversation."""
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

        await self.remember(*FACTS)
        if with_messages:
            await self.say(*TRANSCRIPT)

    async def remember(self, *facts: tuple[MemoryCategory, str]) -> None:
        """Store approved memories about the contact."""
        repository = self.memories(self.unit_of_work(), ACCOUNT_A)
        for index, (category, value) in enumerate(facts):
            await repository.add(
                Memory(
                    id=MemoryId(self.ids.new_id()),
                    account_id=ACCOUNT_A,
                    contact_id=CONTACT,
                    category=category,
                    key=MemoryKey.of(value),
                    value=value,
                    confidence=Confidence(0.8),
                    source=MemorySource.AI_APPROVED,
                    proposal_id=MemoryProposalId(500 + index),
                    conversation_id=ConversationId(301),
                    ai_call_id=AiCallId(401),
                    created_at=NOW + timedelta(minutes=index),
                    importance=Importance.normal(),
                )
            )

    async def say(self, *turns: tuple[SenderKind, str], chat_id: ChatId = CHAT) -> None:
        """Store messages in a chat, oldest first."""
        repository = self.messages(self.unit_of_work(), ACCOUNT_A)
        for index, (sender, text) in enumerate(turns):
            await repository.add(
                Message.record(
                    message_id=MessageId(self.ids.new_id()),
                    account_id=ACCOUNT_A,
                    chat_id=chat_id,
                    sender_kind=sender,
                    sent_at=NOW + timedelta(minutes=index),
                    ingested_at=NOW,
                    text=text,
                    message_type=MessageType.TEXT,
                    telegram_message_id=TelegramMessageId(1000 + index),
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

    def retrieval(self, *, record: bool = True) -> GetMemoryContext:
        kind = BuildMemoryContext if record else GetMemoryContext
        return kind(
            self.unit_of_work,
            self.memories,
            self.chats,
            self.accounts,
            self.clock,
            self.selection_rules,
        )

    def builder(self, *, record: bool = True) -> BuildPromptContext:
        return BuildPromptContext(
            self.unit_of_work,
            self.retrieval(record=record),
            self.messages,
            self.chats,
            self.accounts,
            self.registry,
            self.assembly_rules,
        )

    def generator(self) -> GenerateConversationSuggestion:
        return GenerateConversationSuggestion(
            self.builder(), StructuredAiTask(self.task()), self.registry
        )

    async def recorded_calls(self) -> list[Any]:
        page = await self.calls(self.unit_of_work(), ACCOUNT_A).list_recent(PageRequest(limit=50))
        return list(page.items)


@pytest.fixture
async def harness() -> _Harness:
    """One account, one chat with two memories and three messages."""
    built = _Harness()
    await built.setup()
    return built


# ---------------------------------------------------------------------------
# Assembling the prompt
# ---------------------------------------------------------------------------


class TestBuildingThePrompt:
    async def test_it_supplies_the_retrieved_memories(self, harness: _Harness) -> None:
        assembled = await harness.builder().execute(int(CHAT))

        assert len(assembled.memories) == len(FACTS)

    async def test_the_memories_are_in_retrieval_order(self, harness: _Harness) -> None:
        # The constraint outranks the interest, and assembly does not re-rank.
        assembled = await harness.builder().execute(int(CHAT))

        assert assembled.memories[0].category is MemoryCategory.CONSTRAINT

    async def test_it_includes_the_conversation_oldest_first(self, harness: _Harness) -> None:
        assembled = await harness.builder().execute(int(CHAT))

        assert [turn.text for turn in assembled.context.conversation.turns] == [
            text for _sender, text in TRANSCRIPT
        ]

    async def test_the_prompt_contains_both(self, harness: _Harness) -> None:
        assembled = await harness.builder().execute(int(CHAT))

        assert "Do not bring up her old job" in assembled.text
        assert "Any recommendations for what to read next?" in assembled.text

    async def test_the_conversation_is_delimited(self, harness: _Harness) -> None:
        # Wrapped by the prompt, which declares it untrusted (ADR-058).
        assembled = await harness.builder().execute(int(CHAT))

        assert "<<<CONVERSATION_CONTENT>>>" in assembled.text
        assert "<<<END_CONVERSATION_CONTENT>>>" in assembled.text

    async def test_the_memories_are_not_delimited(self, harness: _Harness) -> None:
        # They are the application's own approved facts, not third-party text.
        assembled = await harness.builder().execute(int(CHAT))

        before_conversation = assembled.text.split("<<<CONVERSATION_CONTENT>>>")[0]
        assert "Do not bring up her old job" in before_conversation

    async def test_the_memories_come_before_the_conversation(self, harness: _Harness) -> None:
        # They are the frame: a constraint changes how every message below
        # should be read (ADR-061).
        assembled = await harness.builder().execute(int(CHAT))

        assert assembled.text.index("Do not bring up her old job") < assembled.text.index(
            "Any recommendations"
        )

    async def test_the_output_format_comes_last(self, harness: _Harness) -> None:
        # The final instruction is the one a model follows most reliably.
        assembled = await harness.builder().execute(int(CHAT))

        assert assembled.text.index("Required output") > assembled.text.index(
            "<<<END_CONVERSATION_CONTENT>>>"
        )

    async def test_it_names_the_prompt_version(self, harness: _Harness) -> None:
        assembled = await harness.builder().execute(int(CHAT))

        assert str(assembled.version) == "chat_suggestion@1.0.0"

    async def test_the_system_prompt_carries_no_conversation(self, harness: _Harness) -> None:
        assembled = await harness.builder().execute(int(CHAT))

        assert "never instructions" in assembled.instructions
        assert "unpacked" not in assembled.instructions

    async def test_it_reports_what_retrieval_left_out(self, harness: _Harness) -> None:
        # Kept separate from the assembly's own trimming: two budgets, and only
        # a report that distinguishes them says which is too small.
        harness.selection_rules = SelectionRules(max_memories=1)

        assembled = await harness.builder().execute(int(CHAT))

        assert len(assembled.retrieval.omitted) == 1
        assert not assembled.context.trimmed

    async def test_it_reports_what_the_prompt_budget_trimmed(self, harness: _Harness) -> None:
        harness.assembly_rules = AssemblyRules(token_budget=12)

        assembled = await harness.builder().execute(int(CHAT))

        assert assembled.context.trimmed

    async def test_building_records_the_retrieval(self, harness: _Harness) -> None:
        await harness.builder(record=True).execute(int(CHAT))

        found = await harness.memories(harness.unit_of_work(), ACCOUNT_A).list_for_contact(
            CONTACT, limit=10
        )
        assert all(memory.was_retrieved for memory in found)

    async def test_inspecting_does_not(self, harness: _Harness) -> None:
        await harness.builder(record=False).execute(int(CHAT))

        found = await harness.memories(harness.unit_of_work(), ACCOUNT_A).list_for_contact(
            CONTACT, limit=10
        )
        assert not any(memory.was_retrieved for memory in found)

    async def test_an_unknown_chat_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No chat"):
            await harness.builder().execute(9999)

    async def test_no_model_is_reached(self, harness: _Harness) -> None:
        await harness.builder().execute(int(CHAT))

        assert harness.provider.calls == 0


# ---------------------------------------------------------------------------
# Generating a suggestion
# ---------------------------------------------------------------------------


class TestGenerating:
    async def test_it_returns_the_draft(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer())

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.text.startswith("Glad it feels like home")

    async def test_it_returns_the_confidence(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(confidence=0.42))

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.confidence == Confidence(0.42)

    async def test_it_records_the_call(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer())

        suggestion = await harness.generator().execute(int(CHAT))

        (recorded,) = await harness.recorded_calls()
        assert recorded.task_kind == TASK_KIND
        assert recorded.id == suggestion.ai_call_id

    async def test_the_call_records_the_prompt_version(self, harness: _Harness) -> None:
        # So a suggestion can be traced to the exact wording that produced it.
        harness.provider.script_answers(answer())

        await harness.generator().execute(int(CHAT))

        (recorded,) = await harness.recorded_calls()
        assert str(recorded.prompt) == "chat_suggestion@1.0.0"

    async def test_it_keeps_the_whole_prompt_for_inspection(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer())

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.prompt.text
        assert suggestion.prompt.memories

    async def test_nothing_is_sent(self, harness: _Harness) -> None:
        # There is no gateway here at all: the use case cannot reach Telegram
        # because it was never given anything that could.
        harness.provider.script_answers(answer())

        suggestion = await harness.generator().execute(int(CHAT))

        assert isinstance(suggestion.text, str)
        assert not hasattr(harness.generator(), "_gateway")

    async def test_a_chat_with_nothing_said_is_refused(self) -> None:
        # A suggestion for a conversation nobody has had is a guess.
        harness = _Harness()
        await harness.setup(with_messages=False)
        harness.provider.script_answers(answer())

        with pytest.raises(RecordNotFoundError, match="no messages to reply to"):
            await harness.generator().execute(int(CHAT))

        assert harness.provider.calls == 0

    async def test_a_disabled_chat_refuses(self, harness: _Harness) -> None:
        # The privacy gate is inherited from ExecuteAiTask, not reimplemented.
        await harness.say(*TRANSCRIPT, chat_id=CLOSED_CHAT)

        with pytest.raises(AiForbiddenError, match="switched off"):
            await harness.generator().execute(int(CLOSED_CHAT))

    async def test_a_cloud_model_is_refused_for_a_local_only_chat(self, harness: _Harness) -> None:
        harness.provider = ScriptedAiProvider(model=CLOUD_MODEL)
        harness.provider.script_answers(answer())

        with pytest.raises(AiForbiddenError, match="local_only"):
            await harness.generator().execute(int(CHAT))


class TestAttribution:
    async def test_a_reported_memory_is_matched(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(used_memory_keys=["reads science fiction"]))

        suggestion = await harness.generator().execute(int(CHAT))

        assert [m.value for m in suggestion.used_memories] == ["Reads science fiction"]
        assert suggestion.is_grounded

    async def test_reporting_none_is_a_correct_answer(self, harness: _Harness) -> None:
        # Many replies need no stored knowledge at all.
        harness.provider.script_answers(answer(used_memory_keys=[]))

        suggestion = await harness.generator().execute(int(CHAT))

        assert not suggestion.used_memories
        assert suggestion.is_grounded

    async def test_a_key_that_was_never_supplied_is_caught(self, harness: _Harness) -> None:
        # A suggestion that claims grounding it does not have is worse than one
        # that claims none, because the first invites trust (ADR-061).
        harness.provider.script_answers(answer(used_memory_keys=["has a dog called rex"]))

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.fabricated_keys == ("has a dog called rex",)
        assert not suggestion.is_grounded

    async def test_a_fabricated_key_is_not_shown_as_used(self, harness: _Harness) -> None:
        harness.provider.script_answers(
            answer(used_memory_keys=["reads science fiction", "invented"])
        )

        suggestion = await harness.generator().execute(int(CHAT))

        assert len(suggestion.used_memories) == 1
        assert suggestion.fabricated_keys == ("invented",)

    async def test_a_trimmed_memory_cannot_be_cited(self, harness: _Harness) -> None:
        # It was never supplied, so citing it is a fabrication -- which is
        # exactly right, and the reason attribution checks the *assembled* keys
        # rather than everything known.
        harness.selection_rules = SelectionRules(max_memories=1)
        harness.provider.script_answers(answer(used_memory_keys=["reads science fiction"]))

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.fabricated_keys == ("reads science fiction",)

    async def test_a_repeated_key_is_reported_once(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(used_memory_keys=["nope", "nope"]))

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.fabricated_keys == ("nope",)


class TestWhenTheAnswerIsWrong:
    async def test_prose_triggers_a_repair(self, harness: _Harness) -> None:
        harness.provider.script_answers("I think you should say hello.", answer())

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.repaired
        assert suggestion.text.startswith("Glad it feels")

    async def test_a_missing_field_triggers_a_repair(self, harness: _Harness) -> None:
        broken = json.loads(answer())
        del broken["confidence"]
        harness.provider.script_answers(json.dumps(broken), answer())

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.repaired

    async def test_an_extra_field_triggers_a_repair(self, harness: _Harness) -> None:
        # The model cannot name a recipient, an action or a chat: there is no
        # field for one, so the attempt fails validation rather than being
        # ignored.
        harness.provider.script_answers(answer(send_to="+441234567890"), answer())

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.repaired

    async def test_a_confidence_out_of_range_triggers_a_repair(self, harness: _Harness) -> None:
        harness.provider.script_answers(answer(confidence=5), answer())

        suggestion = await harness.generator().execute(int(CHAT))

        assert suggestion.repaired

    async def test_a_second_failure_raises(self, harness: _Harness) -> None:
        # Exactly one repair, and it is the shared rule rather than this use
        # case's own (ADR-061).
        harness.provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError, match="after one repair"):
            await harness.generator().execute(int(CHAT))

    async def test_a_second_failure_makes_exactly_two_calls(self, harness: _Harness) -> None:
        harness.provider.script_answers("nonsense", "still nonsense", answer())

        with pytest.raises(SchemaViolationError):
            await harness.generator().execute(int(CHAT))

        assert harness.provider.calls == 2

    async def test_both_calls_are_still_recorded(self, harness: _Harness) -> None:
        harness.provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError):
            await harness.generator().execute(int(CHAT))

        recorded = await harness.recorded_calls()
        assert len(recorded) == 2
        assert all(call.outcome is AiOutcome.SUCCESS for call in recorded)

    async def test_the_error_carries_no_model_output(self, harness: _Harness) -> None:
        harness.provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError) as excinfo:
            await harness.generator().execute(int(CHAT))

        assert "still nonsense" not in str(excinfo.value.context)

    async def test_a_timeout_propagates(self, harness: _Harness) -> None:
        harness.provider = ScriptedAiProvider(latency_seconds=5.0)
        harness.provider.script_answers(answer())

        with pytest.raises(AiTimeoutError):
            await _impatient(harness).execute(int(CHAT))

    async def test_a_timeout_is_recorded(self, harness: _Harness) -> None:
        harness.provider = ScriptedAiProvider(latency_seconds=5.0)

        with pytest.raises(AiTimeoutError):
            await _impatient(harness).execute(int(CHAT))

        (recorded,) = await harness.recorded_calls()
        assert recorded.outcome is AiOutcome.TIMEOUT


class _ImpatientTask(ExecuteAiTask):
    """An AI task that waits a very short time."""

    def __init__(self, inner: ExecuteAiTask, seconds: float) -> None:
        self._inner = inner
        self._seconds = seconds

    async def execute(self, **kwargs: Any) -> Any:
        kwargs["timeout_seconds"] = self._seconds
        return await self._inner.execute(**kwargs)


def _impatient(harness: _Harness, seconds: float = 0.05) -> GenerateConversationSuggestion:
    """Build a generator whose model call times out almost immediately."""
    return GenerateConversationSuggestion(
        harness.builder(),
        StructuredAiTask(_ImpatientTask(harness.task(), seconds)),
        harness.registry,
    )


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


async def _prepare(container: Container) -> int:
    """Create an account, a contact, a chat, memories and a conversation."""
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
                text=text,
                message_type=MessageType.TEXT,
                telegram_message_id=10 + index,
            )
            for index, (sender, text) in enumerate(TRANSCRIPT)
        ],
    )

    account = await container.get_account().execute(None)
    assert account is not None
    async with container.unit_of_work() as uow:
        repository = container.memories(uow, account.id)
        for index, (category, value) in enumerate(FACTS):
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
    return int(chat.id)


async def _generator(
    container: Container, provider: ScriptedAiProvider
) -> GenerateConversationSuggestion:
    """Build a generator over a real database and a scripted model."""
    task = ExecuteAiTask(
        container.unit_of_work,
        container.ai_calls,
        container.chats,
        container.accounts,
        provider,
        container.clock,
        container.ids,
    )
    return GenerateConversationSuggestion(
        container.build_prompt_context(), StructuredAiTask(task), container.prompts()
    )


@pytest.fixture
async def stored(container: Container) -> AsyncIterator[Container]:
    """A container over a real SQLite file."""
    try:
        yield container
    finally:
        await container.aclose()


class TestAgainstARealDatabase:
    async def test_a_suggestion_is_produced(self, stored: Container) -> None:
        chat_id = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer())

        suggestion = await (await _generator(stored, provider)).execute(chat_id)

        assert suggestion.text.startswith("Glad it feels like home")
        assert len(suggestion.prompt.memories) == len(FACTS)

    async def test_the_prompt_survives_the_database(self, stored: Container) -> None:
        # Everything in it made a round trip: the memories, their keys, the
        # messages and their order.
        chat_id = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer())

        suggestion = await (await _generator(stored, provider)).execute(chat_id)

        assert "Do not bring up her old job" in suggestion.prompt.text
        assert "Any recommendations for what to read next?" in suggestion.prompt.text

    async def test_the_call_is_recorded(self, stored: Container) -> None:
        chat_id = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer())

        suggestion = await (await _generator(stored, provider)).execute(chat_id)

        call = await stored.get_ai_call().execute(int(suggestion.ai_call_id))
        assert call is not None
        assert call.task_kind == TASK_KIND

    async def test_generating_records_the_retrieval(self, stored: Container) -> None:
        # A generation is a real use of the memories, unlike an inspection.
        chat_id = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer())

        await (await _generator(stored, provider)).execute(chat_id)

        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert all(memory.retrieval_count == 1 for memory in page.items)

    async def test_two_identical_runs_send_the_same_prompt(self, stored: Container) -> None:
        # Deterministic against real data, which is what makes a prompt version
        # comparable to the next one.
        chat_id = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers(answer(), answer())
        generator = await _generator(stored, provider)

        await generator.execute(chat_id)
        await generator.execute(chat_id)

        assert provider.requests[0].content == provider.requests[1].content
        assert provider.requests[0].instructions == provider.requests[1].instructions

    async def test_a_failed_generation_still_records_its_calls(self, stored: Container) -> None:
        # The rollback case: the task fails after two paid calls, and the audit
        # records survive because ExecuteAiTask commits each one in its own
        # transaction (ADR-057).
        chat_id = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError):
            await (await _generator(stored, provider)).execute(chat_id)

        page = await stored.list_ai_calls().execute(PageRequest(limit=10))
        assert len(page.items) == 2

    async def test_but_a_failed_generation_writes_nothing_else(self, stored: Container) -> None:
        # No suggestion is stored anywhere -- there is no table for one, which
        # is the strongest form of "nothing was kept".
        chat_id = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError):
            await (await _generator(stored, provider)).execute(chat_id)

        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert all(memory.is_active for memory in page.items)

    async def test_the_retrieval_is_recorded_even_when_generation_fails(
        self, stored: Container
    ) -> None:
        # Deliberate: the memories *were* used -- they were assembled into a
        # prompt and paid for -- whether or not the model then answered
        # usefully (ADR-060).
        chat_id = await _prepare(stored)
        provider = ScriptedAiProvider()
        provider.script_answers("nonsense", "still nonsense")

        with pytest.raises(SchemaViolationError):
            await (await _generator(stored, provider)).execute(chat_id)

        page = await stored.list_memories().execute(PageRequest(limit=10))
        assert all(memory.retrieval_count == 1 for memory in page.items)


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


@pytest.mark.usefixtures("cli_env")
class TestSuggestCommand:
    """The command, end to end, against the shipped fake."""

    @pytest.fixture
    def chat(self) -> str:
        """Create a chat with memories and a conversation."""
        import asyncio  # noqa: PLC0415

        async def seed() -> int:
            container = Container.create()
            try:
                return await _prepare(container)
            finally:
                await container.aclose()

        return str(asyncio.run(seed()))

    def test_it_reports_an_answer_it_cannot_read(self, chat: str) -> None:
        # The shipped fake answers prose, so this exercises assembly, the call,
        # validation and the one repair -- and then says so plainly rather than
        # inventing a suggestion.
        result = runner.invoke(app, ["chat", "suggest", chat])

        assert result.exit_code != 0
        assert "could not be read" in result.output

    def test_the_calls_are_still_recorded(self, chat: str) -> None:
        runner.invoke(app, ["chat", "suggest", chat])

        listing = runner.invoke(app, ["ai", "list"])
        assert "suggest_reply" in listing.output

    def test_an_unknown_chat_is_reported(self, chat: str) -> None:  # noqa: ARG002
        result = runner.invoke(app, ["chat", "suggest", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_a_chat_with_nothing_said_is_reported(self, chat: str) -> None:  # noqa: ARG002
        runner.invoke(app, ["contact", "add", "3003", "Bob"])
        contacts = runner.invoke(app, ["contact", "list"])
        other = contacts.output.splitlines()[0].split()[0]
        runner.invoke(app, ["chat", "open", "6000", "--contact", other])
        chats = runner.invoke(app, ["chat", "list"])
        empty = chats.output.splitlines()[0].split()[0]

        result = runner.invoke(app, ["chat", "suggest", empty])

        assert result.exit_code != 0
        assert "nothing in that chat" in result.output
