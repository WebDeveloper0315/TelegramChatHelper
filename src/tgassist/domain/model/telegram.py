"""Data transfer objects for the Telegram boundary.

What the gateway returns instead of TDLib JSON. They are domain types rather
than infrastructure ones because the application layer consumes them, and the
whole point of the gateway port is that nothing above it knows TDLib exists.

Deliberately not entities. A :class:`TelegramUser` is what Telegram said at a
moment in time; a ``Contact`` is what this application has decided to remember
about a person. Conflating them would put an external system in charge of local
identity, which ADR-041 already rejected.

This module grows one slice at a time. Chat, message and update types arrive
with the code that reads them, so nothing here is a guess about a shape that has
no consumer. :class:`TelegramUpdate` is the one deliberate exception: a base
with no fields and one subclass, because a stream needs a type to be typed as
even when only one thing travels on it yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.chat import ChatType
from tgassist.domain.model.identifiers import (
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
    require_nonzero_chat_identifier,
    require_positive_identifier,
)
from tgassist.domain.model.message import MessageType


@dataclass(frozen=True, slots=True)
class TelegramUser:
    """A Telegram account as Telegram describes it.

    Attributes:
        id: The Telegram user identifier.
        first_name: Given name as set by that user. May be empty for accounts
            that have never set one -- Telegram permits it, so this type does.
        last_name: Family name, or ``None`` when unset.
        username: Public handle without the leading ``@``, or ``None``. Most
            accounts have never set one.

    Deliberately absent: the phone number. Nothing above this boundary needs it,
    and a field that exists is a field that eventually reaches a log
    (``SECURITY.md`` section 9).
    """

    id: TelegramUserId
    first_name: str
    last_name: str | None = None
    username: str | None = None

    def __post_init__(self) -> None:
        """Validate the identifier.

        Raises:
            DomainValidationError: If the identifier is not positive.
        """
        require_positive_identifier(self.id, name="Telegram user id")

    @property
    def display_name(self) -> str:
        """Return the best human-readable name available.

        Falls back through given name, handle, then the identifier, because a
        user interface must show *something* and an account with no name at all
        is a real state rather than an error.
        """
        parts = [part for part in (self.first_name, self.last_name) if part]
        if parts:
            return " ".join(parts)
        if self.username:
            return f"@{self.username}"
        return str(int(self.id))


@dataclass(frozen=True, slots=True)
class CodeHint:
    """What is known about the login code Telegram has sent.

    Carries no code -- only how it was delivered and how long it will be, which
    is what a prompt needs in order to be accurate about where to look.

    Attributes:
        delivery: How the code was sent, as TDLib names it (``sms``, ``app``,
            ``call``, ``email``, ``other``). Passed through rather than mapped
            to an enumeration: this is display text, and an unmapped new
            delivery method should read oddly rather than crash.
        length: Number of digits, or ``None`` when TDLib did not say.
        timeout_seconds: How long the code remains valid, or ``None``.
    """

    delivery: str
    length: int | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class PasswordHint:
    """What is known about the two-factor password.

    Carries no password. The hint is text the *user* wrote when they set the
    password up, so it is theirs to see and meaningless to anyone else.

    Attributes:
        hint: The user's own reminder, or ``None`` if they set none.
        has_recovery_email: Whether a recovery address is configured, so a
            prompt can say whether recovery is possible.
        recovery_email_pattern: Redacted address Telegram supplies, such as
            ``a**@e*****.com``, or ``None``.
    """

    hint: str | None = None
    has_recovery_email: bool = False
    recovery_email_pattern: str | None = None


@dataclass(frozen=True, slots=True)
class TelegramChatInfo:
    """A chat as Telegram describes it.

    Not a :class:`~tgassist.domain.model.chat.Chat`. This is what Telegram said;
    a Chat is what this application decided to remember, with a local identifier
    and the operator's own settings on it (ADR-041, ADR-044). Conflating them
    would put an external system in charge of local identity.

    Attributes:
        id: The chat identifier Telegram uses.
        chat_type: Which kind of container this is.
        title: What Telegram calls it. For a private chat this is the other
            person's name, which is why listing private chats needs no second
            lookup.
        counterpart_id: For a private chat, the other person's user identifier;
            ``None`` for every other kind. Telegram carries it on the chat
            itself, and it is what will join a chat to a contact.
        unread_count: Messages Telegram considers unread, never negative.
        last_message_id: The newest message Telegram knows of, or ``None`` for
            an empty chat. The starting point for a backfill.
    """

    id: TelegramChatId
    chat_type: ChatType
    title: str
    counterpart_id: TelegramUserId | None = None
    unread_count: int = 0
    last_message_id: TelegramMessageId | None = None

    def __post_init__(self) -> None:
        """Validate what a chat cannot be without.

        Raises:
            DomainValidationError: If the identifier is zero, the unread count
                is negative, or a non-private chat names a counterpart.
        """
        require_nonzero_chat_identifier(self.id)

        if self.unread_count < 0:
            msg = f"A chat cannot have {self.unread_count} unread messages"
            raise DomainValidationError(
                msg, user_message="Telegram reported an impossible unread count."
            )

        # A group with a "counterpart" would invite every private-chat rule to
        # be applied to it, which is the confusion ADR-044 avoids by making the
        # chat the only edge.
        if self.counterpart_id is not None and self.chat_type is not ChatType.PRIVATE:
            msg = f"A {self.chat_type.value} chat cannot have a single counterpart"
            raise DomainValidationError(
                msg, user_message="Telegram described that chat inconsistently."
            )

    @property
    def is_private(self) -> bool:
        """Whether this is a one-to-one conversation."""
        return self.chat_type is ChatType.PRIVATE

    @property
    def is_empty(self) -> bool:
        """Whether Telegram knows of no messages in this chat."""
        return self.last_message_id is None


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    """A message as Telegram describes it.

    The gateway's counterpart to ``IncomingMessage``: this says what Telegram
    reported, while ``IncomingMessage`` says what a source offered for
    ingestion. The sync engine translates between them, because deciding whether
    a sender is the *operator* means knowing who the operator is -- and the
    gateway deliberately does not.

    Attributes:
        id: The message identifier within its chat. Telegram's identifiers are
            unique per chat, not globally.
        chat_id: The chat it belongs to.
        sender_id: Who sent it, or ``None`` for a service message Telegram
            itself produced.
        sent_at: When it was sent, UTC.
        text: The content, or ``None`` for a message that carries none.
        message_type: What kind of message it is.
        is_outgoing: Whether this account sent it. Telegram states this
            directly, so it is read rather than inferred from the sender --
            which matters for messages sent from another device.
        reply_to_message_id: The message this replies to, or ``None``.
    """

    id: TelegramMessageId
    chat_id: TelegramChatId
    sender_id: TelegramUserId | None
    sent_at: datetime
    text: str | None = None
    message_type: MessageType = MessageType.TEXT
    is_outgoing: bool = False
    reply_to_message_id: TelegramMessageId | None = None

    def __post_init__(self) -> None:
        """Validate identity and time.

        Raises:
            DomainValidationError: If an identifier is unusable, or the
                timestamp is not timezone-aware UTC.
        """
        require_positive_identifier(self.id, name="Telegram message id")
        require_nonzero_chat_identifier(self.chat_id)
        if self.sender_id is not None:
            require_positive_identifier(self.sender_id, name="Telegram sender id")
        if self.reply_to_message_id is not None:
            require_positive_identifier(
                self.reply_to_message_id, name="Telegram reply-to message id"
            )

        if self.sent_at.tzinfo is None:
            msg = "sent_at must be timezone-aware; a naive datetime has no defined instant"
            raise DomainValidationError(
                msg, user_message="Telegram reported a message with no usable time."
            )
        if self.sent_at.utcoffset() != UTC.utcoffset(None):
            msg = f"sent_at must be UTC, got offset {self.sent_at.utcoffset()}"
            raise DomainValidationError(
                msg, user_message="Telegram reported a message with an invalid time."
            )


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One page of a chat's history, newest first.

    Attributes:
        messages: What the page contains, ordered newest to oldest -- the only
            direction Telegram's history API supports efficiently, and the only
            one where an interruption leaves a contiguous stored range.
        oldest_message_id: Where the next call should continue from, or ``None``
            when there is nothing older.

    ``oldest_message_id`` is returned explicitly rather than left to the caller
    to derive. A backfill that inferred "are we done" from an empty page is the
    classic infinite loop, because Telegram returns a short or empty page for
    reasons other than reaching the beginning.
    """

    messages: tuple[TelegramMessage, ...] = ()
    oldest_message_id: TelegramMessageId | None = None

    def __post_init__(self) -> None:
        """Validate the page against its own boundary.

        Raises:
            DomainValidationError: If an empty page names a message to continue
                from, which would send a backfill after messages the page just
                said do not exist.
        """
        if self.oldest_message_id is not None and not self.messages:
            msg = "An empty page cannot name a message to continue from"
            raise DomainValidationError(
                msg, user_message="Telegram returned an inconsistent page of history."
            )

    @property
    def is_empty(self) -> bool:
        """Whether this page carries nothing."""
        return not self.messages

    @property
    def reached_beginning(self) -> bool:
        """Whether there is nothing older to fetch.

        The single question a backfill loop asks. Deriving it here rather than
        at each call site keeps "are we done" from being answered two different
        ways.
        """
        return self.oldest_message_id is None


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    """Something Telegram reported without being asked.

    A base with no fields, so ``updates()`` has one type to yield and a consumer
    has one type to match on. Concrete kinds arrive with the code that consumes
    them -- ADR-051's rule, applied to the stream's payload rather than to the
    port's methods.
    """


