"""Contact aggregate, mapper and migration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.contact import (
    MAX_DISPLAY_NAME_LENGTH,
    Contact,
    validate_username,
)
from tgassist.domain.model.identifiers import AccountId, ContactId, TelegramUserId
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    ContactMapper,
    SqliteDatabase,
)
from tgassist.infrastructure.persistence.mapper import column_names
from tgassist.infrastructure.persistence.schema import CONTACTS_TABLE, contacts

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
ACCOUNT = AccountId(7)
CONTACT = ContactId(11)
TELEGRAM = TelegramUserId(555)


def make_contact(**overrides: Any) -> Contact:
    """Build a valid contact, with fields overridden as needed."""
    base = Contact.create(
        contact_id=CONTACT,
        account_id=ACCOUNT,
        telegram_user_id=TELEGRAM,
        display_name="Alice Example",
        username="alice_example",
        now=NOW,
    )
    return replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# Username
# ---------------------------------------------------------------------------


class TestUsernameValidation:
    @pytest.mark.parametrize(
        ("given", "normalised"),
        [
            ("alice", "alice"),
            ("@alice", "alice"),
            ("  @alice  ", "alice"),
            ("Alice_99", "Alice_99"),
            ("a" * 32, "a" * 32),
        ],
    )
    def test_normalises(self, given: str, normalised: str) -> None:
        assert validate_username(given) == normalised

    def test_case_is_preserved(self) -> None:
        # Telegram treats handles case-insensitively but displays them as set,
        # and lowercasing would show a contact a name they did not choose.
        assert validate_username("AliceExample") == "AliceExample"

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "@",
            "abc",  # too short
            "a" * 33,  # too long
            "1alice",  # must start with a letter
            "alice_",  # must not end with an underscore
            "ali ce",
            "alice-99",
            "ali.ce",
        ],
    )
    def test_rejects_malformed_handles(self, value: str) -> None:
        with pytest.raises(DomainValidationError):
            validate_username(value)

    def test_a_blank_username_says_to_omit_it(self) -> None:
        # Rather than silently storing None: the caller asked for something, and
        # guessing what they meant is how empty strings end up in a database.
        with pytest.raises(DomainValidationError, match="blank") as excinfo:
            validate_username("  ")

        assert "Omit it" in excinfo.value.user_message


# ---------------------------------------------------------------------------
# Construction and validation
# ---------------------------------------------------------------------------


class TestContactConstruction:
    def test_a_new_contact_is_active(self) -> None:
        contact = make_contact()

        assert contact.is_active
        assert not contact.is_archived
        assert not contact.is_deleted
        assert contact.status == "active"

    def test_creation_timestamps_match(self) -> None:
        contact = make_contact()

        assert contact.created_at == contact.updated_at == NOW

    def test_the_display_name_is_trimmed(self) -> None:
        contact = Contact.create(
            contact_id=CONTACT,
            account_id=ACCOUNT,
            telegram_user_id=TELEGRAM,
            display_name="  Alice  ",
            now=NOW,
        )

        assert contact.display_name == "Alice"

    def test_the_username_is_optional(self) -> None:
        contact = Contact.create(
            contact_id=CONTACT,
            account_id=ACCOUNT,
            telegram_user_id=TELEGRAM,
            display_name="Alice",
            now=NOW,
        )

        assert contact.username is None

    def test_a_leading_at_is_stripped_on_creation(self) -> None:
        assert make_contact().username == "alice_example"


class TestContactValidation:
    @pytest.mark.parametrize("field", ["id", "account_id", "telegram_user_id"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_identifiers(self, field: str, value: int) -> None:
        with pytest.raises(DomainValidationError, match="positive"):
            make_contact(**{field: value})

    @pytest.mark.parametrize("name", ["", "   "])
    def test_rejects_a_blank_display_name(self, name: str) -> None:
        with pytest.raises(DomainValidationError, match="non-empty display name"):
            make_contact(display_name=name)

    def test_rejects_an_over_long_display_name(self) -> None:
        with pytest.raises(DomainValidationError, match="at most"):
            make_contact(display_name="x" * (MAX_DISPLAY_NAME_LENGTH + 1))

    def test_rejects_a_malformed_username(self) -> None:
        with pytest.raises(DomainValidationError):
            make_contact(username="no")

    def test_rejects_a_naive_timestamp(self) -> None:
        with pytest.raises(DomainValidationError, match="timezone-aware"):
            make_contact(created_at=datetime(2026, 1, 1))  # noqa: DTZ001

    def test_rejects_a_non_utc_timestamp(self) -> None:
        tokyo = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))

        with pytest.raises(DomainValidationError, match="must be UTC"):
            make_contact(created_at=tokyo, updated_at=tokyo)

    def test_rejects_a_naive_lifecycle_timestamp(self) -> None:
        with pytest.raises(DomainValidationError, match="timezone-aware"):
            make_contact(archived_at=datetime(2026, 1, 1))  # noqa: DTZ001

    def test_rejects_an_update_before_creation(self) -> None:
        with pytest.raises(DomainValidationError, match="updated before"):
            make_contact(updated_at=NOW - timedelta(days=1))

    def test_rejects_being_archived_and_deleted_at_once(self) -> None:
        # The two states are mutually exclusive, not two flags that happen to be
        # usually apart.
        with pytest.raises(DomainValidationError, match="archived and deleted"):
            make_contact(archived_at=LATER, deleted_at=LATER)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestContactLifecycle:
    def test_is_immutable(self) -> None:
        contact = make_contact()

        with pytest.raises((AttributeError, TypeError), match=r"cannot assign|frozen"):
            contact.display_name = "Other"  # type: ignore[misc]

    def test_archiving_returns_a_new_instance(self) -> None:
        contact = make_contact()

        archived = contact.archived(LATER)

        assert archived is not contact
        assert archived.is_archived
        assert contact.is_active
        assert archived.archived_at == LATER
        assert archived.updated_at == LATER

    def test_archiving_twice_is_a_no_op(self) -> None:
        archived = make_contact().archived(LATER)

        assert archived.archived(LATER + timedelta(days=1)) is archived

    def test_deleting_returns_a_new_instance(self) -> None:
        deleted = make_contact().deleted(LATER)

        assert deleted.is_deleted
        assert deleted.deleted_at == LATER
        assert deleted.status == "deleted"

    def test_deleting_twice_is_a_no_op(self) -> None:
        deleted = make_contact().deleted(LATER)

        assert deleted.deleted(LATER + timedelta(days=1)) is deleted

    def test_deleting_an_archived_contact_clears_the_archive(self) -> None:
        # Otherwise restoring would silently return it to the archive rather
        # than to active, which is not what the user asked for.
        deleted = make_contact().archived(LATER).deleted(LATER)

        assert deleted.is_deleted
        assert deleted.archived_at is None

    def test_archiving_a_deleted_contact_is_refused(self) -> None:
        deleted = make_contact().deleted(LATER)

        with pytest.raises(DomainValidationError, match="deleted and cannot be archived"):
            deleted.archived(LATER)

    @pytest.mark.parametrize("state", ["archived", "deleted"])
    def test_restoring_returns_a_contact_to_active(self, state: str) -> None:
        contact = getattr(make_contact(), state)(LATER)

        restored = contact.restored(LATER + timedelta(hours=1))

        assert restored.is_active
        assert restored.archived_at is None
        assert restored.deleted_at is None

    def test_restoring_an_active_contact_is_a_no_op(self) -> None:
        contact = make_contact()

        assert contact.restored(LATER) is contact

    def test_the_creation_time_survives_every_transition(self) -> None:
        contact = make_contact().archived(LATER).deleted(LATER).restored(LATER)

        assert contact.created_at == NOW

    def test_identity_survives_every_transition(self) -> None:
        contact = make_contact().archived(LATER).deleted(LATER).restored(LATER)

        assert contact.id == CONTACT
        assert contact.account_id == ACCOUNT
        assert contact.telegram_user_id == TELEGRAM


class TestContactEdits:
    def test_renaming_returns_a_new_instance(self) -> None:
        renamed = make_contact().renamed("Alice B", LATER)

        assert renamed.display_name == "Alice B"
        assert renamed.updated_at == LATER

    def test_renaming_trims(self) -> None:
        assert make_contact().renamed("  Alice B  ", LATER).display_name == "Alice B"

    def test_renaming_to_the_same_name_is_a_no_op(self) -> None:
        contact = make_contact()

        assert contact.renamed("Alice Example", LATER) is contact

    def test_renaming_to_a_blank_name_is_refused(self) -> None:
        with pytest.raises(DomainValidationError):
            make_contact().renamed("   ", LATER)

    def test_setting_a_username_returns_a_new_instance(self) -> None:
        changed = make_contact().with_username("@alice_b", LATER)

        assert changed.username == "alice_b"
        assert changed.updated_at == LATER

    def test_clearing_a_username(self) -> None:
        # Telegram handles can be given up, so this is a real transition rather
        # than a way to blank a field.
        assert make_contact().with_username(None, LATER).username is None

    def test_setting_the_same_username_is_a_no_op(self) -> None:
        contact = make_contact()

        assert contact.with_username("alice_example", LATER) is contact

    def test_setting_the_same_username_with_an_at_is_a_no_op(self) -> None:
        contact = make_contact()

        assert contact.with_username("@alice_example", LATER) is contact


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


class _FakeRow:
    """A row-like object for testing the mapper without a database."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        return self._values[name]


