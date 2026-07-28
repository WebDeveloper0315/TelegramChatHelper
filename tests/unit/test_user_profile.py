"""UserProfile aggregate, mapper and migration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.user_profile import (
    MINUTES_PER_DAY,
    EmojiUsage,
    MessageLength,
    TimeRange,
    TonePreference,
    UserProfile,
    validate_language,
)
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqliteDatabase,
    UserProfileMapper,
)
from tgassist.infrastructure.persistence.mapper import column_names
from tgassist.infrastructure.persistence.schema import USER_PROFILES_TABLE, user_profiles

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT = AccountId(7)


# ---------------------------------------------------------------------------
# TimeRange
# ---------------------------------------------------------------------------


class TestTimeRange:
    def test_parses_clock_times(self) -> None:
        assert TimeRange.from_clock("22:00", "08:00") == TimeRange(1320, 480)

    def test_renders_as_clock_times(self) -> None:
        assert str(TimeRange(1320, 480)) == "22:00-08:00"

    def test_detects_a_wrapping_range(self) -> None:
        assert TimeRange.from_clock("22:00", "08:00").wraps_midnight
        assert not TimeRange.from_clock("09:00", "17:00").wraps_midnight

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [("22:00", "08:00", 600), ("09:00", "17:00", 480), ("23:59", "00:01", 2)],
    )
    def test_computes_duration(self, start: str, end: str, expected: int) -> None:
        assert TimeRange.from_clock(start, end).duration_minutes == expected

    @pytest.mark.parametrize(
        ("minute", "inside"),
        [(1320, True), (1439, True), (0, True), (479, True), (480, False), (720, False)],
    )
    def test_containment_across_midnight(self, minute: int, inside: bool) -> None:
        # The case a naive start <= x < end comparison gets wrong.
        assert TimeRange.from_clock("22:00", "08:00").contains(minute) is inside

    @pytest.mark.parametrize(
        ("minute", "inside"), [(540, True), (1019, True), (1020, False), (0, False)]
    )
    def test_containment_within_a_day(self, minute: int, inside: bool) -> None:
        assert TimeRange.from_clock("09:00", "17:00").contains(minute) is inside

    def test_containment_from_a_local_time(self) -> None:
        quiet = TimeRange.from_clock("22:00", "08:00")

        assert quiet.contains_local_time(datetime(2026, 1, 1, 23, 30, tzinfo=UTC))
        assert not quiet.contains_local_time(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

    def test_rejects_equal_bounds(self) -> None:
        # Ambiguous between empty and whole-day, and the documented invariant
        # forbids the whole day, so the ambiguity is rejected.
        with pytest.raises(DomainValidationError, match="ambiguous"):
            TimeRange(540, 540)

    @pytest.mark.parametrize("value", [-1, MINUTES_PER_DAY, 5000])
    def test_rejects_bounds_outside_a_day(self, value: int) -> None:
        with pytest.raises(DomainValidationError, match="between 0"):
            TimeRange(value, 100)

    @pytest.mark.parametrize("value", ["25:00", "12:60", "noon", "12", "12:0"])
    def test_rejects_malformed_clock_times(self, value: str) -> None:
        with pytest.raises(DomainValidationError):
            TimeRange.from_clock(value, "08:00")

    def test_is_immutable(self) -> None:
        quiet = TimeRange(0, 60)

        with pytest.raises((AttributeError, TypeError), match=r"cannot assign|frozen"):
            quiet.start_minute = 5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------


class TestLanguageValidation:
    @pytest.mark.parametrize(
        ("given", "normalised"),
        [("en", "en"), ("EN", "en"), ("en-gb", "en-GB"), ("EN-GB", "en-GB"), ("  fr  ", "fr")],
    )
    def test_normalises(self, given: str, normalised: str) -> None:
        assert validate_language(given) == normalised

    @pytest.mark.parametrize("value", ["", "  ", "e", "english!", "en_GB", "123"])
    def test_rejects_malformed_tags(self, value: str) -> None:
        with pytest.raises(DomainValidationError):
            validate_language(value)

    def test_accepts_an_unregistered_but_well_formed_tag(self) -> None:
        # Structural validation only. An unregistered tag simply matches
        # nothing; a malformed one breaks parsing everywhere it is used.
        assert validate_language("zz") == "zz"


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


class TestUserProfileConstruction:
    def test_defaults_are_complete(self) -> None:
        # Every field has a defensible default, which is what allows the profile
        # to be created on first access rather than demanding a setup step.
        profile = UserProfile.default_for(ACCOUNT, NOW)

        assert profile.account_id == ACCOUNT
        assert profile.primary_language == "en"
        assert profile.tone_preference is TonePreference.NEUTRAL
        assert profile.preferred_message_length is MessageLength.MEDIUM
        assert profile.emoji_usage is EmojiUsage.SPARING
        assert str(profile.quiet_hours) == "22:00-08:00"
        assert profile.created_at == profile.updated_at == NOW

    def test_identity_is_the_account(self) -> None:
        # No surrogate key: exactly one profile per account, so the account
        # identifier already identifies it uniquely (ADR-038).
        assert not hasattr(UserProfile.default_for(ACCOUNT, NOW), "id")


class TestUserProfileValidation:
    @pytest.mark.parametrize("account_id", [0, -1])
    def test_rejects_a_non_positive_account(self, account_id: int) -> None:
        with pytest.raises(DomainValidationError, match="positive"):
            UserProfile.default_for(AccountId(account_id), NOW)

    def test_rejects_a_malformed_language(self) -> None:
        with pytest.raises(DomainValidationError):
            replace(UserProfile.default_for(ACCOUNT, NOW), primary_language="not a tag")

    def test_rejects_a_naive_timestamp(self) -> None:
        with pytest.raises(DomainValidationError, match="timezone-aware"):
            replace(
                UserProfile.default_for(ACCOUNT, NOW),
                created_at=datetime(2026, 1, 1),  # noqa: DTZ001
            )

    def test_rejects_a_non_utc_timestamp(self) -> None:
        tokyo = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))

        with pytest.raises(DomainValidationError, match="must be UTC"):
            replace(UserProfile.default_for(ACCOUNT, NOW), created_at=tokyo, updated_at=tokyo)

    def test_rejects_an_update_before_creation(self) -> None:
        with pytest.raises(DomainValidationError, match="updated before"):
            replace(UserProfile.default_for(ACCOUNT, NOW), updated_at=NOW - timedelta(days=1))


class TestUserProfileChanges:
    def test_is_immutable(self) -> None:
        profile = UserProfile.default_for(ACCOUNT, NOW)

        with pytest.raises((AttributeError, TypeError), match=r"cannot assign|frozen"):
            profile.primary_language = "fr"  # type: ignore[misc]

    def test_changing_a_value_returns_a_new_instance(self) -> None:
        profile = UserProfile.default_for(ACCOUNT, NOW)
        later = NOW + timedelta(hours=1)

        changed = profile.with_tone(TonePreference.FORMAL, later)

        assert changed is not profile
        assert changed.tone_preference is TonePreference.FORMAL
        assert profile.tone_preference is TonePreference.NEUTRAL
        assert changed.updated_at == later

    @pytest.mark.parametrize(
        ("method", "value"),
        [
            ("with_language", "en"),
            ("with_tone", TonePreference.NEUTRAL),
            ("with_message_length", MessageLength.MEDIUM),
            ("with_emoji_usage", EmojiUsage.SPARING),
            ("with_quiet_hours", TimeRange(1320, 480)),
        ],
    )
    def test_setting_the_current_value_is_a_no_op(self, method: str, value: object) -> None:
        # A redundant edit must not move updated_at and make nothing look like
        # something.
        profile = UserProfile.default_for(ACCOUNT, NOW)

        assert getattr(profile, method)(value, NOW + timedelta(hours=1)) is profile

    def test_language_is_normalised_on_change(self) -> None:
        profile = UserProfile.default_for(ACCOUNT, NOW)

        assert profile.with_language("EN-gb", NOW).primary_language == "en-GB"

    def test_ownership_survives_changes(self) -> None:
        profile = UserProfile.default_for(ACCOUNT, NOW)

        assert profile.with_tone(TonePreference.CASUAL, NOW).account_id == ACCOUNT


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


class _FakeRow:
    """A row-like object for testing the mapper without a database."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        return self._values[name]


