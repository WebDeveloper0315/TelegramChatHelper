"""Message aggregate, mapper, redaction and migration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    MessageId,
    TelegramMessageId,
)
from tgassist.domain.model.message import (
    MAX_TEXT_LENGTH,
    Message,
    MessageType,
    SenderKind,
)
from tgassist.domain.services.sensitivity import REDACTED_CONTENT, is_content_key
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.logging import build_redaction_processor
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    MessageMapper,
    SqliteDatabase,
)
from tgassist.infrastructure.persistence.mapper import column_names
from tgassist.infrastructure.persistence.schema import MESSAGES_TABLE, messages

SENT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
INGESTED = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
ACCOUNT = AccountId(7)
CHAT = ChatId(11)
MESSAGE = MessageId(13)


def make_message(**overrides: Any) -> Message:
    """Build a valid message, with fields overridden as needed."""
    base = Message.record(
        message_id=MESSAGE,
        account_id=ACCOUNT,
        chat_id=CHAT,
        sender_kind=SenderKind.CONTACT,
        text="Hello there",
        sent_at=SENT,
        ingested_at=INGESTED,
        telegram_message_id=TelegramMessageId(9001),
    )
    return replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# Construction and invariants
# ---------------------------------------------------------------------------


class TestMessageConstruction:
    def test_records_both_timestamps_separately(self) -> None:
        # A backfill ingests a message from years ago today; conflating the two
        # would make every timing analysis wrong.
        message = make_message()

        assert message.sent_at == SENT
        assert message.ingested_at == INGESTED

    def test_an_external_identifier_is_optional(self) -> None:
        message = Message.record(
            message_id=MESSAGE,
            account_id=ACCOUNT,
            chat_id=CHAT,
            sender_kind=SenderKind.OPERATOR,
            text="Typed at a keyboard",
            sent_at=SENT,
            ingested_at=INGESTED,
        )

        assert message.telegram_message_id is None
        assert not message.has_external_identity

    def test_an_external_identifier_is_recognised(self) -> None:
        assert make_message().has_external_identity

    def test_the_default_type_is_text(self) -> None:
        assert make_message().message_type is MessageType.TEXT

    def test_a_message_without_text_is_permitted_for_other_types(self) -> None:
        message = Message.record(
            message_id=MESSAGE,
            account_id=ACCOUNT,
            chat_id=CHAT,
            sender_kind=SenderKind.CONTACT,
            message_type=MessageType.STICKER,
            sent_at=SENT,
            ingested_at=INGESTED,
        )

        assert message.text is None
        assert not message.is_analysable


class TestMessageInvariants:
    @pytest.mark.parametrize("field", ["id", "account_id", "chat_id"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_rejects_non_positive_identifiers(self, field: str, value: int) -> None:
        with pytest.raises(DomainValidationError, match="positive"):
            make_message(**{field: value})

    def test_rejects_a_non_positive_telegram_identifier(self) -> None:
        with pytest.raises(DomainValidationError, match="positive"):
            make_message(telegram_message_id=TelegramMessageId(0))

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_a_text_message_requires_text(self, value: str | None) -> None:
        with pytest.raises(DomainValidationError, match="requires text"):
            make_message(text=value)

    def test_rejects_text_beyond_the_limit(self) -> None:
        with pytest.raises(DomainValidationError, match="at most"):
            make_message(text="x" * (MAX_TEXT_LENGTH + 1))

    def test_accepts_text_at_the_limit(self) -> None:
        assert len(make_message(text="x" * MAX_TEXT_LENGTH).text or "") == MAX_TEXT_LENGTH

    @pytest.mark.parametrize("field", ["sent_at", "ingested_at"])
    def test_rejects_a_naive_timestamp(self, field: str) -> None:
        with pytest.raises(DomainValidationError, match="timezone-aware"):
            make_message(**{field: datetime(2026, 1, 1)})  # noqa: DTZ001

    def test_rejects_a_non_utc_timestamp(self) -> None:
        tokyo = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=9)))

        with pytest.raises(DomainValidationError, match="must be UTC"):
            make_message(sent_at=tokyo)

    def test_a_message_may_be_ingested_before_it_was_sent(self) -> None:
        # Deliberately permitted. Clock skew between a sender and this device is
        # ordinary, and rejecting it would lose a real message over a fraction
        # of a second.
        message = make_message(ingested_at=SENT - timedelta(seconds=2))

        assert message.ingested_at < message.sent_at


class TestMessageImmutability:
    def test_is_frozen(self) -> None:
        message = make_message()

        with pytest.raises((AttributeError, TypeError), match=r"cannot assign|frozen"):
            message.text = "edited"  # type: ignore[misc]

    def test_has_no_updated_at(self) -> None:
        # The absence of the field is the statement: a message is an immutable
        # factual record, and there is nothing for an updated_at to record.
        assert not hasattr(make_message(), "updated_at")

    def test_exposes_no_transitions(self) -> None:
        # Unlike every other aggregate here, there is nothing a message becomes.
        transitions = [
            name
            for name in dir(Message)
            if name.startswith(("with_", "archived", "deleted", "restored", "renamed"))
        ]

        assert transitions == []


class TestDerivedState:
    @pytest.mark.parametrize(
        ("sender", "outgoing"),
        [
            (SenderKind.OPERATOR, True),
            (SenderKind.CONTACT, False),
            (SenderKind.SYSTEM, False),
        ],
    )
    def test_outgoing_is_derived_from_the_sender(self, sender: SenderKind, outgoing: bool) -> None:
        # Derived rather than stored: a stored copy could disagree with the
        # field it is derived from.
        assert make_message(sender_kind=sender).is_outgoing is outgoing

    def test_a_message_with_text_is_analysable(self) -> None:
        assert make_message().is_analysable

    def test_a_message_with_blank_text_is_not(self) -> None:
        assert not make_message(message_type=MessageType.PHOTO, text="   ").is_analysable


# ---------------------------------------------------------------------------
# Content redaction
# ---------------------------------------------------------------------------


class TestContentRedaction:
    def test_the_text_key_is_recognised_as_content(self) -> None:
        # Message.text is the most sensitive field in the application, and a
        # bare "text" key was not previously matched by the sensitivity policy.
        assert is_content_key("text")

    def test_context_is_not_redacted(self) -> None:
        # Why "text" is matched exactly rather than as a fragment: "context" is
        # a structural key on every application error and contains "text".
        assert not is_content_key("context")

    @pytest.mark.parametrize("key", ["subtext", "text_length", "pretext"])
    def test_keys_merely_containing_text_are_not_redacted(self, key: str) -> None:
        assert not is_content_key(key)

    def test_message_text_never_reaches_a_log_record(self) -> None:
        processor = build_redaction_processor(allow_content=False)

        result = processor(None, "info", {"event": "ingested", "text": "private words"})

        assert result["text"] == REDACTED_CONTENT
        assert "private words" not in str(result)

    def test_content_passes_in_diagnostic_mode(self) -> None:
        processor = build_redaction_processor(allow_content=True)

        result = processor(None, "info", {"event": "ingested", "text": "private words"})

        assert result["text"] == "private words"


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


class _FakeRow:
    """A row-like object for testing the mapper without a database."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        return self._values[name]


