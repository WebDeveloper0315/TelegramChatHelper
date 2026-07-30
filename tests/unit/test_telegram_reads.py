"""Chat and message translation, the read DTOs, and the read commands.

The contract suite proves both implementations agree. This covers what only the
translation layer has — TDLib shapes that are wrong, missing or new — and the
two commands that display the result.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fakes import InMemorySecretStore
from tests.fakes.tdjson import chat_frame, message_frame
from tests.fakes.telegram_gateway import FakeTelegramGateway
from tgassist.application.container import Container
from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.chat import ChatType
from tgassist.domain.model.identifiers import (
    AccountId,
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.message import MessageType
from tgassist.domain.model.telegram import HistoryPage, TelegramChatInfo, TelegramMessage
from tgassist.infrastructure.telegram import mapping
from tgassist.presentation.cli.app import app

runner = CliRunner()
TELEGRAM_USER = 1001
SENT_AT = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# TelegramChatInfo
# ---------------------------------------------------------------------------


def chat(**overrides: Any) -> TelegramChatInfo:
    """Build a private chat, with optional field overrides."""
    values: dict[str, Any] = {
        "id": TelegramChatId(100),
        "chat_type": ChatType.PRIVATE,
        "title": "Ada",
        "counterpart_id": TelegramUserId(2002),
    }
    values.update(overrides)
    return TelegramChatInfo(**values)


class TestTelegramChatInfo:
    def test_refuses_a_zero_identifier(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot be zero"):
            chat(id=TelegramChatId(0))

    @pytest.mark.parametrize("kind", [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL])
    def test_a_negative_identifier_is_legitimate(self, kind: ChatType) -> None:
        # Telegram numbers groups and channels below zero, so requiring a
        # positive value here would refuse every group a real account has --
        # which is exactly what this type did until chat synchronisation was
        # written against realistic data.
        assert int(chat(id=TelegramChatId(-100_500), chat_type=kind, counterpart_id=None).id) < 0

    def test_refuses_a_negative_unread_count(self) -> None:
        with pytest.raises(DomainValidationError, match="cannot have -1 unread") as excinfo:
            chat(unread_count=-1)

        assert "impossible unread" in excinfo.value.user_message

    @pytest.mark.parametrize(
        "kind", [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL, ChatType.SAVED]
    )
    def test_only_a_private_chat_may_name_a_counterpart(self, kind: ChatType) -> None:
        # A group with a "counterpart" would invite every private-chat rule to
        # be applied to it.
        with pytest.raises(DomainValidationError, match="single counterpart"):
            chat(chat_type=kind)

    def test_a_group_without_one_is_fine(self) -> None:
        assert not chat(chat_type=ChatType.GROUP, counterpart_id=None).is_private

    def test_a_chat_with_no_messages_is_empty(self) -> None:
        assert chat().is_empty
        assert not chat(last_message_id=TelegramMessageId(5)).is_empty


# ---------------------------------------------------------------------------
# TelegramMessage
# ---------------------------------------------------------------------------


def msg(**overrides: Any) -> TelegramMessage:
    """Build a message, with optional field overrides."""
    values: dict[str, Any] = {
        "id": TelegramMessageId(7),
        "chat_id": TelegramChatId(100),
        "sender_id": TelegramUserId(2002),
        "sent_at": SENT_AT,
        "text": "hello",
    }
    values.update(overrides)
    return TelegramMessage(**values)


class TestTelegramMessage:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("id", TelegramMessageId(0)),
            ("chat_id", TelegramChatId(0)),
            ("sender_id", TelegramUserId(-1)),
            ("reply_to_message_id", TelegramMessageId(0)),
        ],
    )
    def test_unusable_identifiers_are_refused(self, field: str, value: int) -> None:
        with pytest.raises(DomainValidationError):
            msg(**{field: value})

    def test_a_message_in_a_group_has_a_negative_chat_identifier(self) -> None:
        # The same rule as the chat itself: a message in a group carries the
        # group's identifier, and Telegram numbers those below zero.
        assert int(msg(chat_id=TelegramChatId(-100_500)).chat_id) < 0

    def test_a_service_message_may_have_no_sender(self) -> None:
        # Telegram itself produced it; attributing it to somebody would put
        # words in their mouth.
        assert msg(sender_id=None, message_type=MessageType.SERVICE).sender_id is None

    def test_a_naive_timestamp_is_refused(self) -> None:
        naive = datetime(2026, 6, 1, 9, 0)  # noqa: DTZ001 - the point of the test

        with pytest.raises(DomainValidationError, match="timezone-aware"):
            msg(sent_at=naive)

    def test_a_message_may_carry_no_text(self) -> None:
        assert msg(text=None, message_type=MessageType.STICKER).text is None


# ---------------------------------------------------------------------------
# HistoryPage
# ---------------------------------------------------------------------------


class TestHistoryPage:
    def test_an_empty_page_has_reached_the_beginning(self) -> None:
        page = HistoryPage()

        assert page.is_empty
        assert page.reached_beginning

    def test_a_page_with_a_cursor_has_not(self) -> None:
        page = HistoryPage(messages=(msg(),), oldest_message_id=TelegramMessageId(7))

        assert not page.is_empty
        assert not page.reached_beginning

    def test_an_empty_page_cannot_name_a_cursor(self) -> None:
        # It would send a backfill after messages the page just said do not
        # exist.
        with pytest.raises(DomainValidationError, match="empty page"):
            HistoryPage(messages=(), oldest_message_id=TelegramMessageId(7))


# ---------------------------------------------------------------------------
# Chat mapping
# ---------------------------------------------------------------------------


class TestChatTypeMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ({"@type": "chatTypePrivate"}, ChatType.PRIVATE),
            ({"@type": "chatTypeSecret"}, ChatType.PRIVATE),
            ({"@type": "chatTypeBasicGroup"}, ChatType.GROUP),
            ({"@type": "chatTypeSupergroup", "is_channel": False}, ChatType.SUPERGROUP),
            ({"@type": "chatTypeSupergroup", "is_channel": True}, ChatType.CHANNEL),
        ],
    )
    def test_known_types(self, raw: dict[str, Any], expected: ChatType) -> None:
        assert mapping.chat_type_from(raw) is expected

    def test_an_unknown_type_is_read_conservatively(self) -> None:
        # A chat kind this version does not recognise is still a chat, and the
        # conservative reading grants it no private-chat privileges.
        assert mapping.chat_type_from({"@type": "chatTypeSomethingNew"}) is ChatType.GROUP


class TestChatMapping:
    def test_a_private_chat_round_trips(self) -> None:
        original = chat(unread_count=4, last_message_id=TelegramMessageId(88))

        assert mapping.chat_info_from(chat_frame(original)) == original

    def test_a_supergroup_round_trips(self) -> None:
        original = TelegramChatInfo(
            id=TelegramChatId(200), chat_type=ChatType.SUPERGROUP, title="Engineering"
        )

        assert mapping.chat_info_from(chat_frame(original)) == original

    def test_a_channel_round_trips(self) -> None:
        original = TelegramChatInfo(
            id=TelegramChatId(300), chat_type=ChatType.CHANNEL, title="Announcements"
        )

        assert mapping.chat_info_from(chat_frame(original)) == original

    def test_a_private_chat_with_no_user_id_names_no_counterpart(self) -> None:
        found = mapping.chat_info_from(
            {"id": 100, "type": {"@type": "chatTypePrivate", "user_id": 0}, "title": "x"}
        )

        assert found.counterpart_id is None

    def test_a_missing_title_becomes_empty_not_none(self) -> None:
        # Telegram permits it, so the type does; a caller rendering a name needs
        # a string rather than a branch.
        assert mapping.chat_info_from({"id": 100, "type": {}, "title": None}).title == ""

    def test_a_negative_unread_count_is_clamped(self) -> None:
        found = mapping.chat_info_from({"id": 100, "type": {}, "unread_count": -5})

        assert found.unread_count == 0

    def test_a_missing_identifier_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="Telegram chat id"):
            mapping.chat_info_from({"type": {}, "title": "x"})


# ---------------------------------------------------------------------------
# Message mapping
# ---------------------------------------------------------------------------


class TestMessageTypeMapping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("messageText", MessageType.TEXT),
            ("messagePhoto", MessageType.PHOTO),
            ("messageVoiceNote", MessageType.VOICE),
            ("messageVideoNote", MessageType.VIDEO),
            ("messageAnimation", MessageType.VIDEO),
            ("messageAudio", MessageType.DOCUMENT),
            ("messageSticker", MessageType.STICKER),
            ("messageVenue", MessageType.LOCATION),
            ("messagePoll", MessageType.POLL),
        ],
    )
    def test_known_types(self, raw: str, expected: MessageType) -> None:
        assert mapping.message_type_from({"@type": raw}) is expected

    @pytest.mark.parametrize(
        "raw",
        [
            "messageChatJoinByLink",
            "messageChatAddMembers",
            "messageChatChangeTitle",
            "messagePinMessage",
            "messageContactRegistered",
        ],
    )
    def test_service_messages_are_recognised_by_shape(self, raw: str) -> None:
        # TDLib has dozens and adds more, and every one means the same here.
        assert mapping.message_type_from({"@type": raw}) is MessageType.SERVICE

    def test_an_unknown_type_becomes_other_not_an_error(self) -> None:
        # Losing it would leave a hole in a conversation the user can see in
        # Telegram.
        assert mapping.message_type_from({"@type": "messageBrandNew"}) is MessageType.OTHER


class TestMessageTextMapping:
    def test_reads_text(self) -> None:
        content = {"@type": "messageText", "text": {"text": "hello"}}

        assert mapping.message_text_from(content) == "hello"

    def test_reads_a_caption(self) -> None:
        # A conversation held in photo captions would otherwise look empty.
        content = {"@type": "messagePhoto", "caption": {"text": "look at this"}}

        assert mapping.message_text_from(content) == "look at this"

    def test_an_empty_caption_is_none(self) -> None:
        assert mapping.message_text_from({"caption": {"text": ""}}) is None

    def test_no_text_at_all_is_none(self) -> None:
        assert mapping.message_text_from({"@type": "messageSticker"}) is None


class TestMessageMapping:
    def test_a_text_message_round_trips(self) -> None:
        original = msg(is_outgoing=True, reply_to_message_id=TelegramMessageId(3))

        assert mapping.message_from(message_frame(original)) == original

    def test_a_photo_with_a_caption_round_trips(self) -> None:
        original = msg(message_type=MessageType.PHOTO, text="a caption")

        assert mapping.message_from(message_frame(original)) == original

    def test_a_service_message_round_trips(self) -> None:
        original = msg(sender_id=None, text=None, message_type=MessageType.SERVICE)

        assert mapping.message_from(message_frame(original)) == original

    def test_reads_the_flat_reply_field(self) -> None:
        found = mapping.message_from(
            {"id": 7, "chat_id": 100, "date": 1780000000, "reply_to_message_id": 3}
        )

        assert found.reply_to_message_id == TelegramMessageId(3)

    def test_a_non_user_sender_is_read_as_none(self) -> None:
        # A channel post has a chat as its sender, and attributing it to a user
        # would invent one.
        found = mapping.message_from(
            {
                "id": 7,
                "chat_id": 100,
                "date": 1780000000,
                "sender_id": {"@type": "messageSenderChat", "chat_id": 100},
            }
        )

        assert found.sender_id is None


class TestTimestampMapping:
    def test_reads_a_unix_timestamp_as_utc(self) -> None:
        found = mapping.timestamp_from(int(SENT_AT.timestamp()))

        assert found == SENT_AT
        assert found.tzinfo is not None

    @pytest.mark.parametrize("value", [None, 0, -1, "yesterday", True])
    def test_an_unusable_timestamp_is_refused(self, value: Any) -> None:
        # A message with no time cannot be ordered, and ordering is most of what
        # this application does with messages.
        with pytest.raises(DomainValidationError, match="time"):
            mapping.timestamp_from(value)

    def test_an_out_of_range_timestamp_is_refused(self) -> None:
        with pytest.raises(DomainValidationError, match="time"):
            mapping.timestamp_from(10**18)


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------

CHAT_A = TelegramChatInfo(
    id=TelegramChatId(100),
    chat_type=ChatType.PRIVATE,
    title="Ada Lovelace",
    counterpart_id=TelegramUserId(2002),
    unread_count=2,
    last_message_id=TelegramMessageId(3),
)
CHAT_B = TelegramChatInfo(
    id=TelegramChatId(200), chat_type=ChatType.SUPERGROUP, title="Engineering"
)
MESSAGES = (
    TelegramMessage(
        id=TelegramMessageId(1),
        chat_id=CHAT_A.id,
        sender_id=TelegramUserId(2002),
        sent_at=SENT_AT,
        text="first",
    ),
    TelegramMessage(
        id=TelegramMessageId(2),
        chat_id=CHAT_A.id,
        sender_id=TelegramUserId(TELEGRAM_USER),
        sent_at=SENT_AT,
        text="second",
        is_outgoing=True,
    ),
    TelegramMessage(
        id=TelegramMessageId(3),
        chat_id=CHAT_A.id,
        sender_id=TelegramUserId(2002),
        sent_at=SENT_AT,
        text=None,
        message_type=MessageType.STICKER,
    ),
)


@pytest.fixture
def cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,  # noqa: ARG001 - a command configures logging process-wide
) -> Path:
    """Point the CLI at an isolated data directory, with nothing reaching the OS."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(data_dir))
    monkeypatch.setenv("TGASSIST_LOGGING__CONSOLE_ENABLED", "false")
    monkeypatch.setenv("TGASSIST_LOGGING__FILE_ENABLED", "false")

    store = InMemorySecretStore()
    monkeypatch.setattr("tgassist.application.container.build_default_secret_store", lambda: store)
    return data_dir


