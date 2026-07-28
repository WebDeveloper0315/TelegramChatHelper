"""Chat aggregate, mapper and migration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.chat import (
    MAX_TITLE_LENGTH,
    AiProcessingMode,
    Chat,
    ChatType,
)
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
)
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    ChatMapper,
    SqliteDatabase,
)
from tgassist.infrastructure.persistence.mapper import column_names
from tgassist.infrastructure.persistence.schema import CHATS_TABLE, chats

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
ACCOUNT = AccountId(7)
CHAT = ChatId(11)
CONTACT = ContactId(13)
TELEGRAM = TelegramChatId(555)


def private_chat(**overrides: Any) -> Chat:
    """Build a valid private chat, with fields overridden as needed."""
    base = Chat.private_with(
        chat_id=CHAT,
        account_id=ACCOUNT,
        telegram_chat_id=TELEGRAM,
        contact_id=CONTACT,
        now=NOW,
    )
    return replace(base, **overrides) if overrides else base


def group_chat(**overrides: Any) -> Chat:
    """Build a valid group chat, with fields overridden as needed."""
    base = Chat.group_titled(
        chat_id=CHAT,
        account_id=ACCOUNT,
        # Negative, as Telegram numbers groups.
        telegram_chat_id=TelegramChatId(-1_001_234),
        chat_type=ChatType.GROUP,
        title="Team",
        now=NOW,
    )
    return replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestPrivateChatConstruction:
    def test_names_its_contact_and_has_no_title(self) -> None:
        chat = private_chat()

        assert chat.is_private
        assert chat.contact_id == CONTACT
        assert chat.title is None

    def test_defaults_keep_content_local(self) -> None:
        # ADR-024: content stays on the device unless the user decides
        # otherwise, per chat.
        chat = private_chat()

        assert chat.ai_processing_mode is AiProcessingMode.LOCAL_ONLY
        assert chat.allows_ai
        assert not chat.allows_cloud_ai

    def test_synchronisation_is_on_by_default(self) -> None:
        # Storing history locally is the application's purpose, and nothing
        # leaves the device by doing it.
        assert private_chat().sync_enabled

    def test_creation_timestamps_match(self) -> None:
        chat = private_chat()

        assert chat.created_at == chat.updated_at == NOW


class TestGroupChatConstruction:
    def test_has_a_title_and_no_contact(self) -> None:
        chat = group_chat()

        assert not chat.is_private
        assert chat.title == "Team"
        assert chat.contact_id is None

    def test_the_title_is_trimmed(self) -> None:
        chat = Chat.group_titled(
            chat_id=CHAT,
            account_id=ACCOUNT,
            telegram_chat_id=TelegramChatId(-1),
            chat_type=ChatType.GROUP,
            title="  Team  ",
            now=NOW,
        )

        assert chat.title == "Team"

    @pytest.mark.parametrize(
        "chat_type", [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL, ChatType.SAVED]
    )
    def test_every_non_private_kind_is_buildable(self, chat_type: ChatType) -> None:
        chat = Chat.group_titled(
            chat_id=CHAT,
            account_id=ACCOUNT,
            telegram_chat_id=TelegramChatId(-1),
            chat_type=chat_type,
            title="Something",
            now=NOW,
        )

        assert chat.chat_type is chat_type

    def test_refuses_to_build_a_private_chat(self) -> None:
        # The private constructor requires a contact; routing a private chat
        # through here would produce one with nobody in it.
        with pytest.raises(DomainValidationError, match="private_with"):
            Chat.group_titled(
                chat_id=CHAT,
                account_id=ACCOUNT,
                telegram_chat_id=TelegramChatId(1),
                chat_type=ChatType.PRIVATE,
                title="Nope",
                now=NOW,
            )


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class TestChatInvariants:
    @pytest.mark.parametrize("field", ["id", "account_id"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_identifiers(self, field: str, value: int) -> None:
        with pytest.raises(DomainValidationError, match="positive"):
            private_chat(**{field: value})

    def test_rejects_a_zero_telegram_identifier(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot be zero"):
            private_chat(telegram_chat_id=TelegramChatId(0))

    def test_accepts_a_negative_telegram_identifier(self) -> None:
        # Telegram numbers groups and channels below zero. A "must be positive"
        # rule -- correct for a user identifier -- would reject every group.
        assert group_chat().telegram_chat_id < 0

    def test_a_private_chat_without_a_contact_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="requires the contact"):
            private_chat(contact_id=None)

    def test_a_non_private_chat_with_a_contact_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot name a single contact"):
            group_chat(contact_id=CONTACT)

    def test_a_private_chat_with_a_title_is_refused(self) -> None:
        # A private chat's name is the contact's name, stored once, on them.
        with pytest.raises(DomainValidationError, match="no title of its own"):
            private_chat(title="Alice")

    @pytest.mark.parametrize("title", [None, "", "   "])
    def test_a_non_private_chat_without_a_title_is_refused(self, title: str | None) -> None:
        with pytest.raises(DomainValidationError, match="requires a title"):
            group_chat(title=title)

    def test_rejects_an_over_long_title(self) -> None:
        with pytest.raises(DomainValidationError, match="at most"):
            group_chat(title="x" * (MAX_TITLE_LENGTH + 1))

    def test_rejects_a_non_positive_contact_identifier(self) -> None:
        with pytest.raises(DomainValidationError, match="positive"):
            private_chat(contact_id=ContactId(0))

    def test_rejects_a_naive_timestamp(self) -> None:
        with pytest.raises(DomainValidationError, match="timezone-aware"):
            private_chat(created_at=datetime(2026, 1, 1))  # noqa: DTZ001

    def test_rejects_a_non_utc_timestamp(self) -> None:
        tokyo = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))

        with pytest.raises(DomainValidationError, match="must be UTC"):
            private_chat(created_at=tokyo, updated_at=tokyo)

    def test_rejects_an_update_before_creation(self) -> None:
        with pytest.raises(DomainValidationError, match="updated before"):
            private_chat(updated_at=NOW - timedelta(days=1))


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class TestChatPolicy:
    def test_is_immutable(self) -> None:
        chat = private_chat()

        with pytest.raises((AttributeError, TypeError), match=r"cannot assign|frozen"):
            chat.sync_enabled = False  # type: ignore[misc]

    def test_disabling_sync_returns_a_new_instance(self) -> None:
        chat = private_chat()

        changed = chat.with_sync_enabled(enabled=False, now=LATER)

        assert changed is not chat
        assert not changed.sync_enabled
        assert chat.sync_enabled
        assert changed.updated_at == LATER

    def test_setting_sync_to_its_current_value_is_a_no_op(self) -> None:
        chat = private_chat()

        assert chat.with_sync_enabled(enabled=True, now=LATER) is chat

    @pytest.mark.parametrize(
        ("mode", "allows_ai", "allows_cloud"),
        [
            (AiProcessingMode.DISABLED, False, False),
            (AiProcessingMode.LOCAL_ONLY, True, False),
            (AiProcessingMode.CLOUD_ALLOWED, True, True),
        ],
    )
    def test_each_mode_answers_both_privacy_questions(
        self, mode: AiProcessingMode, allows_ai: bool, allows_cloud: bool
    ) -> None:
        # "Stop using AI" and "do not send our messages to a cloud service" are
        # different requests (PRIVACY.md section 7), so they are different
        # questions rather than one flag a caller must interpret.
        chat = private_chat().with_ai_processing_mode(mode, LATER)

        assert chat.allows_ai is allows_ai
        assert chat.allows_cloud_ai is allows_cloud

    def test_setting_the_same_mode_is_a_no_op(self) -> None:
        chat = private_chat()

        assert chat.with_ai_processing_mode(AiProcessingMode.LOCAL_ONLY, LATER) is chat

    def test_retitling_a_group_chat(self) -> None:
        changed = group_chat().retitled("New name", LATER)

        assert changed.title == "New name"
        assert changed.updated_at == LATER

    def test_retitling_to_the_same_name_is_a_no_op(self) -> None:
        chat = group_chat()

        assert chat.retitled("Team", LATER) is chat

    def test_retitling_a_private_chat_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="rename the contact") as excinfo:
            private_chat().retitled("Alice", LATER)

        assert "Rename them instead" in excinfo.value.user_message

    def test_identity_survives_policy_changes(self) -> None:
        chat = private_chat().with_sync_enabled(enabled=False, now=LATER)

        assert chat.id == CHAT
        assert chat.account_id == ACCOUNT
        assert chat.contact_id == CONTACT
        assert chat.created_at == NOW


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


class _FakeRow:
    """A row-like object for testing the mapper without a database."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        return self._values[name]