class TestMessageMapper:
    def test_round_trip_preserves_every_field(self) -> None:
        mapper = MessageMapper()
        original = make_message()

        assert mapper.to_domain(_FakeRow(mapper.to_params(original))) == original

    def test_round_trip_preserves_an_absent_identifier_and_text(self) -> None:
        mapper = MessageMapper()
        original = make_message(message_type=MessageType.PHOTO, text=None, telegram_message_id=None)

        restored = mapper.to_domain(_FakeRow(mapper.to_params(original)))

        assert restored.telegram_message_id is None
        assert restored.text is None

    def test_covers_every_column(self) -> None:
        written = column_names(MessageMapper().to_params(make_message()))
        declared = {column.name for column in messages.columns}

        assert declared == written

    def test_stores_enumerations_as_their_values(self) -> None:
        params = MessageMapper().to_params(make_message())

        assert params["sender_kind"] == "contact"
        assert params["message_type"] == "text"

    def test_is_pure(self) -> None:
        mapper = MessageMapper()
        message = make_message()

        assert mapper.to_params(message) == mapper.to_params(message)

    def test_reads_text_timestamps(self) -> None:
        params = MessageMapper().to_params(make_message())
        params["sent_at"] = SENT.isoformat()
        params["ingested_at"] = INGESTED.isoformat()

        restored = MessageMapper().to_domain(_FakeRow(params))

        assert restored.sent_at == SENT
        assert restored.ingested_at == INGESTED


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SqliteDatabase]:
    """A connected database with no schema applied."""
    db = SqliteDatabase(DatabaseSection(path=tmp_path / "messages.db"))
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