class TestContactMapper:
    def test_round_trip_preserves_every_field(self) -> None:
        mapper = ContactMapper()
        original = make_contact().archived(LATER)

        assert mapper.to_domain(_FakeRow(mapper.to_params(original))) == original

    def test_round_trip_preserves_an_absent_username(self) -> None:
        mapper = ContactMapper()
        original = make_contact(username=None)

        assert mapper.to_domain(_FakeRow(mapper.to_params(original))).username is None

    def test_round_trip_preserves_deletion(self) -> None:
        mapper = ContactMapper()
        original = make_contact().deleted(LATER)

        restored = mapper.to_domain(_FakeRow(mapper.to_params(original)))

        assert restored.is_deleted
        assert restored.deleted_at == LATER

    def test_covers_every_column(self) -> None:
        # Fails the moment a migration adds a column the mapper does not write.
        written = column_names(ContactMapper().to_params(make_contact()))
        declared = {column.name for column in contacts.columns}

        assert declared == written

    def test_is_pure(self) -> None:
        mapper = ContactMapper()
        contact = make_contact()

        assert mapper.to_params(contact) == mapper.to_params(contact)

    def test_reads_text_timestamps(self) -> None:
        params = ContactMapper().to_params(make_contact().archived(LATER))
        for key in ("created_at", "updated_at", "archived_at"):
            params[key] = params[key].isoformat()

        restored = ContactMapper().to_domain(_FakeRow(params))

        assert restored.created_at == NOW
        assert restored.archived_at == LATER


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SqliteDatabase]:
    """A connected database with no schema applied."""
    db = SqliteDatabase(DatabaseSection(path=tmp_path / "contacts.db"))
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