@pytest.fixture
def _account() -> None:
    """Create an active account for the commands to work against."""
    result = runner.invoke(app, ["account", "create", str(TELEGRAM_USER), "Primary"])
    assert result.exit_code == 0, result.output


@pytest.fixture
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the container's gateway with one scripted to have two chats."""

    @asynccontextmanager
    async def fake_gateway(
        self: Container, account_id: AccountId
    ) -> AsyncIterator[FakeTelegramGateway]:
        del self
        gateway = FakeTelegramGateway(account_id, starts_authorized=True)
        gateway.script_chats(CHAT_A, CHAT_B)
        gateway.script_history(CHAT_A.id, *MESSAGES)
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    monkeypatch.setattr(Container, "telegram_for", fake_gateway)


@pytest.mark.usefixtures("cli_env", "_account", "_gateway")
class TestChatsCommand:
    def test_it_lists_every_chat(self) -> None:
        result = runner.invoke(app, ["telegram", "chats"])

        assert result.exit_code == 0, result.output
        assert "Ada Lovelace" in result.stdout
        assert "Engineering" in result.stdout

    def test_it_shows_the_identifier_and_kind(self) -> None:
        result = runner.invoke(app, ["telegram", "chats"])

        assert "100" in result.stdout
        assert "private" in result.stdout
        assert "supergroup" in result.stdout

    def test_it_shows_unread_counts(self) -> None:
        result = runner.invoke(app, ["telegram", "chats"])

        assert "2 unread" in result.stdout

    def test_it_says_nothing_was_stored(self) -> None:
        # A read, not an import. Saying so is what stops the two being confused.
        result = runner.invoke(app, ["telegram", "chats"])

        assert "Nothing was stored" in result.stdout

    def test_the_limit_is_honoured(self) -> None:
        result = runner.invoke(app, ["telegram", "chats", "--limit", "1"])

        assert "Ada Lovelace" in result.stdout
        assert "Engineering" not in result.stdout