async def _seed(database: SqliteDatabase, *, account_id: int = 1, chat_id: int = 1) -> None:
    """Insert an account, a contact and their private chat."""
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
            {"id": chat_id, "account_id": account_id},
        )
    )
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO chats (id, account_id, telegram_chat_id, chat_type, contact_id, "
                "title, sync_enabled, ai_processing_mode, created_at, updated_at) "
                "VALUES (:id, :account_id, :id, 'private', :id, NULL, 1, 'local_only', "
                "'2026-01-01', '2026-01-01')"
            ),
            {"id": chat_id, "account_id": account_id},
        )
    )


async def _insert_message(database: SqliteDatabase, **overrides: object) -> None:
    values: dict[str, object] = {
        "id": 1,
        "account_id": 1,
        "chat_id": 1,
        "telegram_message_id": 500,
        "sender_kind": "contact",
        "message_type": "text",
        "text": "hello",
        "sent_at": "2026-01-01",
        "ingested_at": "2026-01-02",
    }
    values.update(overrides)
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO messages (id, account_id, chat_id, telegram_message_id, "
                "sender_kind, message_type, text, sent_at, ingested_at) "
                "VALUES (:id, :account_id, :chat_id, :telegram_message_id, :sender_kind, "
                ":message_type, :text, :sent_at, :ingested_at)"
            ),
            values,
        )
    )


async def _message_count(database: SqliteDatabase) -> int:
    return await database.executor.run(
        lambda: database.connection.execute(text("SELECT COUNT(*) FROM messages")).scalar_one()
    )


