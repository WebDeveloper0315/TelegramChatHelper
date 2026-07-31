"""The ingestion pipeline, and the CLI that drives it.

The pipeline is what this milestone exists to build, so these tests are mostly
about its two defining properties: it does not know where a message came from,
and running it twice does not store a message twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.chat_repository import InMemoryChatRepository, InMemoryChatStore
from tests.fakes.message_repository import InMemoryMessageRepository, InMemoryMessageStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.use_cases.message import (
    GetMessage,
    IncomingMessage,
    IngestMessages,
    ReadChatHistory,
)
from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
    TelegramUserId,
)
from tgassist.domain.model.message import MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.presentation.cli.app import app

runner = CliRunner()
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SENT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CHAT_A = ChatId(101)
CHAT_B = ChatId(102)


def incoming(
    index: int = 1,
    *,
    telegram_message_id: int | None = None,
    sender: SenderKind = SenderKind.CONTACT,
) -> IncomingMessage:
    """Build one incoming message as a source would describe it."""
    return IncomingMessage(
        sender_kind=sender,
        sent_at=SENT + timedelta(minutes=index),
        text=f"message {index}",
        telegram_message_id=telegram_message_id,
    )


class _Harness:
    """A use-case environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)}, contacts={}
        )
        self.message_store = InMemoryMessageStore(chats={})
        self.clock = AdvanceableClock(NOW)
        self.ids = SequentialIdGenerator()
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

    async def add_account(self, account_id: AccountId, *, is_active: bool = False) -> Account:
        account = Account.create(
            account_id=account_id,
            telegram_user_id=TelegramUserId(1000 + int(account_id)),
            display_name=f"account-{int(account_id)}",
            now=NOW,
            is_active=is_active,
        )
        await self.accounts_repository.add(account)
        return account

    async def add_chat(self, chat_id: ChatId, account_id: AccountId) -> Chat:
        contact_id = ContactId(int(chat_id) + 500)
        self.chat_store.register_contact(contact_id, account_id)
        chat = Chat.private_with(
            chat_id=chat_id,
            account_id=account_id,
            telegram_chat_id=TelegramChatId(int(chat_id) * 7),
            contact_id=contact_id,
            now=NOW,
        )
        await self.chats(InMemoryUnitOfWork(), account_id).add(chat)
        self.message_store.register_chat(chat_id, account_id)
        return chat

    def ingest(self) -> IngestMessages:
        return IngestMessages(
            self.unit_of_work,
            self.messages,
            self.chats,
            self.accounts,
            self.clock,
            self.ids,
        )

    def history(self) -> ReadChatHistory:
        return ReadChatHistory(self.unit_of_work, self.messages, self.accounts)

    def get(self) -> GetMessage:
        return GetMessage(self.unit_of_work, self.messages, self.accounts)


@pytest.fixture
async def harness() -> _Harness:
    """One active account with one chat."""
    built = _Harness()
    await built.add_account(ACCOUNT_A, is_active=True)
    await built.add_chat(CHAT_A, ACCOUNT_A)
    return built