class TestChatMapper:
    @pytest.mark.parametrize("build", [private_chat, group_chat])
    def test_round_trip_preserves_every_field(self, build: Any) -> None:
        mapper = ChatMapper()
        original = build()

        assert mapper.to_domain(_FakeRow(mapper.to_params(original))) == original

    def test_covers_every_column(self) -> None:
        written = column_names(ChatMapper().to_params(private_chat()))
        declared = {column.name for column in chats.columns}

        assert declared == written

    def test_stores_enumerations_as_their_values(self) -> None:
        params = ChatMapper().to_params(private_chat())

        assert params["chat_type"] == "private"
        assert params["ai_processing_mode"] == "local_only"

    def test_is_pure(self) -> None:
        mapper = ChatMapper()
        chat = private_chat()

        assert mapper.to_params(chat) == mapper.to_params(chat)

    def test_reads_text_timestamps(self) -> None:
        params = ChatMapper().to_params(private_chat())
        params["created_at"] = NOW.isoformat()
        params["updated_at"] = NOW.isoformat()

        assert ChatMapper().to_domain(_FakeRow(params)).created_at == NOW


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SqliteDatabase]:
    """A connected database with no schema applied."""
    db = SqliteDatabase(DatabaseSection(path=tmp_path / "chats.db"))
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