async def _index_names(database: SqliteDatabase) -> list[str]:
    return await database.executor.run(
        lambda: list(
            database.connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='contacts'")
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


async def _insert_contact(database: SqliteDatabase, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": 1,
        "account_id": 1,
        "telegram_user_id": 500,
        "username": None,
        "display_name": "Alice",
        "archived_at": None,
        "deleted_at": None,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
    }
    values.update(overrides)
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO contacts (id, account_id, telegram_user_id, username, "
                "display_name, archived_at, deleted_at, created_at, updated_at) "
                "VALUES (:id, :account_id, :telegram_user_id, :username, :display_name, "
                ":archived_at, :deleted_at, :created_at, :updated_at)"
            ),
            values,
        )
    )


async def _contact_count(database: SqliteDatabase) -> int:
    return await database.executor.run(
        lambda: database.connection.execute(text("SELECT COUNT(*) FROM contacts")).scalar_one()
    )


class TestContactsMigration:
    async def test_creates_the_table(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        assert CONTACTS_TABLE in await _tables(database)

    async def test_upgrading_reaches_the_contacts_revision(self, database: SqliteDatabase) -> None:
        # Deliberately not pinned to head. The account and profile suites each
        # pinned it once, and each broke on the next milestone for no reason
        # other than that a later table exists.
        await AlembicMigrationRunner(database).upgrade()
        applied = await AlembicMigrationRunner(database).current_revision()

        assert CONTACTS_TABLE in await _tables(database)
        assert applied is not None
        assert applied >= "0004"

    async def test_creates_both_indexes(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        names = await _index_names(database)

        assert "uq_contacts_account_id_telegram_user_id" in names
        assert "ix_contacts_account_id_created_at" in names

    async def test_round_trips_up_down_up(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)

        await runner.upgrade()
        await runner.downgrade("0003")
        assert CONTACTS_TABLE not in await _tables(database)

        await runner.upgrade()
        assert CONTACTS_TABLE in await _tables(database)

    async def test_downgrade_leaves_earlier_tables_intact(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()

        await runner.downgrade("0003")

        tables = await _tables(database)
        assert "accounts" in tables
        assert "user_profiles" in tables


class TestForeignKeyIntegrity:
    async def test_a_contact_requires_an_existing_account(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        with pytest.raises(Exception, match="FOREIGN KEY constraint"):
            await _insert_contact(database, account_id=999)

    async def test_deleting_an_account_deletes_its_contacts(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)
        await _insert_contact(database, id=1, telegram_user_id=500)
        await _insert_contact(database, id=2, telegram_user_id=501)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM accounts WHERE id = 1"))
        )

        assert await _contact_count(database) == 0

    async def test_deleting_one_account_leaves_the_others_contacts(
        self, database: SqliteDatabase
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        for account_id in (1, 2):
            await _insert_account(database, account_id)
            await _insert_contact(database, id=account_id, account_id=account_id)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM accounts WHERE id = 1"))
        )

        assert await _contact_count(database) == 1


class TestUniqueness:
    async def test_one_telegram_user_per_account(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)
        await _insert_contact(database, id=1, telegram_user_id=500)

        with pytest.raises(Exception, match="UNIQUE constraint"):
            await _insert_contact(database, id=2, telegram_user_id=500)

    async def test_the_same_telegram_user_across_two_accounts_is_allowed(
        self, database: SqliteDatabase
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        for account_id in (1, 2):
            await _insert_account(database, account_id)

        await _insert_contact(database, id=1, account_id=1, telegram_user_id=500)
        await _insert_contact(database, id=2, account_id=2, telegram_user_id=500)

        assert await _contact_count(database) == 2

    async def test_a_deleted_contact_still_occupies_its_telegram_id(
        self, database: SqliteDatabase
    ) -> None:
        # The index covers soft-deleted rows on purpose: the row still holds the
        # person's history, and a second row for the same person would split it.
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)
        await _insert_contact(database, id=1, telegram_user_id=500, deleted_at="2026-02-01")

        with pytest.raises(Exception, match="UNIQUE constraint"):
            await _insert_contact(database, id=2, telegram_user_id=500)

    async def test_the_identifier_is_unique(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)
        await _insert_contact(database, id=1, telegram_user_id=500)

        with pytest.raises(Exception, match=r"UNIQUE constraint|PRIMARY KEY"):
            await _insert_contact(database, id=1, telegram_user_id=501)


class TestCheckConstraints:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"id": 0},
            {"telegram_user_id": 0},
            {"display_name": "   "},
            {"username": "abc"},
            {"username": "x" * 33},
            {"created_at": "2026-06-01", "updated_at": "2026-01-01"},
            {"archived_at": "2026-02-01", "deleted_at": "2026-02-01"},
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
            await _insert_contact(database, **overrides)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"username": None},
            {"username": "alice"},
            {"archived_at": "2026-02-01"},
            {"deleted_at": "2026-02-01"},
        ],
    )
    async def test_valid_rows_are_accepted(
        self, database: SqliteDatabase, overrides: dict[str, object]
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)

        await _insert_contact(database, **overrides)

        assert await _contact_count(database) == 1
