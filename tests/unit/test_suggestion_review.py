"""Reviewing suggestions: deciding, and what deciding does not do.

Three things this file is for:

* **the six decision cases** -- accept, dismiss, accept twice, dismiss twice,
  accept a dismissed one, dismiss an accepted one;
* **the transaction** -- a decision that failed to commit did not happen, and a
  decision that committed is announced;
* **that accepting executes nothing** -- asserted structurally, because a test
  that merely observes no message being sent would keep passing on the day
  somebody wires one up.

No model is called anywhere here. Deciding needs no AI, which is the point of
having separated generation from review.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock, InMemorySecretStore, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.event_bus import RecordingEventBus
from tests.fakes.suggestion_repository import (
    InMemorySuggestionRepository,
    InMemorySuggestionStore,
)
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.container import Container
from tgassist.application.use_cases.account import CreateAccountRequest
from tgassist.application.use_cases.ai import ExecuteAiTask, StructuredAiTask
from tgassist.application.use_cases.message import IncomingMessage
from tgassist.application.use_cases.suggestion import GenerateConversationSuggestion
from tgassist.application.use_cases.suggestion_review import (
    AcceptSuggestion,
    DismissSuggestion,
    GetSuggestion,
    ListSuggestions,
)
from tgassist.domain.errors import (
    DomainValidationError,
    InvalidStateTransitionError,
    RecordNotFoundError,
)
from tgassist.domain.events import (
    SuggestionAccepted,
    SuggestionDismissed,
    SuggestionsCreated,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import AiProcessingMode
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    SuggestionId,
    TelegramUserId,
)
from tgassist.domain.model.message import MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.suggestion import ProposalType, Suggestion, SuggestionStatus
from tgassist.domain.ports.suggestion_repository import SuggestionRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.ai.scripted import ScriptedAiProvider
from tgassist.presentation.cli.app import app

runner = CliRunner()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)

ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CHAT = ChatId(11)
OTHER_CHAT = ChatId(12)
CALL = AiCallId(401)

ANSWER = (
    '{"suggestion": "Glad it feels like home. Want a few book ideas?", '
    '"confidence": 0.8, "used_memory_keys": []}'
)


def make_suggestion(**overrides: Any) -> Suggestion:
    """Build a pending suggestion."""
    values: dict[str, Any] = {
        "suggestion_id": SuggestionId(1),
        "account_id": ACCOUNT_A,
        "chat_id": CHAT,
        "ai_call_id": CALL,
        "proposal_type": ProposalType.REPLY_DRAFT,
        "title": "Reply about the move",
        "description": "Glad it feels like home. Want a few book ideas?",
        "payload": {"confidence": 0.8},
        "now": NOW,
    }
    values.update(overrides)
    return Suggestion.draft(**values)


# ---------------------------------------------------------------------------
# The aggregate
# ---------------------------------------------------------------------------


class TestTheAggregate:
    def test_a_suggestion_starts_pending(self) -> None:
        assert make_suggestion().status is SuggestionStatus.PENDING

    def test_there_is_no_way_to_create_a_decided_one(self) -> None:
        # The factory takes no status. A suggestion created already accepted
        # would be a decision nobody made.
        with pytest.raises(TypeError):
            make_suggestion(status=SuggestionStatus.ACCEPTED)

    def test_it_can_be_accepted(self) -> None:
        decided = make_suggestion().decided(SuggestionStatus.ACCEPTED, LATER)

        assert decided.was_accepted
        assert decided.decided_at == LATER

    def test_and_dismissed(self) -> None:
        decided = make_suggestion().decided(SuggestionStatus.DISMISSED, LATER)

        assert decided.status is SuggestionStatus.DISMISSED
        assert not decided.was_accepted

    def test_deciding_twice_is_refused(self) -> None:
        decided = make_suggestion().decided(SuggestionStatus.ACCEPTED, LATER)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            decided.decided(SuggestionStatus.DISMISSED, LATER)

    def test_there_is_no_way_back_to_pending(self) -> None:
        with pytest.raises(InvalidStateTransitionError, match="not a decision"):
            make_suggestion().decided(SuggestionStatus.PENDING, LATER)

    def test_it_has_exactly_one_transition(self) -> None:
        changing = [
            name
            for name in dir(Suggestion)
            if not name.startswith("_") and callable(getattr(Suggestion, name, None))
        ]

        assert changing == ["decided", "details", "draft"]

    def test_a_decision_cannot_precede_the_suggestion(self) -> None:
        with pytest.raises(DomainValidationError, match="before it was made"):
            make_suggestion().decided(SuggestionStatus.ACCEPTED, NOW - timedelta(hours=1))

    def test_a_payload_that_is_not_json_is_refused(self) -> None:
        from dataclasses import replace  # noqa: PLC0415

        with pytest.raises(DomainValidationError, match="must be JSON"):
            replace(make_suggestion(), payload="not json")

    def test_a_payload_that_is_not_an_object_is_refused(self) -> None:
        # Whatever eventually reads it can then rely on that much without a
        # schema of its own.
        from dataclasses import replace  # noqa: PLC0415

        with pytest.raises(DomainValidationError, match="must be a JSON object"):
            replace(make_suggestion(), payload="[1, 2]")

    def test_the_payload_round_trips(self) -> None:
        assert make_suggestion().details() == {"confidence": 0.8}

    def test_a_blank_description_is_refused(self) -> None:
        # A reviewer decides by reading this; there is nothing to decide about
        # without it.
        with pytest.raises(DomainValidationError, match="must have a description"):
            make_suggestion(description="   ")


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class _Harness:
    """A review environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.store = InMemorySuggestionStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)},
            chats={int(CHAT): int(ACCOUNT_A), int(OTHER_CHAT): int(ACCOUNT_A)},
            calls={int(CALL): int(ACCOUNT_A)},
        )
        self.clock = AdvanceableClock(LATER)
        self.ids = SequentialIdGenerator(start=900)
        self.events = RecordingEventBus()
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()
        self.suggestions_factory: Any = self.suggestions

    def unit_of_work(self) -> InMemoryUnitOfWork:
        uow = self._factory()
        self.units.append(uow)
        return uow

    def accounts(self, _uow: UnitOfWork) -> InMemoryAccountRepository:
        return self.accounts_repository

    def suggestions(self, _uow: UnitOfWork, account_id: AccountId) -> InMemorySuggestionRepository:
        return InMemorySuggestionRepository(self.store, account_id)

    async def setup(self) -> Suggestion:
        """Create an account and one pending suggestion."""
        await self.accounts_repository.add(
            Account.create(
                account_id=ACCOUNT_A,
                telegram_user_id=TelegramUserId(1001),
                display_name="me",
                now=NOW,
                is_active=True,
            )
        )
        suggestion = make_suggestion()
        await self.suggestions(self.unit_of_work(), ACCOUNT_A).add(suggestion)
        return suggestion

    async def queue(self, **overrides: Any) -> Suggestion:
        """Add another pending suggestion."""
        suggestion = make_suggestion(**overrides)
        await self.suggestions(self.unit_of_work(), ACCOUNT_A).add(suggestion)
        return suggestion

    def accept(self) -> AcceptSuggestion:
        return AcceptSuggestion(
            self.unit_of_work,
            self.suggestions_factory,
            self.accounts,
            self.clock,
            self.events,
        )

    def dismiss(self) -> DismissSuggestion:
        return DismissSuggestion(
            self.unit_of_work,
            self.suggestions_factory,
            self.accounts,
            self.clock,
            self.events,
        )

    def read(self) -> GetSuggestion:
        return GetSuggestion(self.unit_of_work, self.suggestions_factory, self.accounts)

    def listing(self) -> ListSuggestions:
        return ListSuggestions(self.unit_of_work, self.suggestions_factory, self.accounts)

    async def stored(self, suggestion_id: int = 1) -> Suggestion | None:
        return await self.suggestions(self.unit_of_work(), ACCOUNT_A).get(
            SuggestionId(suggestion_id)
        )


