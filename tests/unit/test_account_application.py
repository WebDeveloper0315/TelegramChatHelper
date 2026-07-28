"""Account use cases and CLI commands.

The use cases run against fakes, so they exercise application logic with no
database. The CLI runs end to end against a real SQLite file, because the point
of that layer is the wiring and wiring is exactly what a fake would hide.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes import FixedClock, SequentialIdGenerator
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tgassist.application.use_cases.account import (
    CreateAccount,
    CreateAccountRequest,
    GetAccount,
    ListAccounts,
    SetActiveAccount,
)
from tgassist.domain.errors import ConflictError, DomainValidationError, RecordNotFoundError
from tgassist.domain.events import AccountActivated, AccountCreated
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.presentation.cli.app import app

runner = CliRunner()
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class _Harness:
    """A use-case environment built entirely from fakes."""

    def __init__(self) -> None:
        self.repository = InMemoryAccountRepository()
        self.clock = FixedClock(NOW)
        self.ids = SequentialIdGenerator()
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()

    def unit_of_work(self) -> InMemoryUnitOfWork:
        uow = self._factory()
        self.units.append(uow)
        return uow

    def accounts(self, _uow: UnitOfWork) -> InMemoryAccountRepository:
        """Return the one repository, ignoring the transaction.

        The fake repository has no transaction to enlist in; the unit of work
        is still exercised so that commit and event-release behaviour is real.
        """
        return self.repository

    @property
    def events(self) -> list[object]:
        return [event for uow in self.units for event in uow.collect_events()]

    def create(self) -> CreateAccount:
        return CreateAccount(self.unit_of_work, self.accounts, self.clock, self.ids)

    def get(self) -> GetAccount:
        return GetAccount(self.unit_of_work, self.accounts)

    def list(self) -> ListAccounts:
        return ListAccounts(self.unit_of_work, self.accounts)

    def activate(self) -> SetActiveAccount:
        return SetActiveAccount(self.unit_of_work, self.accounts, self.clock)


@pytest.fixture
def harness() -> _Harness:
    """A use-case environment built from fakes."""
    return _Harness()


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


class TestCreateAccount:
    async def test_persists_the_account(self, harness: _Harness) -> None:
        account = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="Primary")
        )

        assert await harness.repository.get(account.id) == account

    async def test_uses_the_injected_clock(self, harness: _Harness) -> None:
        account = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="Primary")
        )

        assert account.created_at == NOW

    async def test_uses_the_injected_identifier_generator(self, harness: _Harness) -> None:
        account = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="Primary")
        )

        assert int(account.id) == 1

    async def test_the_first_account_becomes_active(self, harness: _Harness) -> None:
        # Otherwise a fresh installation sits in a state where nothing works and
        # the reason is not obvious.
        account = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="First")
        )

        assert account.is_active

    async def test_later_accounts_do_not_become_active(self, harness: _Harness) -> None:
        await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="First")
        )

        second = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=200, display_name="Second")
        )

        assert second.is_active is False

    async def test_duplicate_telegram_user_is_refused(self, harness: _Harness) -> None:
        # Checked before writing, so the caller gets a message naming the
        # conflict rather than a constraint violation naming a column.
        await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="First")
        )

        with pytest.raises(ConflictError) as excinfo:
            await harness.create().execute(
                CreateAccountRequest(telegram_user_id=100, display_name="Duplicate")
            )

        assert "already been added" in excinfo.value.user_message

    async def test_invalid_timezone_is_refused(self, harness: _Harness) -> None:
        with pytest.raises(DomainValidationError):
            await harness.create().execute(
                CreateAccountRequest(
                    telegram_user_id=100, display_name="X", timezone="Mars/Olympus"
                )
            )

    async def test_publishes_an_event_after_commit(self, harness: _Harness) -> None:
        await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="Primary")
        )

        created = [e for e in harness.events if isinstance(e, AccountCreated)]
        assert len(created) == 1
        assert created[0].is_active is True

    async def test_a_rejected_creation_publishes_nothing(self, harness: _Harness) -> None:
        # The event must not survive a transaction that never committed.
        await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="First")
        )
        harness.units.clear()

        with pytest.raises(ConflictError):
            await harness.create().execute(
                CreateAccountRequest(telegram_user_id=100, display_name="Duplicate")
            )

        assert harness.events == []


class TestGetAccount:
    async def test_returns_by_identifier(self, harness: _Harness) -> None:
        created = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="Primary")
        )

        assert await harness.get().execute(int(created.id)) == created

    async def test_returns_the_active_account_when_no_identifier_is_given(
        self, harness: _Harness
    ) -> None:
        created = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="Primary")
        )

        assert await harness.get().execute() == created

    async def test_returns_none_for_an_unknown_identifier(self, harness: _Harness) -> None:
        assert await harness.get().execute(999) is None

    async def test_returns_none_when_no_account_is_active(self, harness: _Harness) -> None:
        assert await harness.get().execute() is None


class TestListAccounts:
    async def test_returns_an_empty_page_when_there_are_none(self, harness: _Harness) -> None:
        page = await harness.list().execute()

        assert len(page) == 0

    async def test_returns_every_account(self, harness: _Harness) -> None:
        for index in range(3):
            await harness.create().execute(
                CreateAccountRequest(telegram_user_id=100 + index, display_name=f"A{index}")
            )

        page = await harness.list().execute(PageRequest(limit=10))

        assert len(page) == 3


class TestSetActiveAccount:
    async def test_moves_the_active_flag(self, harness: _Harness) -> None:
        first = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="First")
        )
        second = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=200, display_name="Second")
        )

        await harness.activate().execute(int(second.id))

        assert (await harness.get().execute(int(first.id))).is_active is False  # type: ignore[union-attr]
        assert (await harness.get().execute(int(second.id))).is_active is True  # type: ignore[union-attr]

    async def test_publishes_an_event(self, harness: _Harness) -> None:
        first = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=100, display_name="First")
        )
        second = await harness.create().execute(
            CreateAccountRequest(telegram_user_id=200, display_name="Second")
        )
        harness.units.clear()

        await harness.activate().execute(int(second.id))

        activated = [e for e in harness.events if isinstance(e, AccountActivated)]
        assert len(activated) == 1
        assert activated[0].account_id == int(second.id)
        assert first.is_active

    async def test_unknown_account_raises(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError):
            await harness.activate().execute(999)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,  # noqa: ARG001 - a command configures logging process-wide
) -> Path:
    """Point the CLI at an isolated data directory with logging silenced.

    Commands configure logging on startup (ADR-040), so the fixture both turns
    the sinks off -- these tests assert on command output, not on records -- and
    depends on ``restore_logging`` so the configuration does not outlive them.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(data_dir))
    monkeypatch.setenv("TGASSIST_LOGGING__CONSOLE_ENABLED", "false")
    monkeypatch.setenv("TGASSIST_LOGGING__FILE_ENABLED", "false")
    return data_dir


