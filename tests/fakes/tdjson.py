"""Fake native libraries, and the openers that produce them.

Loading real native code is the one thing in this project that cannot be tested
directly: a test would need a C compiler present, and the suite would stop being
deterministic on a machine that lacks one. These doubles are the second
implementation of ``NativeLibrary`` that justifies the protocol existing.

Each double models one way a real library goes wrong, and every one of them has
been seen in practice: a file that is not a library at all, an old TDLib whose
symbols differ, a library that loads but answers nothing, a platform that
refuses to open a file it can see.
"""

from __future__ import annotations

import json
import queue
import struct
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

from tests.fakes.telegram_gateway import ACCEPTED_CODE, ACCEPTED_PASSWORD, DEFAULT_USER
from tgassist.domain.model.chat import ChatType
from tgassist.domain.model.identifiers import TelegramChatId, TelegramUserId
from tgassist.domain.model.message import MessageType
from tgassist.domain.model.telegram import TelegramChatInfo, TelegramMessage, TelegramUser
from tgassist.infrastructure.telegram.loader import REQUIRED_SYMBOLS, NativeLibrary


def user_frame(user: TelegramUser) -> dict[str, object]:
    """Render a :class:`TelegramUser` as the TDLib object it came from.

    Round-tripping through the real mapper is the point: a fake that produced a
    shape the mapper never sees would let a mapping bug pass unnoticed.
    """
    return {
        "@type": "user",
        "id": int(user.id),
        "first_name": user.first_name,
        "last_name": user.last_name or "",
        "usernames": {"editable_username": user.username or ""},
    }


TDLIB_VERSION = "1.8.29"
"""What a healthy fake reports. A real version, so the comparison is realistic."""


class FakeTdjson:
    """A library that behaves like a working TDLib.

    Two roles. For the loader it answers ``td_execute``, and records what it was
    asked so a test can assert that logging was silenced before anything else.
    For the client it is a scriptable receive stream: :meth:`push` queues a
    frame that :meth:`receive` will hand out, and :meth:`reply_to` makes a
    request produce an answer.

    :meth:`receive` genuinely blocks, like the real one, so the client's thread
    behaves as it will in production rather than spinning.
    """

    __slots__ = (
        "_frames",
        "_next_client_id",
        "_receive_error",
        "_replies",
        "_symbols",
        "_version",
        "requests",
        "sent",
    )

    def __init__(
        self,
        *,
        version: str | None = TDLIB_VERSION,
        symbols: tuple[str, ...] = REQUIRED_SYMBOLS,
    ) -> None:
        """Build a library exporting ``symbols`` and reporting ``version``."""
        self._version = version
        self._symbols = symbols
        self.requests: list[dict[str, object]] = []
        self.sent: list[dict[str, object]] = []
        self._frames: queue.Queue[str] = queue.Queue()
        self._replies: dict[str, dict[str, object]] = {}
        self._next_client_id = 1
        self._receive_error: BaseException | None = None

    # -- Loader-facing ---------------------------------------------------

    def has_symbol(self, name: str) -> bool:
        """Report whether this library exports an entry point."""
        return name in self._symbols

    def execute(self, request: str) -> str | None:
        """Answer a synchronous request, as ``td_execute`` does."""
        document = json.loads(request)
        self.requests.append(document)

        if document.get("@type") == "setLogVerbosityLevel":
            return json.dumps({"@type": "ok"})
        if document.get("@type") == "getOption" and document.get("name") == "version":
            if self._version is None:
                return None
            return json.dumps({"@type": "optionValueString", "value": self._version})
        return None

    # -- Client-facing ---------------------------------------------------

    def create_client_id(self) -> int:
        """Issue a client identifier, as TDLib does."""
        client_id = self._next_client_id
        self._next_client_id += 1
        return client_id

    def send(self, client_id: int, request: str) -> None:
        """Record a request and, if one is scripted, queue its reply.

        The reply carries the request's own ``@extra``, so a test exercises the
        real correlation path rather than a shortcut around it.
        """
        document = json.loads(request)
        document["@client_id"] = client_id
        self.sent.append(document)

        scripted = self._replies.get(str(document.get("@type")))
        if scripted is not None:
            reply = dict(scripted)
            extra = document.get("@extra")
            if extra is not None:
                reply["@extra"] = extra
            self.push(reply)

    def receive(self, timeout: float) -> str | None:
        """Block for a queued frame, or return ``None`` when none arrives.

        Raises whatever :meth:`fail_receive` was given, which is how a test
        makes the receive thread die.
        """
        if self._receive_error is not None:
            error, self._receive_error = self._receive_error, None
            raise error
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    # -- Scripting -------------------------------------------------------

    def push(self, frame: dict[str, object] | str) -> None:
        """Queue a frame for :meth:`receive` to hand out.

        Accepts a raw string so a test can push something that is not JSON.
        """
        self._frames.put(frame if isinstance(frame, str) else json.dumps(frame))

    def reply_to(self, request_type: str, reply: dict[str, object]) -> None:
        """Answer every request of a type with a reply."""
        self._replies[request_type] = reply

    def fail_receive(self, error: BaseException) -> None:
        """Make the next :meth:`receive` raise, killing the receive thread."""
        self._receive_error = error