@pytest.fixture
async def harness() -> _Harness:
    """One account and one pending suggestion."""
    built = _Harness()
    await built.setup()
    return built


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


class TestAccepting:
    async def test_it_records_agreement(self, harness: _Harness) -> None:
        decided = await harness.accept().execute(1)

        assert decided.was_accepted
        assert decided.decided_at == LATER

    async def test_the_stored_suggestion_changes(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        stored = await harness.stored()
        assert stored is not None
        assert stored.status is SuggestionStatus.ACCEPTED

    async def test_it_is_one_transaction(self, harness: _Harness) -> None:
        before = sum(1 for unit in harness.units if unit.is_committed)

        await harness.accept().execute(1)

        assert sum(1 for unit in harness.units if unit.is_committed) == before + 1

    async def test_it_publishes_the_decision(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        (published,) = harness.events.events_of(SuggestionAccepted)
        assert isinstance(published, SuggestionAccepted)
        assert published.suggestion_id == 1
        assert published.chat_id == int(CHAT)
        assert published.proposal_type == ProposalType.REPLY_DRAFT.value

    async def test_it_leaves_the_queue(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        page = await harness.listing().execute(PageRequest(limit=10))

        assert not page.items

    async def test_an_unknown_suggestion_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No suggestion"):
            await harness.accept().execute(9999)

    async def test_another_accounts_suggestion_is_invisible(self, harness: _Harness) -> None:
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


class TestDismissing:
    async def test_it_records_the_refusal(self, harness: _Harness) -> None:
        decided = await harness.dismiss().execute(1)

        assert decided.status is SuggestionStatus.DISMISSED

    async def test_the_suggestion_is_kept(self, harness: _Harness) -> None:
        # A record of only what was agreed with cannot show what the generator
        # is getting wrong.
        await harness.dismiss().execute(1)

        assert await harness.stored() is not None

    async def test_it_publishes_the_decision(self, harness: _Harness) -> None:
        await harness.dismiss().execute(1)

        assert len(harness.events.events_of(SuggestionDismissed)) == 1
        assert harness.events.events_of(SuggestionAccepted) == []

    async def test_it_leaves_the_queue(self, harness: _Harness) -> None:
        await harness.dismiss().execute(1)

        assert not (await harness.listing().execute(PageRequest(limit=10))).items

    async def test_it_stays_in_the_chat_history(self, harness: _Harness) -> None:
        await harness.dismiss().execute(1)

        page = await harness.listing().execute(PageRequest(limit=10), chat_id=int(CHAT))

        assert [int(s.id) for s in page.items] == [1]


class TestDecidingTwice:
    """The six cases, and none of them changes anything."""

    async def test_accepting_twice_is_refused(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            await harness.accept().execute(1)

    async def test_dismissing_twice_is_refused(self, harness: _Harness) -> None:
        await harness.dismiss().execute(1)

        with pytest.raises(InvalidStateTransitionError, match="already dismissed"):
            await harness.dismiss().execute(1)

    async def test_accepting_a_dismissed_one_is_refused(self, harness: _Harness) -> None:
        await harness.dismiss().execute(1)

        with pytest.raises(InvalidStateTransitionError, match="already dismissed"):
            await harness.accept().execute(1)

    async def test_dismissing_an_accepted_one_is_refused(self, harness: _Harness) -> None:
        await harness.accept().execute(1)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            await harness.dismiss().execute(1)

    async def test_the_first_decision_stands(self, harness: _Harness) -> None:
        await harness.accept().execute(1)
        with pytest.raises(InvalidStateTransitionError):
            await harness.dismiss().execute(1)

        stored = await harness.stored()
        assert stored is not None
        assert stored.status is SuggestionStatus.ACCEPTED
        assert stored.decided_at == LATER

    async def test_a_refused_decision_publishes_nothing(self, harness: _Harness) -> None:
        await harness.accept().execute(1)
        with pytest.raises(InvalidStateTransitionError):
            await harness.dismiss().execute(1)

        assert harness.events.events_of(SuggestionDismissed) == []


class TestAcceptingExecutesNothing:
    """The guarantee this slice exists for, asserted structurally."""

    def test_the_use_case_holds_nothing_that_could_act(self) -> None:
        # A test that merely observed no message being sent would keep passing
        # on the day somebody wired one up. This asserts the shape instead: the
        # use case is given nothing capable of acting (ADR-062).
        parameters = set(inspect.signature(AcceptSuggestion.__init__).parameters)

        assert parameters == {
            "self",
            "unit_of_work",
            "suggestions",
            "accounts",
            "clock",
            "events",
        }

    def test_it_has_no_gateway_and_no_scheduler(self, harness: _Harness) -> None:
        accept = harness.accept()

        for forbidden in ("_gateway", "_telegram", "_scheduler", "_executor", "_provider"):
            assert not hasattr(accept, forbidden)

    async def test_accepting_changes_only_the_status(self, harness: _Harness) -> None:
        from dataclasses import replace  # noqa: PLC0415

        before = await harness.stored()
        assert before is not None

        after = await harness.accept().execute(1)

        assert replace(after, status=before.status, decided_at=before.decided_at) == before


class TestReading:
    async def test_a_suggestion_can_be_read_back(self, harness: _Harness) -> None:
        found = await harness.read().execute(1)

        assert found is not None
        assert found.title == "Reply about the move"

    async def test_a_decided_one_can_still_be_read(self, harness: _Harness) -> None:
        # "What did I dismiss last week" is a question a person is entitled to
        # ask, and the measurement that says whether the generator earns its
        # cost.
        await harness.dismiss().execute(1)

        found = await harness.read().execute(1)

        assert found is not None
        assert found.status is SuggestionStatus.DISMISSED

    async def test_an_absent_one_is_none(self, harness: _Harness) -> None:
        assert await harness.read().execute(9999) is None

    async def test_the_queue_is_newest_first(self, harness: _Harness) -> None:
        await harness.queue(suggestion_id=SuggestionId(2), now=NOW + timedelta(minutes=10))

        page = await harness.listing().execute(PageRequest(limit=10))

        assert [int(s.id) for s in page.items] == [2, 1]

    async def test_a_chat_filter_includes_decided_ones(self, harness: _Harness) -> None:
        await harness.queue(suggestion_id=SuggestionId(2), chat_id=OTHER_CHAT)
        await harness.accept().execute(1)

        page = await harness.listing().execute(PageRequest(limit=10), chat_id=int(CHAT))

        assert [int(s.id) for s in page.items] == [1]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class _FailsBeforeCommit(SuggestionRepository):
    """A suggestion repository that dies at a chosen point in a decision."""

    def __init__(self, inner: SuggestionRepository, *, on_decide: bool = True) -> None:
        self._inner = inner
        self._on_decide = on_decide

    @property
    def account_id(self) -> AccountId:
        return self._inner.account_id

    async def add(self, suggestion: Suggestion) -> None:
        await self._inner.add(suggestion)

    async def get(self, suggestion_id: SuggestionId) -> Suggestion | None:
        return await self._inner.get(suggestion_id)

    async def list_pending(self, request: PageRequest) -> Any:
        return await self._inner.list_pending(request)

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Any:
        return await self._inner.list_by_chat(chat_id, request)

    async def decide(
        self, suggestion_id: SuggestionId, status: SuggestionStatus, now: datetime
    ) -> bool:
        if not self._on_decide:
            # Writes first, then dies: the decision is in the open transaction
            # when the failure lands.
            await self._inner.decide(suggestion_id, status, now)
        msg = "died here"
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


async def _prepare(container: Container) -> tuple[int, int]:
    """Generate one suggestion through the real pipeline, and return its ids."""
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
                text="Any recommendations for what to read next?",
                message_type=MessageType.TEXT,
                telegram_message_id=10,
            )
        ],
    )

    provider = ScriptedAiProvider()
    provider.script_answers(ANSWER)
    task = ExecuteAiTask(
        container.unit_of_work,
        container.ai_calls,
        container.chats,
        container.accounts,
        provider,
        container.clock,
        container.ids,
    )
    generator = GenerateConversationSuggestion(
        container.build_prompt_context(),
        StructuredAiTask(task),
        container.prompts(),
        container.unit_of_work,
        container.suggestions,
        container.accounts,
        container.clock,
        container.ids,
        container.events,
    )
    generated = await generator.execute(int(chat.id))
    assert generated.suggestion_id is not None
    return int(generated.suggestion_id), int(chat.id)


@pytest.fixture
async def stored(container: Container) -> AsyncIterator[Container]:
    """A container over a real SQLite file."""
    try:
        yield container
    finally:
        await container.aclose()


class TestAgainstARealDatabase:
    async def test_generation_queues_the_draft(self, stored: Container) -> None:
        # The property this slice exists for: a suggestion is reviewable after
        # the command that made it has finished.
        suggestion_id, _chat = await _prepare(stored)

        found = await stored.get_suggestion().execute(suggestion_id)

        assert found is not None
        assert found.is_pending
        assert found.description.startswith("Glad it feels like home")

    async def test_the_draft_carries_its_provenance(self, stored: Container) -> None:
        suggestion_id, chat_id = await _prepare(stored)

        found = await stored.get_suggestion().execute(suggestion_id)

        assert found is not None
        assert int(found.chat_id) == chat_id
        call = await stored.get_ai_call().execute(int(found.ai_call_id))
        assert call is not None
        assert call.task_kind == "suggest_reply"

    async def test_the_payload_holds_what_a_person_does_not_read(self, stored: Container) -> None:
        suggestion_id, _chat = await _prepare(stored)

        found = await stored.get_suggestion().execute(suggestion_id)

        assert found is not None
        details = found.details()
        assert details["prompt_version"] == "chat_suggestion@1.0.0"
        assert details["confidence"] == 0.8

    async def test_accepting_is_recorded(self, stored: Container) -> None:
        suggestion_id, _chat = await _prepare(stored)

        await stored.accept_suggestion().execute(suggestion_id)

        found = await stored.get_suggestion().execute(suggestion_id)
        assert found is not None
        assert found.was_accepted
        assert found.decided_at is not None

    async def test_accepting_twice_is_refused(self, stored: Container) -> None:
        suggestion_id, _chat = await _prepare(stored)
        await stored.accept_suggestion().execute(suggestion_id)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            await stored.accept_suggestion().execute(suggestion_id)

    async def test_dismissing_an_accepted_one_is_refused(self, stored: Container) -> None:
        suggestion_id, _chat = await _prepare(stored)
        await stored.accept_suggestion().execute(suggestion_id)

        with pytest.raises(InvalidStateTransitionError, match="already accepted"):
            await stored.dismiss_suggestion().execute(suggestion_id)

    async def test_a_decided_suggestion_leaves_the_queue(self, stored: Container) -> None:
        suggestion_id, _chat = await _prepare(stored)

        await stored.dismiss_suggestion().execute(suggestion_id)

        page = await stored.list_suggestions().execute(PageRequest(limit=10))
        assert not page.items

    async def test_an_exception_before_commit_persists_nothing(self, stored: Container) -> None:
        suggestion_id, _chat = await _prepare(stored)
        accept = AcceptSuggestion(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(stored.suggestions(uow, account_id)),
            stored.accounts,
            stored.clock,
            stored.events,
        )

        with pytest.raises(RuntimeError, match="died here"):
            await accept.execute(suggestion_id)

        found = await stored.get_suggestion().execute(suggestion_id)
        assert found is not None
        assert found.is_pending

    async def test_an_exception_after_the_decision_persists_nothing(
        self, stored: Container
    ) -> None:
        # The decision is written into the open transaction when the failure
        # lands, and does not survive.
        suggestion_id, _chat = await _prepare(stored)
        accept = AcceptSuggestion(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(
                stored.suggestions(uow, account_id), on_decide=False
            ),
            stored.accounts,
            stored.clock,
            stored.events,
        )

        with pytest.raises(RuntimeError, match="died here"):
            await accept.execute(suggestion_id)

        found = await stored.get_suggestion().execute(suggestion_id)
        assert found is not None
        assert found.is_pending
        assert found.decided_at is None

    async def test_a_failed_decision_can_be_retried(self, stored: Container) -> None:
        suggestion_id, _chat = await _prepare(stored)
        accept = AcceptSuggestion(
            stored.unit_of_work,
            lambda uow, account_id: _FailsBeforeCommit(stored.suggestions(uow, account_id)),
            stored.accounts,
            stored.clock,
            stored.events,
        )
        with pytest.raises(RuntimeError):
            await accept.execute(suggestion_id)

        decided = await stored.accept_suggestion().execute(suggestion_id)

        assert decided.was_accepted

    async def test_the_conditional_update_refuses_a_second_decision(
        self, stored: Container
    ) -> None:
        # Straight at the repository, bypassing the entity's check, to prove
        # the SQL half of the guarantee holds on its own.
        suggestion_id, _chat = await _prepare(stored)
        account = await stored.get_account().execute(None)
        assert account is not None

        # The container's clock is real, so the decision must be too -- a
        # suggestion cannot be decided before it was made.
        decided_at = datetime.now(UTC)
        async with stored.unit_of_work() as uow:
            repository = stored.suggestions(uow, account.id)
            assert await repository.decide(
                SuggestionId(suggestion_id), SuggestionStatus.ACCEPTED, decided_at
            )
            assert not await repository.decide(
                SuggestionId(suggestion_id), SuggestionStatus.DISMISSED, decided_at
            )
            await uow.commit()

        found = await stored.get_suggestion().execute(suggestion_id)
        assert found is not None
        assert found.was_accepted

    async def test_generation_publishes_the_creation(self, stored: Container) -> None:
        recorded: list[SuggestionsCreated] = []

        def remember(event: SuggestionsCreated) -> None:
            recorded.append(event)

        stored.events.subscribe(SuggestionsCreated, remember, name="test")

        suggestion_id, _chat = await _prepare(stored)

        assert len(recorded) == 1
        assert recorded[0].suggestion_ids == (suggestion_id,)


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
class TestSuggestionCommands:
    """The queue, end to end."""

    @pytest.fixture
    def suggestion(self) -> str:
        """Generate one suggestion and return its identifier."""
        import asyncio  # noqa: PLC0415

        async def seed() -> int:
            container = Container.create()
            try:
                suggestion_id, _chat = await _prepare(container)
                return suggestion_id
            finally:
                await container.aclose()

        return str(asyncio.run(seed()))

    def test_list_shows_the_queue(self, suggestion: str) -> None:
        output = _run_cli("suggestion", "list")

        assert suggestion in output
        assert "pending" in output

    def test_show_prints_the_provenance(self, suggestion: str) -> None:
        output = _run_cli("suggestion", "show", suggestion)

        assert "status       pending" in output
        assert "type         reply_draft" in output
        assert "ai call" in output
        assert "prompt_version" in output

    def test_show_says_nothing_was_sent(self, suggestion: str) -> None:
        assert "Nothing has been sent" in _run_cli("suggestion", "show", suggestion)

    def test_accept_records_agreement_and_says_so(self, suggestion: str) -> None:
        output = _run_cli("suggestion", "accept", suggestion)

        assert "Accepted suggestion" in output
        assert "Nothing was sent" in output

    def test_accepting_empties_the_queue(self, suggestion: str) -> None:
        _run_cli("suggestion", "accept", suggestion)

        assert "Nothing to review" in _run_cli("suggestion", "list")

    def test_accepting_twice_is_refused(self, suggestion: str) -> None:
        _run_cli("suggestion", "accept", suggestion)

        result = runner.invoke(app, ["suggestion", "accept", suggestion])

        assert result.exit_code != 0
        assert "already been decided" in result.output

    def test_dismiss_records_the_refusal(self, suggestion: str) -> None:
        output = _run_cli("suggestion", "dismiss", suggestion)

        assert "Dismissed suggestion" in output
        assert "Nothing was done with it" in output

    def test_a_dismissed_one_cannot_be_accepted(self, suggestion: str) -> None:
        _run_cli("suggestion", "dismiss", suggestion)

        result = runner.invoke(app, ["suggestion", "accept", suggestion])

        assert result.exit_code != 0
        assert "already dismissed" in result.output

    def test_a_decided_one_is_still_shown(self, suggestion: str) -> None:
        _run_cli("suggestion", "dismiss", suggestion)

        assert "status       dismissed" in _run_cli("suggestion", "show", suggestion)

    def test_the_chat_listing_includes_decided_ones(self, suggestion: str) -> None:
        shown = _run_cli("suggestion", "show", suggestion)
        chat_id = shown.split("chat         ")[1].split()[0]
        _run_cli("suggestion", "dismiss", suggestion)

        output = _run_cli("suggestion", "list", "--chat", chat_id)

        assert "dismissed" in output

    def test_show_reports_an_unknown_suggestion(self, suggestion: str) -> None:  # noqa: ARG002
        result = runner.invoke(app, ["suggestion", "show", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_accept_reports_an_unknown_suggestion(self, suggestion: str) -> None:  # noqa: ARG002
        result = runner.invoke(app, ["suggestion", "accept", "999999"])

        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_an_empty_queue_says_so(self, suggestion: str) -> None:
        _run_cli("suggestion", "dismiss", suggestion)

        assert "Nothing to review" in _run_cli("suggestion", "list")