@pytest.mark.usefixtures("cli_env")
class TestAccountCli:
    def test_create_reports_success_and_activation(self) -> None:
        result = runner.invoke(app, ["account", "create", "100", "Primary"])

        assert result.exit_code == 0
        assert "Created account" in result.stdout
        assert "active account" in result.stdout

    def test_create_accepts_a_timezone(self) -> None:
        result = runner.invoke(
            app, ["account", "create", "100", "Primary", "--timezone", "Europe/London"]
        )
        show = runner.invoke(app, ["account", "show"])

        assert result.exit_code == 0
        assert "Europe/London" in show.stdout

    def test_create_rejects_an_invalid_timezone_without_a_traceback(self) -> None:
        # A domain validation failure must reach the user as a message, not a
        # stack trace.
        result = runner.invoke(app, ["account", "create", "100", "X", "-t", "Mars/Olympus"])

        assert result.exit_code != 0
        assert "not a recognised timezone" in result.output
        assert "Traceback" not in result.output

    def test_create_rejects_a_duplicate(self) -> None:
        runner.invoke(app, ["account", "create", "100", "First"])

        result = runner.invoke(app, ["account", "create", "100", "Second"])

        assert result.exit_code != 0
        assert "already been added" in result.output

    def test_show_reports_no_active_account(self) -> None:
        result = runner.invoke(app, ["account", "show"])

        assert result.exit_code != 0
        assert "No account is active" in result.stdout

    def test_show_displays_every_field(self) -> None:
        runner.invoke(app, ["account", "create", "12345", "Primary"])

        result = runner.invoke(app, ["account", "show"])

        assert result.exit_code == 0
        for label in ("id", "telegram user id", "display name", "timezone", "active"):
            assert label in result.stdout
        assert "12345" in result.stdout

    def test_list_reports_emptiness(self) -> None:
        result = runner.invoke(app, ["account", "list"])

        assert result.exit_code == 0
        assert "No accounts." in result.stdout

    def test_list_marks_the_active_account(self) -> None:
        runner.invoke(app, ["account", "create", "100", "First"])
        runner.invoke(app, ["account", "create", "200", "Second"])

        result = runner.invoke(app, ["account", "list"])

        assert result.exit_code == 0
        assert "First" in result.stdout
        assert "Second" in result.stdout
        assert "* = active account" in result.stdout

    def test_activate_switches_accounts(self) -> None:
        runner.invoke(app, ["account", "create", "100", "First"])
        runner.invoke(app, ["account", "create", "200", "Second"])
        listing = runner.invoke(app, ["account", "list"])
        # The active marker shifts the columns, so it is stripped before parsing.
        second_id = next(
            line.replace("*", "").split()[0]
            for line in listing.stdout.splitlines()
            if "Second" in line
        )

        result = runner.invoke(app, ["account", "activate", second_id])
        shown = runner.invoke(app, ["account", "show"])

        assert result.exit_code == 0
        assert "is now active" in result.stdout
        assert "Second" in shown.stdout

    def test_activate_reports_an_unknown_account(self) -> None:
        runner.invoke(app, ["account", "create", "100", "First"])

        result = runner.invoke(app, ["account", "activate", "999"])

        assert result.exit_code != 0
        assert "not found" in result.output

    def test_data_survives_between_invocations(self, cli_env: Path) -> None:
        # The whole point of the vertical slice: the aggregate really persists.
        runner.invoke(app, ["account", "create", "100", "Persisted"])

        result = runner.invoke(app, ["account", "show"])

        assert "Persisted" in result.stdout
        assert (cli_env / "tgassist.db").exists()