def chat_frame(chat: TelegramChatInfo) -> dict[str, object]:
    """Render a :class:`TelegramChatInfo` as the TDLib object it came from.

    Round-tripping through the real mapper is the point: a fake that produced a
    shape the mapper never sees would let a mapping bug pass unnoticed.
    """
    if chat.chat_type is ChatType.PRIVATE:
        kind: dict[str, object] = {
            "@type": "chatTypePrivate",
            "user_id": int(chat.counterpart_id) if chat.counterpart_id else 0,
        }
    elif chat.chat_type is ChatType.GROUP:
        kind = {"@type": "chatTypeBasicGroup", "basic_group_id": 1}
    else:
        kind = {
            "@type": "chatTypeSupergroup",
            "supergroup_id": 1,
            "is_channel": chat.chat_type is ChatType.CHANNEL,
        }

    frame: dict[str, object] = {
        "@type": "chat",
        "id": int(chat.id),
        "type": kind,
        "title": chat.title,
        "unread_count": chat.unread_count,
    }
    if chat.last_message_id is not None:
        frame["last_message"] = {"@type": "message", "id": int(chat.last_message_id)}
    return frame


def message_frame(message: TelegramMessage) -> dict[str, object]:
    """Render a :class:`TelegramMessage` as the TDLib object it came from."""
    content: dict[str, object] = {"@type": _CONTENT_TYPES[message.message_type]}
    if message.text is not None:
        key = "text" if message.message_type is MessageType.TEXT else "caption"
        content[key] = {"@type": "formattedText", "text": message.text}

    frame: dict[str, object] = {
        "@type": "message",
        "id": int(message.id),
        "chat_id": int(message.chat_id),
        "date": int(message.sent_at.timestamp()),
        "is_outgoing": message.is_outgoing,
        "content": content,
    }
    if message.sender_id is not None:
        frame["sender_id"] = {
            "@type": "messageSenderUser",
            "user_id": int(message.sender_id),
        }
    if message.reply_to_message_id is not None:
        frame["reply_to"] = {
            "@type": "messageReplyToMessage",
            "message_id": int(message.reply_to_message_id),
        }
    return frame


#: The TDLib content type each domain message type is rendered as.
_CONTENT_TYPES: Final[dict[MessageType, str]] = {
    MessageType.TEXT: "messageText",
    MessageType.PHOTO: "messagePhoto",
    MessageType.VOICE: "messageVoiceNote",
    MessageType.VIDEO: "messageVideo",
    MessageType.DOCUMENT: "messageDocument",
    MessageType.STICKER: "messageSticker",
    MessageType.LOCATION: "messageLocation",
    MessageType.POLL: "messagePoll",
    MessageType.SERVICE: "messageChatJoinByLink",
    MessageType.OTHER: "messageSomethingNewEntirely",
}