class TestIngestion:
    async def test_stores_a_message(self, harness: _Harness) -> None:
        report = await harness.ingest().execute(int(CHAT_A), [incoming()])

        assert report.stored == 1
        assert report.skipped == 0
        assert report.changed

    async def test_the_message_can_be_read_back(self, harness: _Harness) -> None:
        report = await harness.ingest().execute(int(CHAT_A), [incoming()])

        stored = await harness.get().execute(int(report.message_ids[0]))

        assert stored is not None
        assert stored.text == "message 1"
        assert stored.chat_id == CHAT_A

    async def test_the_ingestion_time_comes_from_the_clock_not_the_source(
        self, harness: _Harness
    ) -> None:
        # The distinction that makes a backfill diagnosable: sent_at is what the
        # source reported, ingested_at is when we stored it.
        report = await harness.ingest().execute(int(CHAT_A), [incoming()])

        stored = await harness.get().execute(int(report.message_ids[0]))

        assert stored is not None
        assert stored.sent_at == SENT + timedelta(minutes=1)
        assert stored.ingested_at == NOW

    async def test_a_batch_is_stored_in_one_transaction(self, harness: _Harness) -> None:
        before = len(harness.units)

        report = await harness.ingest().execute(
            int(CHAT_A), [incoming(index) for index in range(1, 6)]
        )

        assert report.stored == 5
        assert len(harness.units) == before + 1

    async def test_every_message_in_a_batch_shares_one_ingestion_time(
        self, harness: _Harness
    ) -> None:
        report = await harness.ingest().execute(
            int(CHAT_A), [incoming(index) for index in range(1, 4)]
        )

        stored = [await harness.get().execute(int(i)) for i in report.message_ids]
        assert {m.ingested_at for m in stored if m is not None} == {NOW}

    async def test_an_empty_batch_is_permitted(self, harness: _Harness) -> None:
        # A synchronisation run that found nothing has not failed.
        report = await harness.ingest().execute(int(CHAT_A), [])

        assert report.total == 0
        assert not report.changed

    async def test_an_empty_batch_commits_nothing(self, harness: _Harness) -> None:
        before = len(harness.units)

        await harness.ingest().execute(int(CHAT_A), [])

        assert not any(uow.is_committed for uow in harness.units[before:])


class TestSourceAgnosticism:
    @pytest.mark.parametrize("sender", list(SenderKind))
    async def test_every_sender_kind_is_accepted(
        self, harness: _Harness, sender: SenderKind
    ) -> None:
        report = await harness.ingest().execute(int(CHAT_A), [incoming(sender=sender)])

        assert report.stored == 1

    async def test_a_message_with_no_external_identifier_is_accepted(
        self, harness: _Harness
    ) -> None:
        # The CLI, an import tool and a test all produce these. Requiring an
        # identifier would make the pipeline Telegram-specific (ADR-045).
        report = await harness.ingest().execute(int(CHAT_A), [incoming()])
        stored = await harness.get().execute(int(report.message_ids[0]))

        assert stored is not None
        assert not stored.has_external_identity

    async def test_a_message_with_an_external_identifier_is_accepted(
        self, harness: _Harness
    ) -> None:
        report = await harness.ingest().execute(int(CHAT_A), [incoming(telegram_message_id=9001)])
        stored = await harness.get().execute(int(report.message_ids[0]))

        assert stored is not None
        assert stored.has_external_identity

    async def test_a_batch_may_mix_identified_and_unidentified_messages(
        self, harness: _Harness
    ) -> None:
        report = await harness.ingest().execute(
            int(CHAT_A),
            [
                incoming(1, telegram_message_id=9001),
                incoming(2),
                incoming(3, telegram_message_id=9003),
            ],
        )

        assert report.stored == 3

    async def test_a_non_text_message_needs_no_text(self, harness: _Harness) -> None:
        report = await harness.ingest().execute(
            int(CHAT_A),
            [
                IncomingMessage(
                    sender_kind=SenderKind.CONTACT,
                    sent_at=SENT,
                    message_type=MessageType.STICKER,
                )
            ],
        )

        assert report.stored == 1