async def _index_names(database: SqliteDatabase, table: str) -> list[str]:
    return await database.executor.run(
        lambda: list(
            database.connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = :t"),
                {"t": table},
            ).scalars()
        )
    )


async def _seed(database: SqliteDatabase, *, account_id: int = 1, contact_id: int = 1) -> None:
    """Insert an account and one of its contacts."""
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
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO contacts (id, account_id, telegram_user_id, display_name, "
                "created_at, updated_at) "
                "VALUES (:id, :account_id, :id, 'c', '2026-01-01', '2026-01-01')"
            ),
            {"id": contact_id, "account_id": account_id},
        )
    )


async def _insert_chat(database: SqliteDatabase, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": 1,
        "account_id": 1,
        "telegram_chat_id": 500,
        "chat_type": "private",
        "contact_id": 1,
        "title": None,
        "sync_enabled": 1,
        "ai_processing_mode": "local_only",
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
    }
    values.update(overrides)
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO chats (id, account_id, telegram_chat_id, chat_type, contact_id, "
                "title, sync_enabled, ai_processing_mode, created_at, updated_at) "
                "VALUES (:id, :account_id, :telegram_chat_id, :chat_type, :contact_id, "
                ":title, :sync_enabled, :ai_processing_mode, :created_at, :updated_at)"
            ),
            values,
        )
    )


async def _chat_count(database: SqliteDatabase) -> int:
    return await database.executor.run(
        lambda: database.connection.execute(text("SELECT COUNT(*) FROM chats")).scalar_one()
    )