class AuthorizingTdjson(FakeTdjson):
    """A TDLib that runs the real authorization state machine.

    ``FakeTdjson`` answers requests; this one also *reacts* to them, pushing the
    ``updateAuthorizationState`` that TDLib would push next. That is the whole
    protocol the gateway exists to drive, so a gateway test against a fake that
    only answered would prove nothing about the part that is hard.

    The script is a state, not a list of frames: a wrong code leaves the state
    where it was, exactly as Telegram does, so retry behaviour emerges rather
    than being special-cased.
    """

    __slots__ = (
        "_book",
        "_chats",
        "_code",
        "_history",
        "_loaded",
        "_password",
        "_requires_password",
        "_starts_authorized",
        "_state",
        "_users",
    )

    def __init__(
        self,
        *,
        starts_authorized: bool = False,
        requires_password: bool = False,
        code: str = ACCEPTED_CODE,
        password: str = ACCEPTED_PASSWORD,
        user: TelegramUser = DEFAULT_USER,
    ) -> None:
        """Build a library scripted for one login."""
        super().__init__()
        self._starts_authorized = starts_authorized
        self._requires_password = requires_password
        self._code = code
        self._password = password
        self._chats: list[TelegramChatInfo] = []
        self._history: dict[int, list[TelegramMessage]] = {}
        # Everyone getUser can resolve, and separately the address book, because
        # Telegram resolves anyone this account has seen whether or not they
        # were ever saved.
        self._users: dict[int, TelegramUser] = {int(user.id): user}
        self._book: list[int] = []
        self._loaded = False
        # Answered by default, because every flow that reaches `ready` asks it
        # next and a test that had to remember would be testing its own setup.
        self.reply_to("getMe", user_frame(user))
        self._state = "authorizationStateWaitTdlibParameters"
        self.announce(self._state)

    def announce(self, state_type: str, **fields: object) -> None:
        """Push an ``updateAuthorizationState`` for ``state_type``."""
        self._state = state_type
        self.push(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": state_type, **fields},
            }
        )

    def announce_connection(self, state_type: str) -> None:
        """Push an ``updateConnectionState``."""
        self.push({"@type": "updateConnectionState", "state": {"@type": state_type}})

    def announce_message(self, message: TelegramMessage) -> None:
        """Push an ``updateNewMessage``, as Telegram does when one arrives.

        Rendered through the same frame builder the history reply uses, so the
        adapter maps a live message with exactly the code that maps a stored one
        -- which is what makes "live and backfill agree" a fact rather than a
        hope.
        """
        self.push({"@type": "updateNewMessage", "message": message_frame(message)})

    def announce_unknown(self, kind: str = "updateChatReadInbox") -> None:
        """Push an update this application has no consumer for."""
        self.push({"@type": kind, "chat_id": 1})

    def send(self, client_id: int, request: str) -> None:
        """Answer a request, then push whatever TDLib would push next."""
        document = json.loads(request)
        kind = document.get("@type")
        extra = document.get("@extra")

        handled = self._advance(kind, document, extra)
        if handled:
            return
        super().send(client_id, request)

    def _advance(  # noqa: PLR0911 - one branch per TDLib request, and they are the point
        self, kind: object, document: dict[str, Any], extra: object
    ) -> bool:
        """Run one step of the login. Returns whether the request was handled."""
        if kind == "setTdlibParameters":
            self._ok(extra)
            if self._starts_authorized:
                self.announce("authorizationStateReady")
            else:
                self.announce("authorizationStateWaitPhoneNumber")
            return True

        if kind == "setAuthenticationPhoneNumber":
            self._ok(extra)
            self.announce(
                "authorizationStateWaitCode",
                code_info={
                    "@type": "authenticationCodeInfo",
                    "type": {"@type": "authenticationCodeTypeSms", "length": 5},
                    "timeout": 60,
                },
            )
            return True

        if kind == "checkAuthenticationCode":
            if document.get("code") != self._code:
                self._error(extra, 400, "PHONE_CODE_INVALID")
                return True
            self._ok(extra)
            if self._requires_password:
                self.announce(
                    "authorizationStateWaitPassword",
                    password_hint="the usual",  # noqa: S106 - the user's own reminder text
                    has_recovery_email_address=True,
                    recovery_email_address_pattern="a**@e*****.com",
                )
            else:
                self.announce("authorizationStateReady")
            return True

        if kind == "checkAuthenticationPassword":
            if document.get("password") != self._password:
                self._error(extra, 400, "PASSWORD_HASH_INVALID")
                return True
            self._ok(extra)
            self.announce("authorizationStateReady")
            return True

        if kind == "logOut":
            self._ok(extra)
            self.announce("authorizationStateLoggingOut")
            return True

        return self._read(kind, document, extra)

    def _read(  # noqa: PLR0911 - one branch per TDLib request, and they are the point
        self, kind: object, document: dict[str, Any], extra: object
    ) -> bool:
        """Answer a read request. Returns whether it was handled.

        Separate from the login for readability rather than by necessity: these
        branches share no state with the state machine above, and keeping them
        apart makes it obvious that a read never advances a login.
        """
        if kind == "loadChats":
            # TDLib answers 404 once the list is complete. Reproducing that is
            # the point: an adapter that treated it as a failure would break for
            # every account with few enough chats, and only there.
            if self._loaded:
                self._error(extra, 404, "Chat list is empty")
            else:
                self._loaded = True
                self._ok(extra)
            return True

        if kind == "getChats":
            limit = document.get("limit")
            ids = [int(chat.id) for chat in self._chats]
            self._answer(
                extra, {"@type": "chats", "total_count": len(ids), "chat_ids": ids[:limit]}
            )
            return True

        if kind == "getChat":
            wanted = document.get("chat_id")
            found = next((c for c in self._chats if int(c.id) == wanted), None)
            if found is None:
                self._error(extra, 404, "Chat not found")
            else:
                self._answer(extra, chat_frame(found))
            return True

        if kind == "getChatHistory":
            self._answer(extra, self._history_page(document))
            return True

        if kind == "getContacts":
            self._answer(
                extra,
                {"@type": "users", "total_count": len(self._book), "user_ids": list(self._book)},
            )
            return True

        if kind == "getUser":
            person = self._users.get(document.get("user_id", 0))
            if person is None:
                self._error(extra, 404, "User not found")
            else:
                self._answer(extra, user_frame(person))
            return True

        return False

    def script_chats(self, *chats: TelegramChatInfo) -> None:
        """Replace the chats this library will report."""
        self._chats = list(chats)

    def script_history(self, chat_id: TelegramChatId, *messages: TelegramMessage) -> None:
        """Replace one chat's history."""
        self._history[int(chat_id)] = list(messages)

    def script_contacts(self, *users: TelegramUser) -> None:
        """Replace the address book. Everyone in it is also resolvable."""
        self._book = [int(user.id) for user in users]
        self.script_users(*users)

    def script_users(self, *users: TelegramUser) -> None:
        """Make users resolvable by ``getUser`` without saving them."""
        self._users.update({int(user.id): user for user in users})

    def forget_user(self, user_id: TelegramUserId) -> None:
        """Make a user unresolvable, as a deleted Telegram account becomes."""
        self._users.pop(int(user_id), None)

    def _history_page(self, document: dict[str, Any]) -> dict[str, object]:
        """Build a ``messages`` reply, paging as TDLib does."""
        chat_id = document.get("chat_id")
        cursor = document.get("from_message_id") or 0
        limit = document.get("limit") or 0

        stored = sorted(
            self._history.get(int(chat_id) if isinstance(chat_id, int) else 0, []),
            key=lambda m: int(m.id),
            reverse=True,
        )
        if cursor:
            stored = [m for m in stored if int(m.id) < int(cursor)]
        page = stored[: int(limit)] if limit else stored

        return {
            "@type": "messages",
            "total_count": len(page),
            "messages": [message_frame(m) for m in page],
        }

    def _answer(self, extra: object, frame: dict[str, object]) -> None:
        """Answer a request with a payload."""
        reply = dict(frame)
        if extra is not None:
            reply["@extra"] = extra
        self.push(reply)

    def _ok(self, extra: object) -> None:
        """Answer a request with TDLib's ``ok``."""
        frame: dict[str, object] = {"@type": "ok"}
        if extra is not None:
            frame["@extra"] = extra
        self.push(frame)

    def _error(self, extra: object, code: int, message: str) -> None:
        """Answer a request with a TDLib error."""
        frame: dict[str, object] = {"@type": "error", "code": code, "message": message}
        if extra is not None:
            frame["@extra"] = extra
        self.push(frame)


