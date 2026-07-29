"""Translation between TDLib's JSON vocabulary and this application's types.

Kept in one module rather than spread across the adapter so that every
assumption about TDLib's wire format has one place to be wrong in, and one place
to be corrected when TDLib changes.

Nothing here raises on an unrecognised value. Translation reports what it could
not translate and lets the caller decide, because the right response differs:
an unknown connection state is noise, while an unknown authorization state is a
login that cannot proceed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.chat import ChatType
from tgassist.domain.model.identifiers import (
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.message import MessageType
from tgassist.domain.model.session import AuthorizationState, ConnectionState
from tgassist.domain.model.telegram import (
    CodeHint,
    PasswordHint,
    TelegramChatInfo,
    TelegramMessage,
    TelegramUser,
)

#: TDLib's authorization states, mapped onto the domain's.
#:
#: The domain has six states and TDLib has more, because several of TDLib's are
#: setup steps rather than anything a user waits for. Collapsing them is
#: deliberate: ``waitTdlibParameters`` is not a state anyone should be shown.
AUTHORIZATION_STATES: Final[dict[str, AuthorizationState]] = {
    "authorizationStateWaitTdlibParameters": AuthorizationState.UNAUTHORIZED,
    "authorizationStateWaitEncryptionKey": AuthorizationState.UNAUTHORIZED,
    "authorizationStateWaitPhoneNumber": AuthorizationState.WAITING_PHONE,
    "authorizationStateWaitCode": AuthorizationState.WAITING_CODE,
    "authorizationStateWaitPassword": AuthorizationState.WAITING_PASSWORD,
    "authorizationStateReady": AuthorizationState.READY,
    "authorizationStateLoggingOut": AuthorizationState.LOGGED_OUT,
}

#: Authorization states this application knowingly does not implement.
#:
#: Each is a real Telegram flow with a real user in front of it, so each gets a
#: sentence explaining what happened rather than being lumped into "unexpected
#: state". They are listed rather than silently treated as unknown, because the
#: difference between "TDLib changed" and "you need a flow we do not support" is
#: the difference between a bug report and an answer.
UNSUPPORTED_AUTHORIZATION_STATES: Final[dict[str, str]] = {
    "authorizationStateWaitRegistration": (
        "That phone number has no Telegram account. Create one in an official "
        "Telegram client first; this application signs in to existing accounts "
        "and does not register new ones."
    ),
    "authorizationStateWaitOtherDeviceConfirmation": (
        "Telegram asked to confirm this login by scanning a QR code on another "
        "device. This application signs in by phone number and code instead."
    ),
    "authorizationStateWaitEmailAddress": (
        "This account requires an email address to sign in. That flow is not supported yet."
    ),
    "authorizationStateWaitEmailCode": (
        "This account requires a code sent by email. That flow is not supported yet."
    ),
}

#: States meaning the client itself is shutting down.
#:
#: Deliberately **not** mapped to ``LOGGED_OUT``: closing a client is what every
#: ordinary disconnect does, and recording it as a logout would tell the user
#: they had been signed out every time they quit the application.
CLOSING_AUTHORIZATION_STATES: Final[frozenset[str]] = frozenset(
    {"authorizationStateClosing", "authorizationStateClosed"}
)

#: TDLib's connection states, mapped onto the domain's.
CONNECTION_STATES: Final[dict[str, ConnectionState]] = {
    "connectionStateWaitingForNetwork": ConnectionState.WAITING_FOR_NETWORK,
    "connectionStateConnectingToProxy": ConnectionState.CONNECTING,
    "connectionStateConnecting": ConnectionState.CONNECTING,
    "connectionStateUpdating": ConnectionState.UPDATING,
    "connectionStateReady": ConnectionState.READY,
}


def frame_type(frame: dict[str, Any]) -> str:
    """Return a frame's ``@type``, or the empty string if it has none."""
    value = frame.get("@type")
    return value if isinstance(value, str) else ""


def authorization_state_type(frame: dict[str, Any]) -> str:
    """Return the ``@type`` of the authorization state inside an update."""
    state = frame.get("authorization_state")
    return frame_type(state) if isinstance(state, dict) else ""


def authorization_state_from(type_name: str) -> AuthorizationState | None:
    """Return the domain state for a TDLib authorization state name.

    Returns:
        The matching state, or ``None`` when TDLib reported something this
        application does not translate. ``None`` is not an error here — the
        caller distinguishes *closing* from *unsupported* from *unknown*.
    """
    return AUTHORIZATION_STATES.get(type_name)


def connection_state_from(type_name: str) -> ConnectionState | None:
    """Return the domain state for a TDLib connection state name."""
    return CONNECTION_STATES.get(type_name)


def code_hint_from(frame: dict[str, Any]) -> CodeHint:
    """Build a :class:`CodeHint` from ``authorizationStateWaitCode``.

    Every field is optional in practice. TDLib's shape has changed across
    versions and a login must not fail because a hint was missing — the hint
    improves a prompt, it does not gate one.
    """
    info = frame.get("code_info")
    info = info if isinstance(info, dict) else {}
    kind = info.get("type")
    kind = kind if isinstance(kind, dict) else {}

    return CodeHint(
        delivery=_delivery_of(frame_type(kind)),
        length=_positive_int(kind.get("length")),
        timeout_seconds=_positive_int(info.get("timeout")),
    )