class TestIdempotency:
    async def test_re_ingesting_an_identified_message_stores_nothing(
        self, harness: _Harness
    ) -> None:
        # The property that makes re-synchronisation safe.
        await harness.ingest().execute(int(CHAT_A), [incoming(telegram_message_id=9001)])

        report = await harness.ingest().execute(int(CHAT_A), [incoming(telegram_message_id=9001)])

        assert report.stored == 0
        assert report.skipped == 1
        assert not report.changed

    async def test_a_repeat_is_reported_rather_than_raised(self, harness: _Harness) -> None:
        # An error would force every caller to wrap the ordinary case in a
        # try/except, and a backfill overlapping live updates is ordinary.
        await harness.ingest().execute(int(CHAT_A), [incoming(telegram_message_id=9001)])

        report = await harness.ingest().execute(int(CHAT_A), [incoming(telegram_message_id=9001)])

        assert report.total == 1

    async def test_an_overlapping_batch_stores_only_what_is_new(self, harness: _Harness) -> None:
        # Exactly what a backfill meeting live updates looks like.
        await harness.ingest().execute(
            int(CHAT_A), [incoming(index, telegram_message_id=9000 + index) for index in (1, 2)]
        )

        report = await harness.ingest().execute(
            int(CHAT_A),
            [incoming(index, telegram_message_id=9000 + index) for index in (1, 2, 3, 4)],
        )

        assert report.stored == 2
        assert report.skipped == 2

    async def test_one_batch_naming_a_message_twice_stores_it_once(self, harness: _Harness) -> None:
        # Nothing is written until the batch is built, so the repository cannot
        # answer for an identifier this batch has already claimed. Without
        # tracking them the second copy would meet the unique index -- an error
        # raised over exactly the case this pipeline promises to absorb.
        report = await harness.ingest().execute(
            int(CHAT_A),
            [incoming(telegram_message_id=9001), incoming(telegram_message_id=9001)],
        )

        assert report.stored == 1
        assert report.skipped == 1

    async def test_a_batch_of_unidentified_messages_is_never_collapsed(
        self, harness: _Harness
    ) -> None:
        # The dedup is by identifier, not by content: two identical typed
        # messages in one batch are still two messages.
        report = await harness.ingest().execute(int(CHAT_A), [incoming(), incoming()])

        assert report.stored == 2

    async def test_a_repeat_commits_nothing(self, harness: _Harness) -> None:
        await harness.ingest().execute(int(CHAT_A), [incoming(telegram_message_id=9001)])
        before = len(harness.units)

        await harness.ingest().execute(int(CHAT_A), [incoming(telegram_message_id=9001)])

        assert not any(uow.is_committed for uow in harness.units[before:])

    async def test_messages_without_identifiers_are_never_deduplicated(
        self, harness: _Harness
    ) -> None:
        # Two identical messages typed at a keyboard are two messages. There is
        # nothing to match on, and inventing a content hash would silently drop
        # a real repeated message (ADR-045).
        await harness.ingest().execute(int(CHAT_A), [incoming()])
        report = await harness.ingest().execute(int(CHAT_A), [incoming()])

        assert report.stored == 1
        assert report.skipped == 0

    async def test_the_same_identifier_in_another_chat_is_a_different_message(
        self, harness: _Harness
    ) -> None:
        other = await harness.add_chat(ChatId(999), ACCOUNT_A)
        await harness.ingest().execute(int(CHAT_A), [incoming(telegram_message_id=9001)])

        report = await harness.ingest().execute(int(other.id), [incoming(telegram_message_id=9001)])

        assert report.stored == 1


class TestOwnershipAndFailure:
    async def test_an_unknown_chat_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No chat"):
            await harness.ingest().execute(999, [incoming()])

    async def test_another_accounts_chat_is_not_reachable(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_B)
        await harness.add_chat(CHAT_B, ACCOUNT_B)

        with pytest.raises(RecordNotFoundError, match="No chat"):
            await harness.ingest().execute(int(CHAT_B), [incoming()])

    async def test_without_an_active_account_it_reports_rather_than_guesses(self) -> None:
        empty = _Harness()

        with pytest.raises(RecordNotFoundError, match="No account is active"):
            await empty.ingest().execute(1, [incoming()])

    async def test_an_invalid_message_rejects_the_whole_batch(self, harness: _Harness) -> None:
        # Better than a partial ingestion whose extent nobody can determine.
        bad = IncomingMessage(sender_kind=SenderKind.CONTACT, sent_at=SENT, text="   ")

        with pytest.raises(DomainValidationError):
            await harness.ingest().execute(int(CHAT_A), [incoming(1), bad, incoming(3)])

        assert len(await harness.history().execute(int(CHAT_A))) == 0