@dataclass(frozen=True, slots=True)
class NewMessage(TelegramUpdate):
    """A message arrived in a chat this account can see.

    The only update kind consumed today. Edits, deletions and reactions are
    separate updates in TDLib and separate work here: each changes a stored
    message rather than adding one, and ``MessageRepository`` has no update path
    on purpose (ADR-046).

    Attributes:
        message: What arrived, as Telegram described it.
    """

    message: TelegramMessage


def require_credential(value: str, *, name: str) -> str:
    """Return ``value`` stripped, refusing anything a submission cannot use.

    Shared by every credential the handler collects, because the failure is the
    same each time: a user pressing enter at a prompt produces an empty string,
    and sending it to Telegram spends one of a small number of attempts to be
    told what was already knowable here.

    Args:
        value: What the handler returned.
        name: What to call it in the message.

    Returns:
        The value with surrounding whitespace removed. Codes are routinely
        pasted with a trailing space.

    Raises:
        DomainValidationError: If nothing usable remains.
    """
    cleaned = value.strip()
    if not cleaned:
        msg = f"{name} is empty"
        raise DomainValidationError(
            msg, user_message=f"No {name.lower()} was entered.", context={"field": name}
        )
    return cleaned


__all__ = [
    "CodeHint",
    "HistoryPage",
    "NewMessage",
    "PasswordHint",
    "TelegramChatInfo",
    "TelegramMessage",
    "TelegramUpdate",
    "TelegramUser",
    "require_credential",
]