class SilentLibrary:
    """A library that exports everything but answers no request.

    A file with the right symbol names that is not TDLib -- a stub, a wrapper,
    or a build with its query interface compiled out.
    """

    __slots__ = ()

    def has_symbol(self, name: str) -> bool:
        """Report every required entry point as present."""
        return name in REQUIRED_SYMBOLS

    def execute(self, request: str) -> str | None:
        """Answer nothing, whatever is asked."""
        return None

    def create_client_id(self) -> int:
        """Issue a client identifier."""
        return 1

    def send(self, client_id: int, request: str) -> None:
        """Accept a request and do nothing with it."""

    def receive(self, timeout: float) -> str | None:
        """Never produce a frame."""
        time.sleep(min(timeout, 0.01))
        return None


class HostileLibrary:
    """A library whose ``td_execute`` raises.

    Real ``ctypes`` calls into a wrong library fail in ways Python cannot
    predict -- ``OSError``, ``ValueError``, an access violation. The loader must
    treat any of them as "this is not usable" rather than propagating.
    """

    __slots__ = ()

    def has_symbol(self, name: str) -> bool:
        """Report every required entry point as present."""
        return name in REQUIRED_SYMBOLS

    def execute(self, request: str) -> str | None:
        """Fail, as a mismatched binary would."""
        msg = f"exception from a mismatched library for {request[:20]}"
        raise OSError(msg)

    def create_client_id(self) -> int:
        """Issue a client identifier."""
        return 1

    def send(self, client_id: int, request: str) -> None:
        """Accept a request and do nothing with it."""

    def receive(self, timeout: float) -> str | None:
        """Never produce a frame."""
        time.sleep(min(timeout, 0.01))
        return None