class TestUserProfileMapper:
    def test_round_trip_preserves_every_field(self) -> None:
        mapper = UserProfileMapper()
        original = (
            UserProfile.default_for(ACCOUNT, NOW)
            .with_tone(TonePreference.MIRROR_CONTACT, NOW)
            .with_emoji_usage(EmojiUsage.NONE, NOW)
            .with_language("de-AT", NOW)
            .with_quiet_hours(TimeRange.from_clock("23:15", "06:45"), NOW)
        )

        restored = mapper.to_domain(_FakeRow(mapper.to_params(original)))

        assert restored == original

    def test_covers_every_column(self) -> None:
        written = column_names(UserProfileMapper().to_params(UserProfile.default_for(ACCOUNT, NOW)))
        declared = {column.name for column in user_profiles.columns}

        assert declared == written

    def test_stores_enumerations_as_their_values(self) -> None:
        # Not ordinals: an ordinal silently changes meaning if a member is ever
        # inserted mid-enum, and it makes the stored file unreadable.
        params = UserProfileMapper().to_params(UserProfile.default_for(ACCOUNT, NOW))

        assert params["tone_preference"] == "neutral"
        assert params["emoji_usage"] == "sparing"

    def test_is_pure(self) -> None:
        mapper = UserProfileMapper()
        profile = UserProfile.default_for(ACCOUNT, NOW)

        assert mapper.to_params(profile) == mapper.to_params(profile)

    def test_reads_text_timestamps(self) -> None:
        params = UserProfileMapper().to_params(UserProfile.default_for(ACCOUNT, NOW))
        params["created_at"] = NOW.isoformat()
        params["updated_at"] = NOW.isoformat()

        assert UserProfileMapper().to_domain(_FakeRow(params)).created_at == NOW


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SqliteDatabase]:
    """A connected database with no schema applied."""
    db = SqliteDatabase(DatabaseSection(path=tmp_path / "profiles.db"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def _tables(database: SqliteDatabase) -> list[str]:
    return await database.executor.run(
        lambda: list(
            database.connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).scalars()
        )
    )


async def _insert_account(database: SqliteDatabase, account_id: int) -> None:
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO accounts (id, telegram_user_id, display_name, timezone, "
                "is_active, created_at, updated_at) "
                "VALUES (:id, :id, 'x', 'UTC', 0, '2026-01-01', '2026-01-01')"
            ),
            {"id": account_id},
        )
    )


