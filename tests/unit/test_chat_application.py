"""Chat use cases and CLI commands.

The use cases run against fakes; the CLI runs end to end against a real SQLite
file, because the point of that layer is the wiring and wiring is what a fake
would hide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.chat_repository import InMemoryChatRepository, InMemoryChatStore
from tests.fakes.contact_repository import InMemoryContactRepository, InMemoryContactStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.use_cases.chat import (
    GetChat,
    ListChats,
    OpenGroupChat,
    OpenPrivateChat,
    SetChatPolicy,
)
from tgassist.domain.errors import ConflictError, DomainValidationError, RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import AiProcessingMode, ChatType
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import AccountId, ContactId, TelegramUserId
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.presentation.cli.app import app

runner = CliRunner()
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
CONTACT_A = ContactId(11)
CONTACT_B = ContactId(22)


class _Harness:
    """A use-case environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.contact_store = InMemoryContactStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})
        self.chat_store = InMemoryChatStore(
            known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)}, contacts={}
        )
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

    def contacts(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryContactRepository:
        return InMemoryContactRepository(self.contact_store, account_id)

    def chats(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryChatRepository:
        return InMemoryChatRepository(self.chat_store, account_id)

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

    async def add_contact(self, contact_id: ContactId, account_id: AccountId) -> Contact:
        contact = Contact.create(
            contact_id=contact_id,
            account_id=account_id,
            telegram_user_id=TelegramUserId(int(contact_id) * 100),
            display_name=f"contact-{int(contact_id)}",
            now=NOW,
        )
        await self.contacts(InMemoryUnitOfWork(), account_id).add(contact)
        # The chat store's composite foreign key needs to know about it too.
        self.chat_store.register_contact(contact_id, account_id)
        return contact

    def open_private(self) -> OpenPrivateChat:
        return OpenPrivateChat(
            self.unit_of_work, self.chats, self.contacts, self.accounts, self.clock, self.ids
        )

    def open_group(self) -> OpenGroupChat:
        return OpenGroupChat(self.unit_of_work, self.chats, self.accounts, self.clock, self.ids)

    def get(self) -> GetChat:
        return GetChat(self.unit_of_work, self.chats, self.accounts)

    def list(self) -> ListChats:
        return ListChats(self.unit_of_work, self.chats, self.accounts)

    def set_policy(self) -> SetChatPolicy:
        return SetChatPolicy(self.unit_of_work, self.chats, self.accounts, self.clock)


@pytest.fixture
async def harness() -> _Harness:
    """A use-case environment with one active account and one contact."""
    built = _Harness()
    await built.add_account(ACCOUNT_A, is_active=True)
    await built.add_contact(CONTACT_A, ACCOUNT_A)
    return built


class TestOpenPrivateChat:
    async def test_creates_the_edge_to_a_contact(self, harness: _Harness) -> None:
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        assert chat.account_id == ACCOUNT_A
        assert chat.contact_id == CONTACT_A
        assert chat.is_private
        assert chat.title is None

    async def test_defaults_keep_content_local(self, harness: _Harness) -> None:
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        assert chat.ai_processing_mode is AiProcessingMode.LOCAL_ONLY
        assert chat.sync_enabled

    async def test_the_chat_is_committed(self, harness: _Harness) -> None:
        await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        assert any(uow.is_committed for uow in harness.units)

    async def test_the_graph_can_be_traversed_from_the_contact(self, harness: _Harness) -> None:
        # The direction the application actually reads it: from a person to the
        # conversation with them.
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        found = await harness.get().with_contact(int(CONTACT_A))

        assert found is not None
        assert found.id == chat.id

    async def test_an_unknown_contact_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No contact"):
            await harness.open_private().execute(contact_id=999, telegram_chat_id=555)

    async def test_another_accounts_contact_is_not_reachable(self, harness: _Harness) -> None:
        # Not a check that could be skipped: the contact repository is scoped to
        # the same account, so somebody else's contact simply is not found.
        await harness.add_account(ACCOUNT_B)
        await harness.add_contact(CONTACT_B, ACCOUNT_B)

        with pytest.raises(RecordNotFoundError, match="No contact"):
            await harness.open_private().execute(contact_id=int(CONTACT_B), telegram_chat_id=555)

    async def test_a_contact_gets_only_one_private_chat(self, harness: _Harness) -> None:
        await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        with pytest.raises(ConflictError, match="already has private chat"):
            await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=556)

    async def test_a_duplicate_telegram_chat_is_refused(self, harness: _Harness) -> None:
        await harness.add_contact(ContactId(12), ACCOUNT_A)
        await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        with pytest.raises(ConflictError, match="already"):
            await harness.open_private().execute(contact_id=12, telegram_chat_id=555)

    async def test_the_ai_mode_can_be_set_at_creation(self, harness: _Harness) -> None:
        chat = await harness.open_private().execute(
            contact_id=int(CONTACT_A),
            telegram_chat_id=555,
            ai_processing_mode=AiProcessingMode.DISABLED,
        )

        assert not chat.allows_ai

    async def test_without_an_active_account_it_reports_rather_than_guesses(self) -> None:
        empty = _Harness()

        with pytest.raises(RecordNotFoundError, match="No account is active"):
            await empty.open_private().execute(contact_id=1, telegram_chat_id=555)


class TestOpenGroupChat:
    async def test_creates_a_titled_chat_with_no_contact(self, harness: _Harness) -> None:
        chat = await harness.open_group().execute(telegram_chat_id=-1001, title="Team")

        assert chat.title == "Team"
        assert chat.contact_id is None
        assert chat.chat_type is ChatType.GROUP

    async def test_accepts_a_negative_telegram_identifier(self, harness: _Harness) -> None:
        chat = await harness.open_group().execute(telegram_chat_id=-1_001_234, title="Team")

        assert int(chat.telegram_chat_id) < 0

    @pytest.mark.parametrize(
        "chat_type", [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL, ChatType.SAVED]
    )
    async def test_every_non_private_kind(self, harness: _Harness, chat_type: ChatType) -> None:
        chat = await harness.open_group().execute(
            telegram_chat_id=-1001, title="Something", chat_type=chat_type
        )

        assert chat.chat_type is chat_type

    async def test_a_private_type_is_refused(self, harness: _Harness) -> None:
        with pytest.raises(DomainValidationError):
            await harness.open_group().execute(
                telegram_chat_id=555, title="Nope", chat_type=ChatType.PRIVATE
            )

    async def test_a_duplicate_telegram_chat_is_refused(self, harness: _Harness) -> None:
        await harness.open_group().execute(telegram_chat_id=-1001, title="Team")

        with pytest.raises(ConflictError):
            await harness.open_group().execute(telegram_chat_id=-1001, title="Team again")


class TestListChats:
    async def test_lists_newest_first(self, harness: _Harness) -> None:
        for index in range(3):
            harness.clock.advance(timedelta(minutes=1))
            await harness.open_group().execute(
                telegram_chat_id=-(1000 + index), title=f"Group {index}"
            )

        page = await harness.list().execute()

        assert [chat.title for chat in page] == ["Group 2", "Group 1", "Group 0"]

    async def test_respects_the_page_limit(self, harness: _Harness) -> None:
        for index in range(5):
            harness.clock.advance(timedelta(minutes=1))
            await harness.open_group().execute(
                telegram_chat_id=-(1000 + index), title=f"Group {index}"
            )

        page = await harness.list().execute(PageRequest(limit=2))

        assert len(page) == 2
        assert page.has_more

    async def test_a_listing_shows_only_its_own_account(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_B)
        await harness.open_group().execute(telegram_chat_id=-1001, title="Mine")
        await harness.open_group().execute(
            telegram_chat_id=-1002, title="Theirs", account_id=ACCOUNT_B
        )

        page = await harness.list().execute()

        assert [chat.title for chat in page] == ["Mine"]


class TestSetChatPolicy:
    async def test_disabling_sync(self, harness: _Harness) -> None:
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        changed = await harness.set_policy().execute(int(chat.id), sync_enabled=False)

        assert not changed.sync_enabled

    async def test_disabling_ai(self, harness: _Harness) -> None:
        # "Stop using AI on our chats" -- a per-chat right in PRIVACY.md.
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        changed = await harness.set_policy().execute(
            int(chat.id), ai_processing_mode=AiProcessingMode.DISABLED
        )

        assert not changed.allows_ai

    async def test_both_at_once(self, harness: _Harness) -> None:
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        changed = await harness.set_policy().execute(
            int(chat.id),
            sync_enabled=False,
            ai_processing_mode=AiProcessingMode.CLOUD_ALLOWED,
        )

        assert not changed.sync_enabled
        assert changed.allows_cloud_ai

    async def test_the_change_is_persisted(self, harness: _Harness) -> None:
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        await harness.set_policy().execute(int(chat.id), sync_enabled=False)
        reread = await harness.get().execute(int(chat.id))

        assert reread is not None
        assert not reread.sync_enabled

    async def test_changing_nothing_commits_nothing(self, harness: _Harness) -> None:
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)
        before = len(harness.units)

        unchanged = await harness.set_policy().execute(int(chat.id))

        assert unchanged == chat
        assert not any(uow.is_committed for uow in harness.units[before:])

    async def test_a_redundant_change_does_not_move_the_timestamp(self, harness: _Harness) -> None:
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)
        harness.clock.advance(timedelta(days=1))

        unchanged = await harness.set_policy().execute(int(chat.id), sync_enabled=True)

        assert unchanged.updated_at == chat.updated_at

    async def test_an_absent_chat_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError):
            await harness.set_policy().execute(999, sync_enabled=False)

    async def test_another_accounts_chat_cannot_be_changed(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_B)
        chat = await harness.open_private().execute(contact_id=int(CONTACT_A), telegram_chat_id=555)

        with pytest.raises(RecordNotFoundError):
            await harness.set_policy().execute(
                int(chat.id), sync_enabled=False, account_id=ACCOUNT_B
            )


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
def contact_id() -> int:
    """Create an active account with one contact, and return the contact."""
    created = runner.invoke(app, ["account", "create", "100", "Primary"])
    assert created.exit_code == 0, created.output
    added = runner.invoke(app, ["contact", "add", "555", "Alice"])
    assert added.exit_code == 0, added.output
    return int(added.stdout.split("Added contact ")[1].split(":")[0])