class MalformedReplyLibrary:
    """A library returning something that is not JSON."""

    __slots__ = ()

    def has_symbol(self, name: str) -> bool:
        """Report every required entry point as present."""
        return name in REQUIRED_SYMBOLS

    def execute(self, request: str) -> str | None:
        """Return bytes that do not parse."""
        return "not json at all"

    def create_client_id(self) -> int:
        """Issue a client identifier."""
        return 1

    def send(self, client_id: int, request: str) -> None:
        """Accept a request and do nothing with it."""

    def receive(self, timeout: float) -> str | None:
        """Never produce a frame."""
        time.sleep(min(timeout, 0.01))
        return None


def opener_for(library: NativeLibrary) -> Callable[[Path], NativeLibrary]:
    """Return an opener that always yields one library."""

    def _open(path: Path) -> NativeLibrary:  # noqa: ARG001 - the path is irrelevant
        return library

    return _open


def refusing_opener(
    message: str = "cannot open shared object file",
) -> Callable[[Path], NativeLibrary]:
    """Return an opener that fails as a platform does.

    The commonest real failure: the file is present and readable, but a
    transitive dependency is missing or the architecture is wrong.
    """

    def _open(path: Path) -> NativeLibrary:
        raise OSError(f"{path}: {message}")

    return _open


