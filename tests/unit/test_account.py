"""Account aggregate, mapper and migration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.account import (
    DEFAULT_TIMEZONE,
    MAX_DISPLAY_NAME_LENGTH,
    Account,
    validate_timezone,
)
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AccountMapper,
    AlembicMigrationRunner,
    SqliteDatabase,
)
from tgassist.infrastructure.persistence.mapper import column_names
from tgassist.infrastructure.persistence.schema import ACCOUNTS_TABLE, accounts

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_account(  # noqa: PLR0913 - a builder takes one argument per field
    *,
    account_id: int = 1,
    telegram_user_id: int = 100,
    display_name: str = "Primary",
    timezone_name: str = DEFAULT_TIMEZONE,
    is_active: bool = False,
    now: datetime = NOW,
) -> Account:
    """Build a valid account for testing."""
    return Account.create(
        account_id=AccountId(account_id),
        telegram_user_id=TelegramUserId(telegram_user_id),
        display_name=display_name,
        timezone=timezone_name,
        is_active=is_active,
        now=now,
    )


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


class TestAccountConstruction:
    def test_create_sets_both_timestamps_to_now(self) -> None:
        account = make_account()

        assert account.created_at == NOW
        assert account.updated_at == NOW

    def test_create_trims_the_display_name(self) -> None:
        # A name differing only by whitespace is the same name, and would
        # otherwise defeat comparison and produce confusing duplicates.
        assert make_account(display_name="  Primary  ").display_name == "Primary"

    def test_defaults_to_inactive(self) -> None:
        assert make_account().is_active is False

    def test_defaults_to_utc(self) -> None:
        assert make_account().timezone == "UTC"


class TestAccountValidation:
    @pytest.mark.parametrize("account_id", [0, -1])
    def test_rejects_a_non_positive_identifier(self, account_id: int) -> None:
        with pytest.raises(DomainValidationError, match="positive"):
            make_account(account_id=account_id)

    @pytest.mark.parametrize("telegram_user_id", [0, -5])
    def test_rejects_a_non_positive_telegram_id(self, telegram_user_id: int) -> None:
        with pytest.raises(DomainValidationError, match="positive"):
            make_account(telegram_user_id=telegram_user_id)

    @pytest.mark.parametrize("name", ["", "   ", "\t\n"])
    def test_rejects_a_blank_display_name(self, name: str) -> None:
        with pytest.raises(DomainValidationError, match="display name"):
            make_account(display_name=name)

    def test_rejects_an_overlong_display_name(self) -> None:
        with pytest.raises(DomainValidationError, match="at most"):
            make_account(display_name="x" * (MAX_DISPLAY_NAME_LENGTH + 1))

    def test_accepts_a_display_name_at_the_limit(self) -> None:
        assert len(make_account(display_name="x" * MAX_DISPLAY_NAME_LENGTH).display_name) == (
            MAX_DISPLAY_NAME_LENGTH
        )

    def test_rejects_an_unknown_timezone(self) -> None:
        with pytest.raises(DomainValidationError, match="not a known IANA"):
            make_account(timezone_name="Mars/Olympus")

    def test_rejects_an_empty_timezone(self) -> None:
        with pytest.raises(DomainValidationError, match="required"):
            make_account(timezone_name="")

    def test_rejects_a_fixed_offset_as_a_timezone(self) -> None:
        # An offset cannot express daylight saving, so it is wrong for half of
        # every subsequent year -- which matters for reply-timing advice.
        with pytest.raises(DomainValidationError):
            make_account(timezone_name="+01:00")

    @pytest.mark.parametrize("zone", ["UTC", "Europe/London", "America/New_York", "Asia/Tokyo"])
    def test_accepts_known_iana_identifiers(self, zone: str) -> None:
        assert validate_timezone(zone) == zone

    def test_rejects_a_naive_timestamp(self) -> None:
        with pytest.raises(DomainValidationError, match="timezone-aware"):
            Account(
                id=AccountId(1),
                telegram_user_id=TelegramUserId(1),
                display_name="x",
                timezone="UTC",
                is_active=False,
                created_at=datetime(2026, 1, 1),  # noqa: DTZ001
                updated_at=NOW,
            )

    def test_rejects_a_non_utc_timestamp(self) -> None:
        tokyo = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))

        with pytest.raises(DomainValidationError, match="must be UTC"):
            Account(
                id=AccountId(1),
                telegram_user_id=TelegramUserId(1),
                display_name="x",
                timezone="UTC",
                is_active=False,
                created_at=tokyo,
                updated_at=tokyo,
            )

    def test_rejects_an_update_before_creation(self) -> None:
        with pytest.raises(DomainValidationError, match="updated before"):
            replace(make_account(), updated_at=NOW - timedelta(days=1))

    def test_validation_errors_are_also_value_errors(self) -> None:
        # Dual inheritance: idiomatic `except ValueError` still works, while the
        # error carries a code and a user-facing message.
        with pytest.raises(ValueError, match="positive"):
            make_account(account_id=0)


class TestAccountTransitions:
    def test_is_immutable(self) -> None:
        account = make_account()

        with pytest.raises((AttributeError, TypeError), match=r"cannot assign|frozen"):
            account.display_name = "other"  # type: ignore[misc]

    def test_activation_returns_a_new_instance(self) -> None:
        account = make_account()
        later = NOW + timedelta(hours=1)

        activated = account.activated(later)

        assert activated is not account
        assert activated.is_active
        assert account.is_active is False
        assert activated.updated_at == later

    def test_activating_an_active_account_is_a_no_op(self) -> None:
        # Returning self keeps updated_at still, so a no-op does not look like
        # a change to anything watching that column.
        account = make_account(is_active=True)

        assert account.activated(NOW + timedelta(hours=1)) is account

    def test_deactivation_returns_a_new_instance(self) -> None:
        account = make_account(is_active=True)
        later = NOW + timedelta(hours=1)

        deactivated = account.deactivated(later)

        assert deactivated.is_active is False
        assert deactivated.updated_at == later

    def test_deactivating_an_inactive_account_is_a_no_op(self) -> None:
        account = make_account()

        assert account.deactivated(NOW + timedelta(hours=1)) is account

    def test_renaming_trims_and_updates(self) -> None:
        later = NOW + timedelta(hours=1)

        renamed = make_account().renamed("  Renamed  ", later)

        assert renamed.display_name == "Renamed"
        assert renamed.updated_at == later

    def test_renaming_to_the_same_name_is_a_no_op(self) -> None:
        account = make_account(display_name="Primary")

        assert account.renamed("Primary", NOW + timedelta(hours=1)) is account

    def test_identity_survives_transitions(self) -> None:
        account = make_account()

        assert account.activated(NOW).id == account.id


class TestAccountEquality:
    def test_equal_by_value(self) -> None:
        assert make_account() == make_account()

    def test_differs_by_any_field(self) -> None:
        assert make_account() != make_account(display_name="Other")


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


class _FakeRow:
    """A row-like object for testing the mapper without a database."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        return self._values[name]