async def _insert_profile(database: SqliteDatabase, account_id: int, **overrides: object) -> None:
    values: dict[str, object] = {
        "account_id": account_id,
        "primary_language": "en",
        "tone_preference": "neutral",
        "preferred_message_length": "medium",
        "emoji_usage": "sparing",
        "quiet_hours_start_minute": 1320,
        "quiet_hours_end_minute": 480,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
    }
    values.update(overrides)
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO user_profiles (account_id, primary_language, tone_preference, "
                "preferred_message_length, emoji_usage, quiet_hours_start_minute, "
                "quiet_hours_end_minute, created_at, updated_at) "
                "VALUES (:account_id, :primary_language, :tone_preference, "
                ":preferred_message_length, :emoji_usage, :quiet_hours_start_minute, "
                ":quiet_hours_end_minute, :created_at, :updated_at)"
            ),
            values,
        )
    )


async def _profile_count(database: SqliteDatabase) -> int:
    return await database.executor.run(
        lambda: database.connection.execute(text("SELECT COUNT(*) FROM user_profiles")).scalar_one()
    )


class TestUserProfilesMigration:
    async def test_creates_the_table(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        assert USER_PROFILES_TABLE in await _tables(database)

    async def test_head_is_the_profiles_revision(self, database: SqliteDatabase) -> None:
        assert AlembicMigrationRunner(database).head_revision() == "0003"

    async def test_round_trips_up_down_up(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)

        await runner.upgrade()
        await runner.downgrade("0002")
        assert USER_PROFILES_TABLE not in await _tables(database)

        await runner.upgrade()
        assert USER_PROFILES_TABLE in await _tables(database)

    async def test_downgrade_leaves_accounts_intact(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()

        await runner.downgrade("0002")

        assert "accounts" in await _tables(database)


class TestForeignKeyIntegrity:
    async def test_a_profile_requires_an_existing_account(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        with pytest.raises(Exception, match="FOREIGN KEY constraint"):
            await _insert_profile(database, account_id=999)

    async def test_foreign_keys_are_actually_enforced(self, database: SqliteDatabase) -> None:
        # SQLite ignores foreign keys unless PRAGMA foreign_keys=ON, which the
        # engine applies per connection. Without it this table's cascade would
        # be decorative, so it is worth asserting directly.
        await AlembicMigrationRunner(database).upgrade()

        health = await database.health()

        assert health.pragmas is not None
        assert health.pragmas.foreign_keys is True


class TestCascadeDeletion:
    async def test_deleting_an_account_deletes_its_profile(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)
        await _insert_profile(database, 1)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM accounts WHERE id = 1"))
        )

        assert await _profile_count(database) == 0

    async def test_deleting_one_account_leaves_others(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        for account_id in (1, 2):
            await _insert_account(database, account_id)
            await _insert_profile(database, account_id)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM accounts WHERE id = 1"))
        )

        assert await _profile_count(database) == 1


class TestCheckConstraints:
    async def test_one_profile_per_account(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)
        await _insert_profile(database, 1)

        with pytest.raises(Exception, match=r"UNIQUE constraint|PRIMARY KEY"):
            await _insert_profile(database, 1)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"tone_preference": "shouty"},
            {"preferred_message_length": "enormous"},
            {"emoji_usage": "constant"},
            {"quiet_hours_start_minute": 1440},
            {"quiet_hours_end_minute": -1},
            {"quiet_hours_start_minute": 600, "quiet_hours_end_minute": 600},
            {"created_at": "2026-06-01", "updated_at": "2026-01-01"},
        ],
    )
    async def test_invalid_rows_are_refused(
        self, database: SqliteDatabase, overrides: dict[str, object]
    ) -> None:
        # The schema restates the entity's invariants, so a row written by any
        # route -- a repair script, a future migration -- cannot violate them.
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)

        with pytest.raises(Exception, match="CHECK constraint"):
            await _insert_profile(database, 1, **overrides)

    async def test_valid_rows_are_accepted(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)

        await _insert_profile(database, 1, tone_preference="mirror_contact", emoji_usage="none")

        assert await _profile_count(database) == 1