@pytest.mark.usefixtures("cli_env", "_account", "_gateway")
class TestHistoryCommand:
    def test_it_shows_the_messages(self) -> None:
        result = runner.invoke(app, ["telegram", "history", "100"])

        assert result.exit_code == 0, result.output
        assert "first" in result.stdout
        assert "second" in result.stdout

    def test_a_conversation_reads_downwards(self) -> None:
        # Whatever order the transport returned it in.
        result = runner.invoke(app, ["telegram", "history", "100"])

        assert result.stdout.index("first") < result.stdout.index("second")

    def test_it_names_who_sent_each_message(self) -> None:
        result = runner.invoke(app, ["telegram", "history", "100"])

        assert "you" in result.stdout
        assert "them" in result.stdout

    def test_a_message_with_no_text_shows_its_kind(self) -> None:
        result = runner.invoke(app, ["telegram", "history", "100"])

        assert "(sticker)" in result.stdout

    def test_it_never_claims_to_have_reached_the_beginning(self) -> None:
        # A short page is not proof of the beginning -- Telegram returns short
        # pages for reasons of its own -- so a non-empty page says "may".
        result = runner.invoke(app, ["telegram", "history", "100"])

        assert "3 message(s)" in result.stdout
        assert "Older messages may continue before 1." in result.stdout

    def test_it_names_the_cursor_to_continue_from(self) -> None:
        result = runner.invoke(app, ["telegram", "history", "100", "--limit", "1"])

        assert "Older messages may continue before 3." in result.stdout

    def test_it_says_nothing_was_stored(self) -> None:
        result = runner.invoke(app, ["telegram", "history", "100"])

        assert "Nothing was stored" in result.stdout

    def test_an_empty_chat_says_so(self) -> None:
        result = runner.invoke(app, ["telegram", "history", "200"])

        assert result.exit_code == 0, result.output
        assert "No messages" in result.stdout

    def test_an_invisible_chat_is_reported_not_crashed(self) -> None:
        result = runner.invoke(app, ["telegram", "history", "999"])

        assert result.exit_code != 0
        assert "not visible" in result.output


@pytest.mark.usefixtures("cli_env", "_gateway")
class TestReadCommandsWithoutAnAccount:
    def test_chats_says_so(self) -> None:
        result = runner.invoke(app, ["telegram", "chats"])

        assert result.exit_code != 0
        assert "No account is active" in result.output

    def test_history_says_so(self) -> None:
        result = runner.invoke(app, ["telegram", "history", "100"])

        assert result.exit_code != 0
        assert "No account is active" in result.output


@pytest.mark.usefixtures("cli_env", "_account", "_gateway")
class TestTheReadCommandsStoreNothing:
    def test_listing_chats_does_not_create_local_ones(self) -> None:
        # The gateway never writes to the database, and these commands are the
        # first that could have been tempted to.
        runner.invoke(app, ["telegram", "chats"])

        result = runner.invoke(app, ["chat", "list"])

        assert "Ada Lovelace" not in result.stdout

    def test_reading_history_does_not_ingest(self) -> None:
        runner.invoke(app, ["telegram", "history", "100"])

        result = runner.invoke(app, ["chat", "list"])

        assert "100" not in result.stdout