class TestAccountMapper:
    def test_round_trip_preserves_every_field(self) -> None:
        mapper = AccountMapper()
        original = make_account(
            account_id=42, telegram_user_id=999, timezone_name="Europe/Berlin", is_active=True
        )

        restored = mapper.to_domain(_FakeRow(mapper.to_params(original)))

        assert restored == original

    def test_covers_every_column(self) -> None:
        # Fails the moment a migration adds a column the mapper does not write.
        written = column_names(AccountMapper().to_params(make_account()))
        declared = {column.name for column in accounts.columns}

        assert declared == written

    def test_is_pure(self) -> None:
        mapper = AccountMapper()
        account = make_account()

        assert mapper.to_params(account) == mapper.to_params(account)

    def test_reads_an_integer_boolean(self) -> None:
        # SQLite has no native boolean; the driver may return 0 or 1.
        params = AccountMapper().to_params(make_account(is_active=True))
        params["is_active"] = 1

        assert AccountMapper().to_domain(_FakeRow(params)).is_active is True

    def test_reads_a_text_timestamp(self) -> None:
        params = AccountMapper().to_params(make_account())
        params["created_at"] = NOW.isoformat()
        params["updated_at"] = NOW.isoformat()

        assert AccountMapper().to_domain(_FakeRow(params)).created_at == NOW


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SqliteDatabase]:
    """A connected database with no schema applied."""
    db = SqliteDatabase(DatabaseSection(path=tmp_path / "accounts.db"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def _table_names(database: SqliteDatabase) -> list[str]:
    return await database.executor.run(
        lambda: list(
            database.connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).scalars()
        )
    )


async def _index_names(database: SqliteDatabase) -> list[str]:
    return await database.executor.run(
        lambda: list(
            database.connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='index'")
            ).scalars()
        )
    )