def password_hint_from(frame: dict[str, Any]) -> PasswordHint:
    """Build a :class:`PasswordHint` from ``authorizationStateWaitPassword``.

    The hint is text the user wrote themselves when setting the password, so it
    is theirs to see. The password is not here, and there is no field it could
    arrive in.
    """
    hint = frame.get("password_hint")
    pattern = frame.get("recovery_email_address_pattern")
    return PasswordHint(
        hint=hint if isinstance(hint, str) and hint else None,
        has_recovery_email=bool(frame.get("has_recovery_email_address")),
        recovery_email_pattern=pattern if isinstance(pattern, str) and pattern else None,
    )


def telegram_user_from(frame: dict[str, Any]) -> TelegramUser:
    """Build a :class:`TelegramUser` from TDLib's ``user`` object.

    Raises:
        DomainValidationError: If the identifier is missing or not positive. A
            user without one is not a user, and accepting it would push the
            failure to whichever field first needed it.
    """
    username = frame.get("username")
    if not username:
        # Since 1.8.x the handles live in a nested object, and the flat field is
        # absent rather than empty. Both shapes are read so a version bump does
        # not silently drop every username.
        usernames = frame.get("usernames")
        if isinstance(usernames, dict):
            username = usernames.get("editable_username")
            if not username:
                active = usernames.get("active_usernames")
                username = active[0] if isinstance(active, list) and active else None

    last_name = frame.get("last_name")
    return TelegramUser(
        id=TelegramUserId(_as_int(frame.get("id"))),
        first_name=str(frame.get("first_name") or ""),
        last_name=str(last_name) if last_name else None,
        username=str(username) if username else None,
    )


#: TDLib's chat types, mapped onto the domain's.
#:
#: ``chatTypeSupergroup`` covers both supergroups and channels, distinguished by
#: an ``is_channel`` flag rather than by type, which is why it is resolved in
#: code below rather than by a lookup here.
CHAT_TYPES: Final[dict[str, ChatType]] = {
    "chatTypePrivate": ChatType.PRIVATE,
    "chatTypeBasicGroup": ChatType.GROUP,
    "chatTypeSecret": ChatType.PRIVATE,
}

#: TDLib's message content types, mapped onto the domain's.
#:
#: Anything unlisted becomes ``OTHER`` rather than being refused. A message kind
#: this version has never heard of is still a message that was sent, and losing
#: it would leave a hole in a conversation the user can see in Telegram.
MESSAGE_TYPES: Final[dict[str, MessageType]] = {
    "messageText": MessageType.TEXT,
    "messagePhoto": MessageType.PHOTO,
    "messageVoiceNote": MessageType.VOICE,
    "messageVideo": MessageType.VIDEO,
    "messageVideoNote": MessageType.VIDEO,
    "messageAnimation": MessageType.VIDEO,
    "messageDocument": MessageType.DOCUMENT,
    "messageAudio": MessageType.DOCUMENT,
    "messageSticker": MessageType.STICKER,
    "messageLocation": MessageType.LOCATION,
    "messageVenue": MessageType.LOCATION,
    "messagePoll": MessageType.POLL,
}


def chat_type_from(frame: dict[str, Any]) -> ChatType:
    """Return the domain chat type for TDLib's ``type`` object.

    Unknown types become ``GROUP`` rather than raising. A chat kind this version
    does not recognise is still a chat the user has, and the conservative
    reading is the one that grants no private-chat privileges to it.
    """
    kind = frame_type(frame)
    if kind == "chatTypeSupergroup":
        return ChatType.CHANNEL if frame.get("is_channel") else ChatType.SUPERGROUP
    return CHAT_TYPES.get(kind, ChatType.GROUP)


def message_type_from(content: dict[str, Any]) -> MessageType:
    """Return the domain message type for TDLib's ``content`` object.

    Service messages -- "X joined the group" -- are recognised by prefix rather
    than enumerated: TDLib has dozens and adds more, and every one of them means
    the same thing here.
    """
    kind = frame_type(content)
    known = MESSAGE_TYPES.get(kind)
    if known is not None:
        return known
    if kind.startswith("messageChat") or kind in _SERVICE_CONTENT:
        return MessageType.SERVICE
    return MessageType.OTHER


def message_text_from(content: dict[str, Any]) -> str | None:
    """Return a message's text or caption, or ``None`` when it carries none.

    A photo's caption is text the user wrote, so it is read into the same field
    rather than discarded -- otherwise a conversation held in captions would
    look empty.
    """
    for key in ("text", "caption"):
        value = content.get(key)
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str) and text:
                return text
    return None


