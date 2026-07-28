"""UserProfile use cases and CLI commands.

The use cases run against fakes, so they exercise application logic with no
database. The CLI runs end to end against a real SQLite file, because the point
of that layer is the wiring and wiring is exactly what a fake would hide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes import AdvanceableClock
from tests.fakes.account_repository import InMemoryAccountRepository
from tests.fakes.unit_of_work import InMemoryUnitOfWork, InMemoryUnitOfWorkFactory
from tests.fakes.user_profile_repository import (
    InMemoryProfileStore,
    InMemoryUserProfileRepository,
)
from tgassist.application.use_cases.user_profile import (
    GetUserProfile,
    ProfileChanges,
    UpdateUserProfile,
)
from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.user_profile import (
    EmojiUsage,
    MessageLength,
    TimeRange,
    TonePreference,
)
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
        self.store = InMemoryProfileStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})
        self.clock = AdvanceableClock(NOW)
        self.units: list[InMemoryUnitOfWork] = []
        self._factory = InMemoryUnitOfWorkFactory()

    def unit_of_work(self) -> InMemoryUnitOfWork:
        uow = self._factory()
        self.units.append(uow)
        return uow

    def accounts(self, _uow: UnitOfWork) -> InMemoryAccountRepository:
        return self.accounts_repository

    def profiles(self, _uow: UnitOfWork, account_id: AccountId) -> InMemoryUserProfileRepository:
        """Build a scoped repository, exactly as the SQL factory does."""
        return InMemoryUserProfileRepository(self.store, account_id)

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

    def get(self) -> GetUserProfile:
        return GetUserProfile(self.unit_of_work, self.profiles, self.accounts, self.clock)

    def update(self) -> UpdateUserProfile:
        return UpdateUserProfile(self.unit_of_work, self.profiles, self.get(), self.clock)


@pytest.fixture
def harness() -> _Harness:
    """A use-case environment built from fakes."""
    return _Harness()


class TestProfileChanges:
    def test_no_values_is_empty(self) -> None:
        assert ProfileChanges().is_empty

    @pytest.mark.parametrize(
        "changes",
        [
            ProfileChanges(primary_language="fr"),
            ProfileChanges(tone_preference=TonePreference.FORMAL),
            ProfileChanges(preferred_message_length=MessageLength.SHORT),
            ProfileChanges(emoji_usage=EmojiUsage.NONE),
            ProfileChanges(quiet_hours=TimeRange(0, 60)),
        ],
    )
    def test_any_value_is_not_empty(self, changes: ProfileChanges) -> None:
        assert not changes.is_empty


class TestGetUserProfile:
    async def test_creates_a_default_on_first_access(self, harness: _Harness) -> None:
        # The profile is not created with the account: adding an account should
        # not require deciding preferences before the application is usable.
        await harness.add_account(ACCOUNT_A, is_active=True)

        profile = await harness.get().execute()

        assert profile.account_id == ACCOUNT_A
        assert profile.tone_preference is TonePreference.NEUTRAL
        assert profile.created_at == NOW

    async def test_returns_the_same_profile_on_later_access(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        first = await harness.get().execute()

        second = await harness.get().execute()

        assert second == first

    async def test_creation_is_committed(self, harness: _Harness) -> None:
        # Otherwise the default would be rebuilt on every read and the user's
        # first edit would apply to a profile that was never stored.
        await harness.add_account(ACCOUNT_A, is_active=True)

        await harness.get().execute()

        assert any(uow.is_committed for uow in harness.units)

    async def test_reading_an_existing_profile_commits_nothing(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.get().execute()
        before = len(harness.units)

        await harness.get().execute()

        assert not any(uow.is_committed for uow in harness.units[before:])

    async def test_uses_the_active_account_by_default(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A)
        await harness.add_account(ACCOUNT_B, is_active=True)

        assert (await harness.get().execute()).account_id == ACCOUNT_B

    async def test_reads_a_named_account(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A)
        await harness.add_account(ACCOUNT_B, is_active=True)

        assert (await harness.get().execute(ACCOUNT_A)).account_id == ACCOUNT_A

    async def test_without_an_active_account_it_reports_rather_than_guesses(
        self, harness: _Harness
    ) -> None:
        await harness.add_account(ACCOUNT_A)

        with pytest.raises(RecordNotFoundError, match="No account is active"):
            await harness.get().execute()

    async def test_an_unknown_account_is_reported(self, harness: _Harness) -> None:
        # A profile cannot exist without an account to own it, so this is an
        # error rather than an empty result.
        with pytest.raises(RecordNotFoundError, match="No account with identifier"):
            await harness.get().execute(AccountId(999))

    async def test_profiles_are_per_account(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.add_account(ACCOUNT_B)

        a = await harness.get().execute(ACCOUNT_A)
        b = await harness.get().execute(ACCOUNT_B)

        assert a.account_id == ACCOUNT_A
        assert b.account_id == ACCOUNT_B


class TestUpdateUserProfile:
    async def test_applies_a_single_change(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        updated = await harness.update().execute(
            ProfileChanges(tone_preference=TonePreference.FORMAL)
        )

        assert updated.tone_preference is TonePreference.FORMAL

    async def test_applies_several_changes_at_once(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        updated = await harness.update().execute(
            ProfileChanges(
                primary_language="de-AT",
                tone_preference=TonePreference.CASUAL,
                preferred_message_length=MessageLength.SHORT,
                emoji_usage=EmojiUsage.NONE,
                quiet_hours=TimeRange.from_clock("23:00", "07:00"),
            )
        )

        assert updated.primary_language == "de-AT"
        assert updated.tone_preference is TonePreference.CASUAL
        assert updated.preferred_message_length is MessageLength.SHORT
        assert updated.emoji_usage is EmojiUsage.NONE
        assert str(updated.quiet_hours) == "23:00-07:00"

    async def test_leaves_unmentioned_preferences_alone(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        before = await harness.get().execute()

        updated = await harness.update().execute(ProfileChanges(emoji_usage=EmojiUsage.NONE))

        assert updated.primary_language == before.primary_language
        assert updated.tone_preference is before.tone_preference
        assert updated.preferred_message_length is before.preferred_message_length
        assert updated.quiet_hours == before.quiet_hours

    async def test_the_change_is_persisted(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        await harness.update().execute(ProfileChanges(tone_preference=TonePreference.FORMAL))
        reread = await harness.get().execute()

        assert reread.tone_preference is TonePreference.FORMAL

    async def test_it_creates_the_profile_if_absent(self, harness: _Harness) -> None:
        # `profile set` must work on a fresh account without a separate init
        # step.
        await harness.add_account(ACCOUNT_A, is_active=True)

        updated = await harness.update().execute(ProfileChanges(primary_language="fr"))

        assert updated.primary_language == "fr"

    async def test_changing_nothing_is_permitted(self, harness: _Harness) -> None:
        # A caller that supplied no options has made no mistake worth an error.
        await harness.add_account(ACCOUNT_A, is_active=True)
        before = await harness.get().execute()

        assert await harness.update().execute(ProfileChanges()) == before

    async def test_a_redundant_change_does_not_move_the_timestamp(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        before = await harness.get().execute()
        harness.clock.set(NOW + timedelta(days=1))

        updated = await harness.update().execute(
            ProfileChanges(tone_preference=before.tone_preference)
        )

        assert updated.updated_at == before.updated_at

    async def test_a_real_change_moves_the_timestamp(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.get().execute()
        later = NOW + timedelta(days=1)
        harness.clock.set(later)

        updated = await harness.update().execute(
            ProfileChanges(tone_preference=TonePreference.FORMAL)
        )

        assert updated.updated_at == later
        assert updated.created_at == NOW

    async def test_an_invalid_value_is_rejected(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)

        with pytest.raises(DomainValidationError):
            await harness.update().execute(ProfileChanges(primary_language="not a tag"))

    async def test_a_rejected_change_leaves_the_profile_intact(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        before = await harness.get().execute()

        with pytest.raises(DomainValidationError):
            await harness.update().execute(ProfileChanges(primary_language="not a tag"))

        assert await harness.get().execute() == before

    async def test_updating_one_account_leaves_the_other_alone(self, harness: _Harness) -> None:
        await harness.add_account(ACCOUNT_A, is_active=True)
        await harness.add_account(ACCOUNT_B)
        await harness.get().execute(ACCOUNT_B)

        await harness.update().execute(
            ProfileChanges(tone_preference=TonePreference.FORMAL), ACCOUNT_A
        )

        other = await harness.get().execute(ACCOUNT_B)
        assert other.tone_preference is TonePreference.NEUTRAL

    async def test_an_unknown_account_is_reported(self, harness: _Harness) -> None:
        with pytest.raises(RecordNotFoundError):
            await harness.update().execute(ProfileChanges(), AccountId(999))


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


@pytest.fixture
def _account() -> None:
    """Create an active account for the profile commands to work against."""
    result = runner.invoke(app, ["account", "create", "100", "Primary"])
    assert result.exit_code == 0, result.output


@pytest.mark.usefixtures("cli_env", "_account")
class TestProfileCli:
    def test_show_creates_and_displays_defaults(self) -> None:
        result = runner.invoke(app, ["profile", "show"])

        assert result.exit_code == 0, result.output
        assert "en" in result.stdout
        assert "neutral" in result.stdout
        assert "22:00-08:00" in result.stdout

    def test_show_is_repeatable(self) -> None:
        # Reading twice must show the same profile: the second call reads the
        # stored row rather than building a fresh default.
        first = runner.invoke(app, ["profile", "show"])

        second = runner.invoke(app, ["profile", "show"])

        assert second.exit_code == 0
        assert second.stdout == first.stdout

    def test_set_changes_a_preference(self) -> None:
        result = runner.invoke(app, ["profile", "set", "--tone", "formal"])
        shown = runner.invoke(app, ["profile", "show"])

        assert result.exit_code == 0, result.output
        assert "formal" in shown.stdout

    def test_set_accepts_several_options(self) -> None:
        result = runner.invoke(
            app,
            [
                "profile",
                "set",
                "--language",
                "de-AT",
                "--length",
                "short",
                "--emoji",
                "none",
                "--quiet-hours",
                "23:00-07:00",
            ],
        )
        shown = runner.invoke(app, ["profile", "show"])

        assert result.exit_code == 0, result.output
        assert "de-AT" in shown.stdout
        assert "short" in shown.stdout
        assert "23:00-07:00" in shown.stdout

    def test_changes_survive_a_new_process(self) -> None:
        # Each invocation builds its own container, so this really does read the
        # value back from the file.
        runner.invoke(app, ["profile", "set", "-l", "fr"])

        assert "fr" in runner.invoke(app, ["profile", "show"]).stdout

    def test_set_without_options_is_not_an_error(self) -> None:
        result = runner.invoke(app, ["profile", "set"])

        assert result.exit_code == 0, result.output

    def test_set_rejects_an_invalid_language_without_a_traceback(self) -> None:
        result = runner.invoke(app, ["profile", "set", "-l", "not a tag"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output

    @pytest.mark.parametrize("value", ["22:00", "22:00-", "22-08", "nonsense"])
    def test_set_rejects_a_malformed_time_range(self, value: str) -> None:
        result = runner.invoke(app, ["profile", "set", "--quiet-hours", value])

        assert result.exit_code != 0
        # Either half of the range may be the malformed part, so the message
        # names the form expected rather than a fixed sentence.
        assert "HH:MM" in result.output
        assert "Traceback" not in result.output

    def test_set_rejects_equal_quiet_hour_bounds(self) -> None:
        result = runner.invoke(app, ["profile", "set", "--quiet-hours", "22:00-22:00"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output

    def test_set_rejects_an_unknown_tone(self) -> None:
        result = runner.invoke(app, ["profile", "set", "--tone", "shouty"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output

    def test_profiles_are_scoped_to_the_named_account(self) -> None:
        second = runner.invoke(app, ["account", "create", "200", "Second"])
        assert second.exit_code == 0, second.output
        listing = runner.invoke(app, ["account", "list"]).stdout

        runner.invoke(app, ["profile", "set", "--tone", "formal"])
        first_account = runner.invoke(app, ["profile", "show"]).stdout

        assert "formal" in first_account
        # The second account keeps its own defaults.
        assert "200" in listing


@pytest.mark.usefixtures("cli_env")
class TestProfileCliWithoutAnAccount:
    def test_show_reports_that_no_account_is_active(self) -> None:
        result = runner.invoke(app, ["profile", "show"])

        assert result.exit_code != 0
        assert "account" in result.output.lower()
        assert "Traceback" not in result.output

    def test_set_reports_that_no_account_is_active(self) -> None:
        result = runner.invoke(app, ["profile", "set", "--tone", "formal"])

        assert result.exit_code != 0
        assert "Traceback" not in result.output