class TestAccountsMigration:
    async def test_creates_the_table(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        assert ACCOUNTS_TABLE in await _table_names(database)

    async def test_creates_both_unique_indexes(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        names = await _index_names(database)

        assert "uq_accounts_telegram_user_id" in names
        assert "uq_accounts_single_active" in names

    async def test_round_trips_up_down_up(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)

        await runner.upgrade()
        await runner.downgrade("0001")
        assert ACCOUNTS_TABLE not in await _table_names(database)

        await runner.upgrade()
        assert ACCOUNTS_TABLE in await _table_names(database)

    async def test_upgrading_reaches_the_accounts_revision(self, database: SqliteDatabase) -> None:
        # Not pinned to head: head moves with every new migration, and asserting
        # it here would fail the next milestone for no reason. What matters is
        # that a plain upgrade applies this table.
        await AlembicMigrationRunner(database).upgrade()

        applied = await AlembicMigrationRunner(database).current_revision()

        assert ACCOUNTS_TABLE in await _table_names(database)
        assert applied is not None
        assert applied >= "0002"

    async def test_check_constraints_reject_invalid_rows(self, database: SqliteDatabase) -> None:
        # The schema restates the entity's invariants, so a row written by any
        # route -- a repair script, a future migration -- cannot violate them.
        await AlembicMigrationRunner(database).upgrade()

        def insert_bad() -> None:
            database.connection.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, telegram_user_id, display_name, timezone, is_active, "
                    " created_at, updated_at) "
                    "VALUES (-1, 1, 'x', 'UTC', 0, '2026-01-01', '2026-01-01')"
                )
            )

        with pytest.raises(Exception, match="CHECK constraint"):
            await database.executor.run(insert_bad)

    async def test_single_active_index_rejects_a_second_active_row(
        self, database: SqliteDatabase
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()

        def insert(account_id: int, telegram_id: int) -> None:
            database.connection.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, telegram_user_id, display_name, timezone, is_active, "
                    " created_at, updated_at) "
                    "VALUES (:id, :tg, 'x', 'UTC', 1, '2026-01-01', '2026-01-01')"
                ),
                {"id": account_id, "tg": telegram_id},
            )

        await database.executor.run(insert, 1, 1)

        with pytest.raises(Exception, match="UNIQUE constraint"):
            await database.executor.run(insert, 2, 2)

    async def test_many_inactive_rows_are_permitted(self, database: SqliteDatabase) -> None:
        # A partial index constrains only the active rows.
        await AlembicMigrationRunner(database).upgrade()

        def insert(account_id: int, telegram_id: int) -> None:
            database.connection.execute(
                text(
                    "INSERT INTO accounts "
                    "(id, telegram_user_id, display_name, timezone, is_active, "
                    " created_at, updated_at) "
                    "VALUES (:id, :tg, 'x', 'UTC', 0, '2026-01-01', '2026-01-01')"
                ),
                {"id": account_id, "tg": telegram_id},
            )

        for index in range(1, 4):
            await database.executor.run(insert, index, index)