class TestReadChatHistory:
    async def test_returns_messages_newest_sent_first(self, harness: _Harness) -> None:
        await harness.ingest().execute(int(CHAT_A), [incoming(index) for index in (1, 2, 3)])

        page = await harness.history().execute(int(CHAT_A))

        assert [m.text for m in page] == ["message 3", "message 2", "message 1"]

    async def test_respects_the_page_limit(self, harness: _Harness) -> None:
        await harness.ingest().execute(int(CHAT_A), [incoming(index) for index in range(1, 6)])

        page = await harness.history().execute(int(CHAT_A), PageRequest(limit=2))

        assert len(page) == 2
        assert page.has_more

    async def test_an_empty_chat_returns_an_empty_page(self, harness: _Harness) -> None:
        assert len(await harness.history().execute(int(CHAT_A))) == 0

    async def test_history_does_not_cross_chats(self, harness: _Harness) -> None:
        other = await harness.add_chat(ChatId(999), ACCOUNT_A)
        await harness.ingest().execute(int(CHAT_A), [incoming(1)])
        await harness.ingest().execute(int(other.id), [incoming(2)])

        page = await harness.history().execute(int(CHAT_A))

        assert [m.text for m in page] == ["message 1"]

    async def test_history_does_not_cross_accounts(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_B)
        await harness.add_chat(CHAT_B, ACCOUNT_B)
        await harness.ingest().execute(int(CHAT_A), [incoming(1)])

        page = await harness.history().execute(int(CHAT_A), account_id=ACCOUNT_B)

        assert len(page) == 0