def chat_info_from(frame: dict[str, Any]) -> TelegramChatInfo:
    """Build a :class:`TelegramChatInfo` from TDLib's ``chat`` object.

    Raises:
        DomainValidationError: If the identifier is missing or not positive.
    """
    chat_type = frame.get("type")
    chat_type = chat_type if isinstance(chat_type, dict) else {}
    resolved = chat_type_from(chat_type)

    counterpart: TelegramUserId | None = None
    if resolved is ChatType.PRIVATE:
        user_id = _as_int(chat_type.get("user_id"))
        counterpart = TelegramUserId(user_id) if user_id > 0 else None

    last_message = frame.get("last_message")
    last_message_id: TelegramMessageId | None = None
    if isinstance(last_message, dict):
        candidate = _as_int(last_message.get("id"))
        last_message_id = TelegramMessageId(candidate) if candidate > 0 else None

    return TelegramChatInfo(
        id=TelegramChatId(_as_int(frame.get("id"))),
        chat_type=resolved,
        title=str(frame.get("title") or ""),
        counterpart_id=counterpart,
        unread_count=max(_as_int(frame.get("unread_count")), 0),
        last_message_id=last_message_id,
    )


def message_from(frame: dict[str, Any]) -> TelegramMessage:
    """Build a :class:`TelegramMessage` from TDLib's ``message`` object.

    Raises:
        DomainValidationError: If an identifier or the timestamp is unusable.
    """
    content = frame.get("content")
    content = content if isinstance(content, dict) else {}

    sender = frame.get("sender_id")
    sender_id: TelegramUserId | None = None
    if isinstance(sender, dict) and frame_type(sender) == "messageSenderUser":
        candidate = _as_int(sender.get("user_id"))
        sender_id = TelegramUserId(candidate) if candidate > 0 else None

    reply_to = _as_int(frame.get("reply_to_message_id"))
    if reply_to <= 0:
        # Since 1.8.x the reply lives in a nested object. Both shapes are read
        # so a version bump does not silently drop every reply relationship.
        nested = frame.get("reply_to")
        if isinstance(nested, dict):
            reply_to = _as_int(nested.get("message_id"))

    return TelegramMessage(
        id=TelegramMessageId(_as_int(frame.get("id"))),
        chat_id=TelegramChatId(_as_int(frame.get("chat_id"))),
        sender_id=sender_id,
        sent_at=timestamp_from(frame.get("date")),
        text=message_text_from(content),
        message_type=message_type_from(content),
        is_outgoing=bool(frame.get("is_outgoing")),
        reply_to_message_id=TelegramMessageId(reply_to) if reply_to > 0 else None,
    )


def timestamp_from(value: Any) -> datetime:
    """Return a TDLib Unix timestamp as an aware UTC datetime.

    Raises:
        DomainValidationError: If the value is not a usable timestamp. A message
            with no time cannot be ordered, and ordering is most of what this
            application does with messages.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"Telegram reported an unusable message timestamp: {value!r}"
        raise DomainValidationError(
            msg, user_message="Telegram reported a message with no usable time."
        )
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        msg = f"Telegram reported a message timestamp out of range: {value!r}"
        raise DomainValidationError(
            msg, user_message="Telegram reported a message with an invalid time."
        ) from exc


#: TDLib service-message contents that do not begin with ``messageChat``.
_SERVICE_CONTENT: Final[frozenset[str]] = frozenset(
    {
        "messagePinMessage",
        "messageScreenshotTaken",
        "messageContactRegistered",
        "messageCustomServiceAction",
        "messageExpiredPhoto",
        "messageExpiredVideo",
        "messageUnsupported",
    }
)


def _delivery_of(type_name: str) -> str:
    """Return a short, readable name for a TDLib code-delivery type."""
    prefix = "authenticationCodeType"
    if type_name.startswith(prefix):
        remainder = type_name[len(prefix) :]
        return _to_words(remainder) if remainder else "other"
    return "other"


def _to_words(value: str) -> str:
    """Split a CamelCase TDLib suffix into lower-case words."""
    words: list[str] = []
    current = ""
    for character in value:
        if character.isupper() and current:
            words.append(current)
            current = character
        else:
            current += character
    if current:
        words.append(current)
    return " ".join(word.lower() for word in words)


def _positive_int(value: Any) -> int | None:
    """Return ``value`` as a positive integer, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _as_int(value: Any) -> int:
    """Return ``value`` as an integer, or ``0`` when it is not one.

    Zero rather than an exception, because the entity that receives it rejects
    non-positive identifiers with a message naming the field. Raising here would
    replace that message with a less specific one.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


__all__ = [
    "AUTHORIZATION_STATES",
    "CHAT_TYPES",
    "CLOSING_AUTHORIZATION_STATES",
    "CONNECTION_STATES",
    "MESSAGE_TYPES",
    "UNSUPPORTED_AUTHORIZATION_STATES",
    "authorization_state_from",
    "authorization_state_type",
    "chat_info_from",
    "chat_type_from",
    "code_hint_from",
    "connection_state_from",
    "frame_type",
    "message_from",
    "message_text_from",
    "message_type_from",
    "password_hint_from",
    "telegram_user_from",
    "timestamp_from",
]
