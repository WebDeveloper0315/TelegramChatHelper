"""Contact use cases and CLI commands.

The use cases run against fakes, so they exercise application logic with no
database. The CLI runs end to end against a real SQLite file, because the point
of that layer is the wiring and wiring is exactly what a fake would hide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.contact_repository import InMemoryContactRepository, InMemoryContactStore
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.use_cases.contact import (
    ChangeContactStatus,
    ContactTransition,
    CreateContact,
    GetContact,
    ListContacts,
)
from tgassist.domain.errors import ConflictError, DomainValidationError, RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.presentation.cli.app import app

runner = CliRunner()
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)


class _Harness:
    """A use-case environment built entirely from fakes."""

    def __init__(self) -> None:
        self.accounts_repository = InMemoryAccountRepository()
        self.store = InMemoryContactStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})
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
        """Build a scoped repository, exactly as the SQL factory does."""
        return InMemoryContactRepository(self.store, account_id)

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

    def create(self) -> CreateContact:
        return CreateContact(self.unit_of_work, self.contacts, self.accounts, self.clock, self.ids)

    def get(self) -> GetContact:
        return GetContact(self.unit_of_work, self.contacts, self.accounts)

    def list(self) -> ListContacts:
        return ListContacts(self.unit_of_work, self.contacts, self.accounts)

    def change(self) -> ChangeContactStatus:
        return ChangeContactStatus(self.unit_of_work, self.contacts, self.accounts, self.clock)


@pytest.fixture
async def harness() -> _Harness:
    """A use-case environment with one active account."""
    built = _Harness()
    await built.add_account(ACCOUNT_A, is_active=True)
    return built


class TestCreateContact:
    async def test_creates_an_active_contact(self, harness: _Harness) -> None:
        contact = await harness.create().execute(
            telegram_user_id=555, display_name="Alice", username="alice_ex"
        )

        assert contact.account_id == ACCOUNT_A
        assert contact.telegram_user_id == TelegramUserId(555)
        assert contact.display_name == "Alice"
        assert contact.username == "alice_ex"
        assert contact.is_active

    async def test_the_username_is_optional(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")

        assert contact.username is None

    async def test_the_contact_is_committed(self, harness: _Harness) -> None:
        await harness.create().execute(telegram_user_id=555, display_name="Alice")

        assert any(uow.is_committed for uow in harness.units)

    async def test_the_contact_can_be_read_back(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")

        assert await harness.get().execute(int(contact.id)) == contact

    async def test_a_duplicate_telegram_user_is_refused(self, harness: _Harness) -> None:
        await harness.create().execute(telegram_user_id=555, display_name="Alice")

        with pytest.raises(ConflictError, match="already knows") as excinfo:
            await harness.create().execute(telegram_user_id=555, display_name="Alice Again")

        assert "already a contact" in excinfo.value.user_message

    async def test_a_deleted_duplicate_says_to_restore(self, harness: _Harness) -> None:
        # A soft-deleted row still holds the natural key, so "already exists"
        # would be true but useless -- the caller cannot see the contact.
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.DELETE)

        with pytest.raises(ConflictError) as excinfo:
            await harness.create().execute(telegram_user_id=555, display_name="Alice Again")

        assert "Restore it instead" in excinfo.value.user_message

    async def test_the_same_telegram_user_may_belong_to_another_account(
        self, harness: _Harness
    ) -> None:
        await harness.add_account(ACCOUNT_B)
        await harness.create().execute(telegram_user_id=555, display_name="Known to A")

        other = await harness.create().execute(
            telegram_user_id=555, display_name="Known to B", account_id=ACCOUNT_B
        )

        assert other.account_id == ACCOUNT_B

    async def test_an_invalid_username_is_rejected(self, harness: _Harness) -> None:
        with pytest.raises(DomainValidationError):
            await harness.create().execute(
                telegram_user_id=555, display_name="Alice", username="no"
            )

    async def test_a_rejected_contact_is_not_stored(self, harness: _Harness) -> None:
        with pytest.raises(DomainValidationError):
            await harness.create().execute(telegram_user_id=555, display_name="   ")

        assert len(await harness.list().execute()) == 0

    async def test_without_an_active_account_it_reports_rather_than_guesses(self) -> None:
        empty = _Harness()

        with pytest.raises(RecordNotFoundError, match="No account is active"):
            await empty.create().execute(telegram_user_id=555, display_name="Alice")

    async def test_an_unknown_account_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError, match="No account with identifier"):
            await harness.create().execute(
                telegram_user_id=555, display_name="Alice", account_id=AccountId(999)
            )


class TestGetContact:
    async def test_an_absent_contact_returns_none(self, harness: _Harness) -> None:
        # Absence is an ordinary state here: the caller asked whether a contact
        # exists, and it does not.
        assert await harness.get().execute(999) is None

    async def test_a_deleted_contact_is_hidden_by_default(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.DELETE)

        assert await harness.get().execute(int(contact.id)) is None

    async def test_a_deleted_contact_is_returned_when_asked_for(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.DELETE)

        found = await harness.get().execute(int(contact.id), include_deleted=True)

        assert found is not None
        assert found.is_deleted

    async def test_an_archived_contact_is_returned(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.ARCHIVE)

        assert await harness.get().execute(int(contact.id)) is not None

    async def test_another_accounts_contact_is_not_visible(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_B)
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")

        assert await harness.get().execute(int(contact.id), account_id=ACCOUNT_B) is None


class TestListContacts:
    async def test_lists_newest_first(self, harness: _Harness) -> None:
        for index in range(3):
            harness.clock.advance(timedelta(minutes=index))
            await harness.create().execute(
                telegram_user_id=500 + index, display_name=f"Contact {index}"
            )

        page = await harness.list().execute()

        assert [c.display_name for c in page] == ["Contact 2", "Contact 1", "Contact 0"]

    async def test_respects_the_page_limit(self, harness: _Harness) -> None:
        for index in range(5):
            harness.clock.advance(timedelta(minutes=1))
            await harness.create().execute(
                telegram_user_id=500 + index, display_name=f"Contact {index}"
            )

        page = await harness.list().execute(PageRequest(limit=2))

        assert len(page) == 2
        assert page.has_more

    async def test_archived_contacts_are_hidden_by_default(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.ARCHIVE)

        assert len(await harness.list().execute()) == 0

    async def test_archived_contacts_are_listed_when_asked_for(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.ARCHIVE)

        assert len(await harness.list().execute(include_archived=True)) == 1

    async def test_deleted_contacts_are_never_listed(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.DELETE)

        assert len(await harness.list().execute(include_archived=True)) == 0

    async def test_a_listing_shows_only_its_own_account(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_B)
        await harness.create().execute(telegram_user_id=555, display_name="Mine")
        await harness.create().execute(
            telegram_user_id=556, display_name="Theirs", account_id=ACCOUNT_B
        )

        page = await harness.list().execute()

        assert [c.display_name for c in page] == ["Mine"]


class TestChangeContactStatus:
    async def test_archiving(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        harness.clock.advance(timedelta(days=1))

        changed = await harness.change().execute(int(contact.id), ContactTransition.ARCHIVE)

        assert changed.is_archived
        assert changed.updated_at == NOW + timedelta(days=1)

    async def test_deleting(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")

        changed = await harness.change().execute(int(contact.id), ContactTransition.DELETE)

        assert changed.is_deleted

    @pytest.mark.parametrize("first", [ContactTransition.ARCHIVE, ContactTransition.DELETE])
    async def test_restoring_from_either_state(
        self, harness: _Harness, first: ContactTransition
    ) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), first)

        restored = await harness.change().execute(int(contact.id), ContactTransition.RESTORE)

        assert restored.is_active

    async def test_the_change_is_persisted(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")

        await harness.change().execute(int(contact.id), ContactTransition.ARCHIVE)
        reread = await harness.get().execute(int(contact.id))

        assert reread is not None
        assert reread.is_archived

    async def test_a_redundant_transition_commits_nothing(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.ARCHIVE)
        before = len(harness.units)
        harness.clock.advance(timedelta(days=1))

        again = await harness.change().execute(int(contact.id), ContactTransition.ARCHIVE)

        assert not any(uow.is_committed for uow in harness.units[before:])
        assert again.updated_at != NOW + timedelta(days=1)

    async def test_archiving_a_deleted_contact_is_refused(self, harness: _Harness) -> None:
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")
        await harness.change().execute(int(contact.id), ContactTransition.DELETE)

        with pytest.raises(DomainValidationError, match="cannot be archived"):
            await harness.change().execute(int(contact.id), ContactTransition.ARCHIVE)

    async def test_an_absent_contact_is_reported(self, harness: _Harness) -> None:
        # Unlike a lookup, this promises a result: a caller archiving a
        # nonexistent contact has made a mistake worth reporting.
        with pytest.raises(RecordNotFoundError):
            await harness.change().execute(999, ContactTransition.ARCHIVE)

    async def test_another_accounts_contact_cannot_be_changed(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_B)
        contact = await harness.create().execute(telegram_user_id=555, display_name="Alice")

        with pytest.raises(RecordNotFoundError):
            await harness.change().execute(
                int(contact.id), ContactTransition.ARCHIVE, account_id=ACCOUNT_B
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
def _account() -> None:
    """Create an active account for the contact commands to work against."""
    result = runner.invoke(app, ["account", "create", "100", "Primary"])
    assert result.exit_code == 0, result.output


def add_contact(*args: str) -> int:
    """Add a contact through the CLI and return its identifier."""
    result = runner.invoke(app, ["contact", "add", *args])
    assert result.exit_code == 0, result.output
    return int(result.stdout.split("Added contact ")[1].split(":")[0])


@pytest.mark.usefixtures("cli_env", "_account")
class TestContactCli:
    def test_add_reports_the_new_contact(self) -> None:
        result = runner.invoke(app, ["contact", "add", "555", "Alice", "-u", "alice_ex"])

        assert result.exit_code == 0, result.output
        assert "Added contact" in result.stdout
        assert "@alice_ex" in result.stdout

    def test_add_without_a_username(self) -> None:
        result = runner.invoke(app, ["contact", "add", "555", "Alice"])

        assert result.exit_code == 0, result.output
        assert "@" not in result.stdout

    def test_show_displays_a_contact(self) -> None:
        contact_id = add_contact("555", "Alice", "-u", "alice_ex")

        result = runner.invoke(app, ["contact", "show", str(contact_id)])

        assert result.exit_code == 0, result.output
        assert "Alice" in result.stdout
        assert "alice_ex" in result.stdout
        assert "active" in result.stdout

    def test_show_reports_an_absent_contact_without_a_traceback(self) -> None:
        result = runner.invoke(app, ["contact", "show", "999"])

        assert result.exit_code != 0
        assert "No such contact" in result.output
        assert "Traceback" not in result.output

    def test_list_shows_added_contacts(self) -> None:
        add_contact("555", "Alice")
        add_contact("556", "Bob")

        result = runner.invoke(app, ["contact", "list"])

        assert result.exit_code == 0, result.output
        assert "Alice" in result.stdout
        assert "Bob" in result.stdout

    def test_list_is_empty_before_anything_is_added(self) -> None:
        result = runner.invoke(app, ["contact", "list"])

        assert result.exit_code == 0, result.output
        assert "No contacts." in result.stdout

    def test_archive_hides_a_contact_from_the_list(self) -> None:
        contact_id = add_contact("555", "Alice")

        archived = runner.invoke(app, ["contact", "archive", str(contact_id)])
        listing = runner.invoke(app, ["contact", "list"])

        assert archived.exit_code == 0, archived.output
        assert "is now archived" in archived.stdout
        assert "Alice" not in listing.stdout

    def test_archived_contacts_appear_with_the_flag(self) -> None:
        contact_id = add_contact("555", "Alice")
        runner.invoke(app, ["contact", "archive", str(contact_id)])

        result = runner.invoke(app, ["contact", "list", "--archived"])

        assert "Alice" in result.stdout
        assert "- = archived" in result.stdout

    def test_restore_brings_a_contact_back(self) -> None:
        contact_id = add_contact("555", "Alice")
        runner.invoke(app, ["contact", "archive", str(contact_id)])

        restored = runner.invoke(app, ["contact", "restore", str(contact_id)])
        listing = runner.invoke(app, ["contact", "list"])

        assert restored.exit_code == 0, restored.output
        assert "is now active" in restored.stdout
        assert "Alice" in listing.stdout

    def test_delete_removes_a_contact_from_every_listing(self) -> None:
        contact_id = add_contact("555", "Alice")

        deleted = runner.invoke(app, ["contact", "delete", str(contact_id)])
        listing = runner.invoke(app, ["contact", "list", "--archived"])

        assert deleted.exit_code == 0, deleted.output
        assert "is now deleted" in deleted.stdout
        assert "Alice" not in listing.stdout

    def test_a_deleted_contact_is_still_shown_by_identifier(self) -> None:
        # Somebody asking for a specific contact wants to know it was deleted,
        # not to be told it never existed.
        contact_id = add_contact("555", "Alice")
        runner.invoke(app, ["contact", "delete", str(contact_id)])

        result = runner.invoke(app, ["contact", "show", str(contact_id)])

        assert result.exit_code == 0, result.output
        assert "deleted" in result.stdout

    def test_restore_undoes_a_delete(self) -> None:
        contact_id = add_contact("555", "Alice")
        runner.invoke(app, ["contact", "delete", str(contact_id)])

        runner.invoke(app, ["contact", "restore", str(contact_id)])
        listing = runner.invoke(app, ["contact", "list"])

        assert "Alice" in listing.stdout

    def test_a_duplicate_is_refused_without_a_traceback(self) -> None:
        add_contact("555", "Alice")

        result = runner.invoke(app, ["contact", "add", "555", "Alice Again"])

        assert result.exit_code != 0
        assert "already a contact" in result.output
        assert "Traceback" not in result.output

    def test_re_adding_a_deleted_contact_says_to_restore(self) -> None:
        contact_id = add_contact("555", "Alice")
        runner.invoke(app, ["contact", "delete", str(contact_id)])

        result = runner.invoke(app, ["contact", "add", "555", "Alice Again"])

        assert result.exit_code != 0
        assert "Restore it instead" in result.output
        assert "Traceback" not in result.output

    def test_an_invalid_username_is_refused_without_a_traceback(self) -> None:
        result = runner.invoke(app, ["contact", "add", "555", "Alice", "-u", "no"])

        assert result.exit_code != 0
        assert "not a valid Telegram username" in result.output
        assert "Traceback" not in result.output

    def test_archiving_a_deleted_contact_is_refused(self) -> None:
        contact_id = add_contact("555", "Alice")
        runner.invoke(app, ["contact", "delete", str(contact_id)])

        result = runner.invoke(app, ["contact", "archive", str(contact_id)])

        assert result.exit_code != 0
        assert "Restore it" in result.output
        assert "Traceback" not in result.output

    def test_contacts_belong_to_their_account(self) -> None:
        # Two accounts, the same Telegram user, two contacts -- and neither
        # account's listing shows the other's.
        second = runner.invoke(app, ["account", "create", "200", "Second"])
        assert second.exit_code == 0, second.output
        second_id = int(second.stdout.split("Created account ")[1].split(" ")[0])
        add_contact("555", "Known to first")
        add_contact("555", "Known to second", "--account", str(second_id))

        first_list = runner.invoke(app, ["contact", "list"])
        second_list = runner.invoke(app, ["contact", "list", "--account", str(second_id)])

        assert "Known to first" in first_list.stdout
        assert "Known to second" not in first_list.stdout
        assert "Known to second" in second_list.stdout
        assert "Known to first" not in second_list.stdout

    def test_changes_survive_a_new_process(self) -> None:
        # Each invocation builds its own container, so this really does read the
        # row back from the file.
        contact_id = add_contact("555", "Alice")
        runner.invoke(app, ["contact", "archive", str(contact_id)])

        result = runner.invoke(app, ["contact", "show", str(contact_id)])

        assert "archived" in result.stdout


@pytest.mark.usefixtures("cli_env")
class TestContactCliWithoutAnAccount:
    def test_add_reports_that_no_account_is_active(self) -> None:
        result = runner.invoke(app, ["contact", "add", "555", "Alice"])

        assert result.exit_code != 0
        assert "No account is active" in result.output
        assert "Traceback" not in result.output

    def test_list_reports_that_no_account_is_active(self) -> None:
        result = runner.invoke(app, ["contact", "list"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output