def write_library(path: Path, content: bytes = b"not a real library") -> Path:
    """Write a file standing in for a shared library.

    Its bytes are what the digest is computed over, so the content only has to
    be stable and distinguishable -- the loader never interprets it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


__all__ = [
    "TDLIB_VERSION",
    "FakeTdjson",
    "HostileLibrary",
    "MalformedReplyLibrary",
    "SilentLibrary",
    "make_elf",
    "make_pe",
    "opener_for",
    "refusing_opener",
    "write_library",
]


# ---------------------------------------------------------------------------
# Synthetic binaries
# ---------------------------------------------------------------------------

PE_MACHINE = {"amd64": 0x8664, "x86": 0x014C, "arm64": 0xAA64}
ELF_MACHINE = {"amd64": 0x3E, "x86": 0x03, "arm64": 0xB7}

_PE_OFFSET = 0x80
_OPTIONAL_SIZE = 240
_SECTION_RVA = 0x1000
_SECTION_RAW = 0x200
_DESCRIPTOR = 20


def make_pe(machine: str = "amd64", imports: Sequence[str] = ()) -> bytes:
    """Build a synthetic PE library with a given architecture and import table.

    Enough of the format for the header reader to walk: DOS stub, PE signature,
    COFF and optional headers, one section, and a real import directory. Written
    by hand rather than checked in as a binary fixture so a reader can see
    exactly what is being parsed, and so a new case is a function call rather
    than a new file.
    """
    names_rva = _SECTION_RVA + (len(imports) + 1) * _DESCRIPTOR

    descriptors = bytearray()
    strings = bytearray()
    for name in imports:
        encoded = name.encode("ascii") + b"\x00"
        descriptors += struct.pack("<IIIII", 0, 0, 0, names_rva + len(strings), 0)
        strings += encoded
    descriptors += bytes(_DESCRIPTOR)  # null terminator

    section_data = bytes(descriptors) + bytes(strings)

    coff = struct.pack(
        "<4sHHIIIHH",
        b"PE\x00\x00",
        PE_MACHINE[machine],
        1,  # one section
        0,
        0,
        0,
        _OPTIONAL_SIZE,
        0x2022,  # DLL, executable
    )

    optional = bytearray(_OPTIONAL_SIZE)
    struct.pack_into("<H", optional, 0, 0x20B)  # PE32+
    # Data directory 1 is the import table; PE32+ keeps directories at 112.
    struct.pack_into("<II", optional, 112 + 8, _SECTION_RVA, len(section_data))

    section = struct.pack(
        "<8sIIII12x",
        b".idata\x00\x00",
        len(section_data),
        _SECTION_RVA,
        len(section_data),
        _SECTION_RAW,
    )

    image = bytearray(_SECTION_RAW + len(section_data))
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, _PE_OFFSET)
    image[_PE_OFFSET : _PE_OFFSET + len(coff)] = coff
    optional_at = _PE_OFFSET + len(coff)
    image[optional_at : optional_at + _OPTIONAL_SIZE] = optional
    section_at = optional_at + _OPTIONAL_SIZE
    image[section_at : section_at + len(section)] = section
    image[_SECTION_RAW : _SECTION_RAW + len(section_data)] = section_data
    return bytes(image)


def make_elf(machine: str = "amd64") -> bytes:
    """Build a synthetic ELF header.

    Only the identification and machine fields, because only those are read:
    imports live in the dynamic section, which this project does not parse
    (see ``infrastructure/telegram/binary.py``).
    """
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # 64-bit
    header[5] = 1  # little-endian
    header[6] = 1  # version
    struct.pack_into("<H", header, 16, 3)  # ET_DYN, a shared object
    struct.pack_into("<H", header, 18, ELF_MACHINE[machine])
    return bytes(header)