class TestChatsMigration:
    async def test_creates_the_table(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        assert CHATS_TABLE in await _tables(database)

    async def test_upgrading_reaches_the_chats_revision(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        applied = await AlembicMigrationRunner(database).current_revision()

        assert applied is not None
        assert applied >= "0005"

    async def test_creates_every_index(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        names = await _index_names(database, "chats")

        assert "uq_chats_account_id_telegram_chat_id" in names
        assert "uq_chats_account_id_contact_id" in names
        assert "ix_chats_account_id_created_at" in names

    async def test_adds_the_index_the_composite_key_needs(self, database: SqliteDatabase) -> None:
        # A composite foreign key can only reference columns unique together, so
        # this index on contacts is what makes the ownership guarantee
        # expressible at all (ADR-043).
        await AlembicMigrationRunner(database).upgrade()

        assert "uq_contacts_account_id_id" in await _index_names(database, "contacts")

    async def test_round_trips_up_down_up(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)

        await runner.upgrade()
        await runner.downgrade("0004")
        assert CHATS_TABLE not in await _tables(database)
        assert "uq_contacts_account_id_id" not in await _index_names(database, "contacts")

        await runner.upgrade()
        assert CHATS_TABLE in await _tables(database)

    async def test_downgrade_leaves_earlier_tables_intact(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()

        await runner.downgrade("0004")

        tables = await _tables(database)
        assert "accounts" in tables
        assert "contacts" in tables


class TestForeignKeyIntegrity:
    async def test_a_chat_requires_an_existing_account(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        with pytest.raises(Exception, match="FOREIGN KEY constraint"):
            await _insert_chat(
                database, account_id=999, contact_id=None, chat_type="group", title="Nowhere"
            )

    async def test_a_private_chat_requires_an_existing_contact(
        self, database: SqliteDatabase
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        with pytest.raises(Exception, match="FOREIGN KEY constraint"):
            await _insert_chat(database, contact_id=999)

    async def test_a_chat_cannot_name_another_accounts_contact(
        self, database: SqliteDatabase
    ) -> None:
        # The guarantee the composite key exists for. A plain
        # contact_id -> contacts.id would accept this row (ADR-043).
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database, account_id=1, contact_id=1)
        await _seed(database, account_id=2, contact_id=2)

        with pytest.raises(Exception, match="FOREIGN KEY constraint"):
            await _insert_chat(database, account_id=1, contact_id=2)


class TestCascadeDeletion:
    async def test_deleting_an_account_deletes_its_chats(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)
        await _insert_chat(database)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM accounts WHERE id = 1"))
        )

        assert await _chat_count(database) == 0

    async def test_deleting_a_contact_deletes_their_private_chat(
        self, database: SqliteDatabase
    ) -> None:
        # PRIVACY.md section 7 requires a contact purge to remove everything
        # referencing them. ON DELETE SET NULL -- what DATABASE.md version 1.0
        # specified -- would instead leave a private chat with nobody in it,
        # violating the invariant one line below it (ADR-043).
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)
        await _insert_chat(database)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        )

        assert await _chat_count(database) == 0

    async def test_deleting_a_contact_leaves_group_chats_alone(
        self, database: SqliteDatabase
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)
        await _insert_chat(database, id=1, telegram_chat_id=500)
        await _insert_chat(
            database, id=2, telegram_chat_id=-600, chat_type="group", contact_id=None, title="Team"
        )

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        )

        assert await _chat_count(database) == 1


class TestUniqueness:
    async def test_one_telegram_chat_per_account(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database, account_id=1, contact_id=1)
        await _seed(database, account_id=2, contact_id=2)
        await _insert_chat(database, id=1, account_id=1, contact_id=1, telegram_chat_id=500)

        with pytest.raises(Exception, match="UNIQUE constraint"):
            await _insert_chat(
                database,
                id=2,
                account_id=1,
                contact_id=None,
                chat_type="group",
                title="Clash",
                telegram_chat_id=500,
            )

    async def test_two_accounts_may_share_a_telegram_chat(self, database: SqliteDatabase) -> None:
        # Two accounts can be in the same group.
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database, account_id=1, contact_id=1)
        await _seed(database, account_id=2, contact_id=2)

        for chat_id, account_id in ((1, 1), (2, 2)):
            await _insert_chat(
                database,
                id=chat_id,
                account_id=account_id,
                contact_id=None,
                chat_type="group",
                title="Shared",
                telegram_chat_id=-700,
            )

        assert await _chat_count(database) == 2

    async def test_one_private_chat_per_contact(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)
        await _insert_chat(database, id=1, telegram_chat_id=500)

        with pytest.raises(Exception, match="UNIQUE constraint"):
            await _insert_chat(database, id=2, telegram_chat_id=501)

    async def test_many_chats_without_a_contact_are_permitted(
        self, database: SqliteDatabase
    ) -> None:
        # The partial index is what allows this: a unique index over a nullable
        # column would be fine in SQLite but the intent must be explicit.
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        for chat_id in (1, 2, 3):
            await _insert_chat(
                database,
                id=chat_id,
                telegram_chat_id=-chat_id,
                chat_type="group",
                contact_id=None,
                title=f"Group {chat_id}",
            )

        assert await _chat_count(database) == 3


class TestCheckConstraints:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"id": 0},
            {"telegram_chat_id": 0},
            {"chat_type": "broadcast"},
            {"ai_processing_mode": "everything"},
            # A private chat with nobody in it.
            {"chat_type": "private", "contact_id": None},
            # A group chat claiming a single counterpart.
            {"chat_type": "group", "contact_id": 1, "title": "Team"},
            # A private chat with a title of its own.
            {"chat_type": "private", "contact_id": 1, "title": "Alice"},
            # A group chat with no title.
            {"chat_type": "group", "contact_id": None, "title": None},
            {"chat_type": "group", "contact_id": None, "title": "   "},
            {"created_at": "2026-06-01", "updated_at": "2026-01-01"},
        ],
    )
    async def test_invalid_rows_are_refused(
        self, database: SqliteDatabase, overrides: dict[str, object]
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        with pytest.raises(Exception, match="CHECK constraint"):
            await _insert_chat(database, **overrides)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"ai_processing_mode": "disabled"},
            {"ai_processing_mode": "cloud_allowed"},
            {"sync_enabled": 0},
            {"telegram_chat_id": -1_001_234},
            {"chat_type": "supergroup", "contact_id": None, "title": "Big", "telegram_chat_id": -9},
            {"chat_type": "channel", "contact_id": None, "title": "News", "telegram_chat_id": -8},
            {"chat_type": "saved", "contact_id": None, "title": "Saved", "telegram_chat_id": -7},
        ],
    )
    async def test_valid_rows_are_accepted(
        self, database: SqliteDatabase, overrides: dict[str, object]
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        await _insert_chat(database, **overrides)

        assert await _chat_count(database) == 1