class TestMessagesMigration:
    async def test_creates_the_table(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        assert MESSAGES_TABLE in await _tables(database)

    async def test_upgrading_reaches_the_messages_revision(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        applied = await AlembicMigrationRunner(database).current_revision()

        assert applied is not None
        assert applied >= "0006"

    async def test_creates_every_index(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        names = await _index_names(database, "messages")

        assert "uq_messages_account_id_chat_id_telegram_message_id" in names
        assert "ix_messages_account_id_chat_id_sent_at" in names

    async def test_adds_the_index_the_composite_key_needs(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        assert "uq_chats_account_id_id" in await _index_names(database, "chats")

    async def test_the_table_has_no_updated_at(self, database: SqliteDatabase) -> None:
        # Append-only, expressed in the schema as well as the interface.
        await AlembicMigrationRunner(database).upgrade()

        columns = await database.executor.run(
            lambda: [
                row[1]
                for row in database.connection.execute(
                    text("PRAGMA table_info(messages)")
                ).fetchall()
            ]
        )

        assert "updated_at" not in columns
        assert "ingested_at" in columns

    async def test_round_trips_up_down_up(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)

        await runner.upgrade()
        await runner.downgrade("0005")
        assert MESSAGES_TABLE not in await _tables(database)
        assert "uq_chats_account_id_id" not in await _index_names(database, "chats")

        await runner.upgrade()
        assert MESSAGES_TABLE in await _tables(database)

    async def test_downgrade_leaves_earlier_tables_intact(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()

        await runner.downgrade("0005")

        tables = await _tables(database)
        assert "chats" in tables
        assert "contacts" in tables


class TestForeignKeyIntegrity:
    async def test_a_message_requires_an_existing_chat(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        with pytest.raises(Exception, match="FOREIGN KEY constraint"):
            await _insert_message(database, chat_id=999)

    async def test_a_message_cannot_be_filed_in_another_accounts_chat(
        self, database: SqliteDatabase
    ) -> None:
        # The composite key (ADR-043). A simple chat_id reference would accept
        # this row and file one account's message under another's conversation.
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database, account_id=1, chat_id=1)
        await _seed(database, account_id=2, chat_id=2)

        with pytest.raises(Exception, match="FOREIGN KEY constraint"):
            await _insert_message(database, account_id=1, chat_id=2)


class TestCascadeDeletion:
    async def test_deleting_a_chat_deletes_its_messages(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)
        await _insert_message(database)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM chats WHERE id = 1"))
        )

        assert await _message_count(database) == 0

    async def test_deleting_a_contact_reaches_messages_through_the_chat(
        self, database: SqliteDatabase
    ) -> None:
        # Two cascades in a chain: a contact purge removes their chat, which
        # removes its messages. This is what PRIVACY.md section 7 promises.
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)
        await _insert_message(database)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        )

        assert await _message_count(database) == 0

    async def test_deleting_an_account_deletes_its_messages(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)
        await _insert_message(database)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM accounts WHERE id = 1"))
        )

        assert await _message_count(database) == 0


class TestUniqueness:
    async def test_one_telegram_message_per_chat(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)
        await _insert_message(database, id=1, telegram_message_id=500)

        with pytest.raises(Exception, match="UNIQUE constraint"):
            await _insert_message(database, id=2, telegram_message_id=500)

    async def test_the_same_identifier_may_appear_in_two_chats(
        self, database: SqliteDatabase
    ) -> None:
        # Telegram numbers messages within a chat, so the same value names a
        # different message elsewhere.
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database, account_id=1, chat_id=1)
        await _seed(database, account_id=2, chat_id=2)

        await _insert_message(database, id=1, account_id=1, chat_id=1, telegram_message_id=500)
        await _insert_message(database, id=2, account_id=2, chat_id=2, telegram_message_id=500)

        assert await _message_count(database) == 2

    async def test_many_messages_without_an_identifier_are_permitted(
        self, database: SqliteDatabase
    ) -> None:
        # The partial index. A non-partial unique index would reject the second
        # message from any source that issues no identifiers (ADR-045).
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        for message_id in (1, 2, 3):
            await _insert_message(database, id=message_id, telegram_message_id=None)

        assert await _message_count(database) == 3


class TestCheckConstraints:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"id": 0},
            {"account_id": 0},
            {"chat_id": 0},
            {"telegram_message_id": 0},
            {"sender_kind": "stranger"},
            {"message_type": "hologram"},
            {"message_type": "text", "text": None},
            {"message_type": "text", "text": "   "},
        ],
    )
    async def test_invalid_rows_are_refused(
        self, database: SqliteDatabase, overrides: dict[str, object]
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        with pytest.raises(Exception, match=r"CHECK constraint|FOREIGN KEY constraint"):
            await _insert_message(database, **overrides)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"telegram_message_id": None},
            {"sender_kind": "operator"},
            {"sender_kind": "system", "message_type": "service", "text": None},
            {"message_type": "photo", "text": None},
            {"message_type": "photo", "text": "a caption"},
            # Ingested before sent: clock skew, deliberately permitted.
            {"sent_at": "2026-02-01", "ingested_at": "2026-01-01"},
        ],
    )
    async def test_valid_rows_are_accepted(
        self, database: SqliteDatabase, overrides: dict[str, object]
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _seed(database)

        await _insert_message(database, **overrides)

        assert await _message_count(database) == 1