def open_private(contact_id: int, telegram_chat_id: int = 555) -> int:
    """Open a private chat through the CLI and return its identifier."""
    result = runner.invoke(
        app, ["chat", "open", str(telegram_chat_id), "--contact", str(contact_id)]
    )
    assert result.exit_code == 0, result.output
    return int(result.stdout.split("chat ")[1].split(" ")[0])


@pytest.mark.usefixtures("cli_env")
class TestChatCli:
    def test_open_reports_the_new_chat(self, contact_id: int) -> None:
        result = runner.invoke(app, ["chat", "open", "555", "--contact", str(contact_id)])

        assert result.exit_code == 0, result.output
        assert "Opened private chat" in result.stdout

    def test_open_a_group_chat_with_a_negative_identifier(self, contact_id: int) -> None:
        # The shell reads a leading minus as an option, so the identifier goes
        # after --. Worth a test because every group chat Telegram has is
        # numbered this way.
        assert contact_id
        result = runner.invoke(
            app, ["chat", "open", "--type", "group", "--title", "Team", "--", "-1001234"]
        )

        assert result.exit_code == 0, result.output
        assert "Opened group chat" in result.stdout

    def test_show_displays_a_chat(self, contact_id: int) -> None:
        chat_id = open_private(contact_id)

        result = runner.invoke(app, ["chat", "show", str(chat_id)])

        assert result.exit_code == 0, result.output
        assert "private" in result.stdout
        assert "local_only" in result.stdout

    def test_show_traverses_from_the_contact(self, contact_id: int) -> None:
        chat_id = open_private(contact_id)

        result = runner.invoke(app, ["chat", "show", "--contact", str(contact_id)])

        assert result.exit_code == 0, result.output
        assert str(chat_id) in result.stdout

    def test_show_requires_exactly_one_selector(self, contact_id: int) -> None:
        chat_id = open_private(contact_id)

        both = runner.invoke(app, ["chat", "show", str(chat_id), "--contact", str(contact_id)])
        neither = runner.invoke(app, ["chat", "show"])

        assert both.exit_code != 0
        assert neither.exit_code != 0
        assert "not both" in both.output

    def test_list_shows_opened_chats(self, contact_id: int) -> None:
        open_private(contact_id)

        result = runner.invoke(app, ["chat", "list"])

        assert result.exit_code == 0, result.output
        assert "private" in result.stdout

    def test_list_is_empty_before_anything_is_opened(self, contact_id: int) -> None:
        assert contact_id
        result = runner.invoke(app, ["chat", "list"])

        assert result.exit_code == 0, result.output
        assert "No chats." in result.stdout

    def test_set_disables_ai(self, contact_id: int) -> None:
        chat_id = open_private(contact_id)

        result = runner.invoke(app, ["chat", "set", str(chat_id), "--ai", "disabled"])

        assert result.exit_code == 0, result.output
        assert "disabled" in result.stdout

    def test_set_disables_sync(self, contact_id: int) -> None:
        chat_id = open_private(contact_id)

        runner.invoke(app, ["chat", "set", str(chat_id), "--no-sync"])
        shown = runner.invoke(app, ["chat", "show", str(chat_id)])

        assert "sync             : False" in shown.stdout

    def test_changes_survive_a_new_process(self, contact_id: int) -> None:
        chat_id = open_private(contact_id)
        runner.invoke(app, ["chat", "set", str(chat_id), "--ai", "cloud_allowed"])

        result = runner.invoke(app, ["chat", "show", str(chat_id)])

        assert "cloud_allowed" in result.stdout

    def test_a_private_chat_without_a_contact_is_refused(self, contact_id: int) -> None:
        assert contact_id
        result = runner.invoke(app, ["chat", "open", "555"])

        assert result.exit_code != 0
        assert "needs --contact" in result.output
        assert "Traceback" not in result.output

    def test_a_private_chat_with_a_title_is_refused(self, contact_id: int) -> None:
        result = runner.invoke(
            app, ["chat", "open", "555", "--contact", str(contact_id), "--title", "Alice"]
        )

        assert result.exit_code != 0
        assert "drop --title" in result.output

    def test_a_group_chat_without_a_title_is_refused(self, contact_id: int) -> None:
        assert contact_id
        result = runner.invoke(app, ["chat", "open", "--type", "group", "--", "-1001"])

        assert result.exit_code != 0
        assert "needs --title" in result.output

    def test_a_group_chat_naming_a_contact_is_refused(self, contact_id: int) -> None:
        result = runner.invoke(
            app,
            [
                "chat",
                "open",
                "--type",
                "group",
                "--title",
                "Team",
                "--contact",
                str(contact_id),
                "--",
                "-1001",
            ],
        )

        assert result.exit_code != 0
        assert "Only a private chat" in result.output

    def test_an_unknown_contact_is_refused_without_a_traceback(self, contact_id: int) -> None:
        assert contact_id
        result = runner.invoke(app, ["chat", "open", "555", "--contact", "999"])

        assert result.exit_code != 0
        assert "contact was not found" in result.output
        assert "Traceback" not in result.output

    def test_a_second_private_chat_is_refused(self, contact_id: int) -> None:
        open_private(contact_id)

        result = runner.invoke(app, ["chat", "open", "556", "--contact", str(contact_id)])

        assert result.exit_code != 0
        assert "already has a chat" in result.output
        assert "Traceback" not in result.output

    def test_deleting_a_contact_does_not_remove_the_chat(self, contact_id: int) -> None:
        # Soft deletion hides a contact; it does not purge them, and the chat is
        # history that the purge in PRIVACY.md section 7 will remove.
        chat_id = open_private(contact_id)
        runner.invoke(app, ["contact", "delete", str(contact_id)])

        result = runner.invoke(app, ["chat", "show", str(chat_id)])

        assert result.exit_code == 0, result.output

    def test_chats_belong_to_their_account(self, contact_id: int) -> None:
        open_private(contact_id)
        second = runner.invoke(app, ["account", "create", "200", "Second"])
        second_id = int(second.stdout.split("Created account ")[1].split(" ")[0])

        listing = runner.invoke(app, ["chat", "list", "--account", str(second_id)])

        assert listing.exit_code == 0, listing.output
        assert "No chats." in listing.stdout


@pytest.mark.usefixtures("cli_env")
class TestChatCliWithoutAnAccount:
    def test_list_reports_that_no_account_is_active(self) -> None:
        result = runner.invoke(app, ["chat", "list"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output