class TestGetMessage:
    async def test_an_absent_message_returns_none(self, harness: _Harness) -> None:
        assert await harness.get().execute(999) is None

    async def test_another_accounts_message_is_not_visible(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_B)
        report = await harness.ingest().execute(int(CHAT_A), [incoming()])

        found = await harness.get().execute(int(report.message_ids[0]), account_id=ACCOUNT_B)

        assert found is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,  # noqa: ARG001 - a command configures logging process-wide
) -> Path:
    """Point the CLI at an isolated data directory with logging silenced."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(data_dir))
    monkeypatch.setenv("TGASSIST_LOGGING__CONSOLE_ENABLED", "false")
    monkeypatch.setenv("TGASSIST_LOGGING__FILE_ENABLED", "false")
    return data_dir


@pytest.fixture
def chat_id() -> int:
    """Create an account, a contact and their chat; return the chat."""
    created = runner.invoke(app, ["account", "create", "100", "Primary"])
    assert created.exit_code == 0, created.output
    added = runner.invoke(app, ["contact", "add", "555", "Alice"])
    assert added.exit_code == 0, added.output
    contact_id = int(added.stdout.split("Added contact ")[1].split(":")[0])
    opened = runner.invoke(app, ["chat", "open", "555", "--contact", str(contact_id)])
    assert opened.exit_code == 0, opened.output
    return int(opened.stdout.split("chat ")[1].split(" ")[0])


@pytest.mark.usefixtures("cli_env")
class TestMessageCli:
    def test_ingest_reports_the_new_message(self, chat_id: int) -> None:
        result = runner.invoke(app, ["message", "ingest", str(chat_id), "Hello there"])

        assert result.exit_code == 0, result.output
        assert "Ingested message" in result.stdout

    def test_ingesting_twice_with_an_identifier_stores_one(self, chat_id: int) -> None:
        first = runner.invoke(
            app, ["message", "ingest", str(chat_id), "Hi", "--telegram-id", "9001"]
        )
        second = runner.invoke(
            app, ["message", "ingest", str(chat_id), "Hi", "--telegram-id", "9001"]
        )
        history = runner.invoke(app, ["message", "history", str(chat_id)])

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert "Already ingested" in second.stdout
        assert history.stdout.count("Hi") == 1

    def test_ingesting_twice_without_an_identifier_stores_two(self, chat_id: int) -> None:
        runner.invoke(app, ["message", "ingest", str(chat_id), "Typed"])
        runner.invoke(app, ["message", "ingest", str(chat_id), "Typed"])

        history = runner.invoke(app, ["message", "history", str(chat_id)])

        assert history.stdout.count("Typed") == 2

    def test_history_shows_direction(self, chat_id: int) -> None:
        runner.invoke(app, ["message", "ingest", str(chat_id), "Mine", "--from", "operator"])
        runner.invoke(app, ["message", "ingest", str(chat_id), "Theirs", "--from", "contact"])

        result = runner.invoke(app, ["message", "history", str(chat_id)])

        assert "> " in result.stdout
        assert "< " in result.stdout

    def test_history_is_empty_before_anything_is_ingested(self, chat_id: int) -> None:
        result = runner.invoke(app, ["message", "history", str(chat_id)])

        assert result.exit_code == 0, result.output
        assert "No messages." in result.stdout

    def test_show_displays_a_message_in_full(self, chat_id: int) -> None:
        ingested = runner.invoke(
            app, ["message", "ingest", str(chat_id), "The whole thing", "--telegram-id", "9001"]
        )
        message_id = int(ingested.stdout.split("Ingested message ")[1].rstrip(".\n"))

        result = runner.invoke(app, ["message", "show", str(message_id)])

        assert result.exit_code == 0, result.output
        assert "The whole thing" in result.stdout
        assert "9001" in result.stdout

    def test_show_reports_an_absent_message_without_a_traceback(self, chat_id: int) -> None:
        assert chat_id
        result = runner.invoke(app, ["message", "show", "999"])

        assert result.exit_code != 0
        assert "No such message" in result.output
        assert "Traceback" not in result.output

    def test_an_explicit_sent_at_is_honoured(self, chat_id: int) -> None:
        # What an import tool supplies: a message from years ago, ingested now.
        result = runner.invoke(
            app,
            [
                "message",
                "ingest",
                str(chat_id),
                "From the archive",
                "--sent-at",
                "2020-01-01T09:00:00+00:00",
            ],
        )
        message_id = int(result.stdout.split("Ingested message ")[1].rstrip(".\n"))
        shown = runner.invoke(app, ["message", "show", str(message_id)])

        assert result.exit_code == 0, result.output
        assert "2020-01-01" in shown.stdout

    def test_a_backfilled_message_sorts_by_when_it_was_sent(self, chat_id: int) -> None:
        runner.invoke(app, ["message", "ingest", str(chat_id), "Recent"])
        runner.invoke(
            app,
            [
                "message",
                "ingest",
                str(chat_id),
                "Ancient",
                "--sent-at",
                "2020-01-01T09:00:00+00:00",
            ],
        )

        history = runner.invoke(app, ["message", "history", str(chat_id)])

        lines = [line for line in history.stdout.splitlines() if line.strip()]
        assert "Recent" in lines[0]
        assert "Ancient" in lines[-1]

    def test_an_unknown_chat_is_refused_without_a_traceback(self, chat_id: int) -> None:
        assert chat_id
        result = runner.invoke(app, ["message", "ingest", "999", "Nowhere"])

        assert result.exit_code != 0
        assert "chat was not found" in result.output
        assert "Traceback" not in result.output

    def test_an_empty_message_is_refused_without_a_traceback(self, chat_id: int) -> None:
        result = runner.invoke(app, ["message", "ingest", str(chat_id), "   "])

        assert result.exit_code != 0
        assert "cannot be empty" in result.output
        assert "Traceback" not in result.output

    def test_messages_belong_to_their_account(self, chat_id: int) -> None:
        runner.invoke(app, ["message", "ingest", str(chat_id), "Mine"])
        second = runner.invoke(app, ["account", "create", "200", "Second"])
        second_id = int(second.stdout.split("Created account ")[1].split(" ")[0])

        history = runner.invoke(
            app, ["message", "history", str(chat_id), "--account", str(second_id)]
        )

        assert history.exit_code == 0, history.output
        assert "No messages." in history.stdout

    def test_deleting_a_contact_leaves_the_history(self, chat_id: int) -> None:
        # Soft deletion hides a contact; the history is what a purge will later
        # remove.
        runner.invoke(app, ["message", "ingest", str(chat_id), "Still here"])
        contacts = runner.invoke(app, ["contact", "list"])
        contact_id = int(contacts.stdout.split()[0])
        runner.invoke(app, ["contact", "delete", str(contact_id)])

        history = runner.invoke(app, ["message", "history", str(chat_id)])

        assert "Still here" in history.stdout
