"""The TDLib implementation of :class:`TelegramGateway`.

TDLib's login is **update-driven**: nothing returns "you are logged in". The
client emits ``updateAuthorizationState``, the application answers with the
matching request, and the next state arrives as another update. A caller wanting
to await a login therefore needs something to turn that stream into a sequence
of awaits, and that is what this module is.

The shape
---------

One asyncio task -- the *dispatch loop* -- is the sole consumer of
``TdjsonClient.receive()``, mirroring the single-owner rule the receive thread
already follows (ADR-048). It updates the two state fields and wakes anything
waiting. Everything else here reads those fields; nothing else touches the
stream.

Requests are sent with ``client.request()`` from the caller's own task, not from
the dispatch loop. A submission that awaited its reply inside dispatch would
stall every other update behind it, including the state change it was waiting
for.

What this does not do
---------------------

It never touches the database. Persisting the states it reports is the
application layer's job (``TELEGRAM_ARCHITECTURE.md`` section 4.1), which is
what lets the sync engine be tested with a fake gateway and a real database.

It also does not delete anything on logout. Destroying the local store and the
key is the caller's work: a gateway that removed files would be reaching past
its own boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from tgassist.domain.errors import (
    AuthorizationError,
    TdlibNotRunningError,
    TdlibRequestFailedError,
    TelegramError,
    TelegramNotConfiguredError,
)
from tgassist.domain.model.identifiers import (
    AccountId,
    TelegramChatId,
    TelegramMessageId,
)
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.model.session import AuthorizationState, ConnectionState
from tgassist.domain.model.telegram import (
    HistoryPage,
    TelegramChatInfo,
    TelegramUser,
    require_credential,
)
from tgassist.domain.ports.telegram_gateway import (
    DEFAULT_CHAT_LIMIT,
    DEFAULT_HISTORY_LIMIT,
    AuthorizationHandler,
    RetryDecision,
)
from tgassist.infrastructure.logging import get_logger
from tgassist.infrastructure.telegram import errors as error_mapping
from tgassist.infrastructure.telegram import mapping
from tgassist.infrastructure.telegram.client import TdjsonClient

#: How long to wait for TDLib to report the state a submission should produce.
#: Generous: the wait spans a network round trip and, for a code, Telegram's own
#: delivery. Too short would report a timeout for a login that then succeeds.
DEFAULT_STATE_TIMEOUT: Final = 60.0

#: How long to wait for TDLib's first authorization update after starting.
#: Local work only, so a much shorter budget is honest.
DEFAULT_STARTUP_TIMEOUT: Final = 30.0

#: Guard against an authorization loop that never settles -- a server repeatedly
#: returning to the same state would otherwise spin until the state timeout on
#: every iteration, forever.
MAX_AUTHORIZATION_STEPS: Final = 32

#: TDLib's code for "no such thing", used for both a chat this account cannot
#: see and a chat list with nothing more to load.
_NOT_FOUND: Final = 404

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """Everything TDLib needs before it will accept any other request.

    Attributes:
        api_id: Application identifier from https://my.telegram.org.
        api_hash: The matching hash, wrapped so it stays masked on every
            incidental rendering path.
        session_path: Directory for the encrypted local store.
        database_encryption_key: Key for that store, from the credential store.
        device_model: What this client calls itself in Telegram's
            active-sessions list.
        application_version: Reported alongside the device model.
        system_language_code: Language Telegram should send its own messages in.
    """

    api_id: int
    api_hash: SecretValue
    session_path: Path
    database_encryption_key: SecretValue
    device_model: str = "Desktop"
    application_version: str = "0.1.0"
    system_language_code: str = "en"

    def to_request(self) -> dict[str, Any]:
        """Build the ``setTdlibParameters`` request.

        Secret material is revealed here and nowhere else, at the moment it is
        handed to the native library.

        ``use_secret_chats`` is off: this application never reads them, and
        asking TDLib to maintain end-to-end sessions it will not use would
        create key material with no consumer.
        """
        return {
            "@type": "setTdlibParameters",
            "database_directory": str(self.session_path),
            "files_directory": str(self.session_path / "files"),
            "database_encryption_key": self.database_encryption_key.reveal(),
            "use_file_database": True,
            "use_chat_info_database": True,
            "use_message_database": True,
            "use_secret_chats": False,
            "api_id": self.api_id,
            "api_hash": self.api_hash.reveal(),
            "system_language_code": self.system_language_code,
            "device_model": self.device_model,
            "application_version": self.application_version,
        }


@dataclass(slots=True)
class _AuthorizationView:
    """The dispatch loop's view of where login stands.

    Mutable and shared: the dispatch loop writes, the flow reads. Both run on
    the event loop, so no lock is needed — the invariant is that nothing here is
    touched from another thread.
    """

    state: AuthorizationState = AuthorizationState.UNAUTHORIZED
    raw_type: str = ""
    # The authorization-state object itself, kept so hints can be built when
    # the flow reaches the step that needs them. Contains no credential: TDLib
    # sends what a prompt should say, never an answer.
    payload: dict[str, Any] = field(default_factory=dict)
    connection: ConnectionState = ConnectionState.OFFLINE
    closed: bool = False
    fatal: Exception | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    known: asyncio.Event = field(default_factory=asyncio.Event)

    def announce(self) -> None:
        """Wake everything waiting on a change."""
        self.changed.set()


class TdlibGateway:
    """Talks to Telegram through a :class:`TdjsonClient`.

    Bound to one account at construction, like every component over
    account-owned data (ADR-039).

    Not re-entrant: :meth:`start_authorization` takes a lock, because two
    concurrent logins would answer each other's prompts.
    """

    __slots__ = (
        "_account_id",
        "_auth_lock",
        "_client",
        "_dispatch",
        "_settings",
        "_started",
        "_startup_timeout",
        "_state_timeout",
        "_unhandled",
        "_view",
    )

    def __init__(
        self,
        account_id: AccountId,
        client: TdjsonClient,
        settings: GatewaySettings,
        *,
        state_timeout: float = DEFAULT_STATE_TIMEOUT,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    ) -> None:
        """Bind to an account, a client and the parameters TDLib requires."""
        self._account_id = account_id
        self._client = client
        self._settings = settings
        self._state_timeout = state_timeout
        self._startup_timeout = startup_timeout
        self._view = _AuthorizationView()
        self._dispatch: asyncio.Task[None] | None = None
        self._started = False
        self._auth_lock = asyncio.Lock()
        self._unhandled = 0

    # -- Identity and observation -----------------------------------------

    @property
    def account_id(self) -> AccountId:
        """Return the account this gateway is bound to."""
        return self._account_id

    @property
    def unhandled_updates(self) -> int:
        """Return how many updates this slice had no consumer for.

        Counted rather than discarded silently. Reading them is slice 4's work;
        until then a number that grows is the honest report, and a number that
        does *not* grow during a login would be the surprising one.
        """
        return self._unhandled

    async def authorization_state(self) -> AuthorizationState:
        """Return where this account stands with Telegram's login flow."""
        return self._view.state

    async def connection_state(self) -> ConnectionState:
        """Return whether a connection to Telegram currently exists."""
        return self._view.connection

    async def is_connected(self) -> bool:
        """Report whether a usable connection exists right now."""
        return self._started and self._view.connection in {
            ConnectionState.UPDATING,
            ConnectionState.READY,
        }

    # -- Lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Start the client and hand TDLib its parameters. Idempotent.

        Returns once TDLib has accepted the parameters and reported what it
        wants next — which after a restart is usually ``ready``, because the
        session on disk is still valid. That is the whole point of persisting
        one.

        Raises:
            TelegramNotConfiguredError: If Telegram rejected the application
                credentials, or none were supplied.
            TelegramError: If TDLib refused the parameters for another reason.
            TimeoutError: If TDLib reported nothing at all.
        """
        if self._started:
            return

        await self._client.start()
        self._started = True
        self._dispatch = asyncio.create_task(self._dispatch_loop(), name="tgassist-td-dispatch")

        try:
            await self._await_known()
            if self._view.raw_type == "authorizationStateWaitTdlibParameters":
                await self._send_parameters()
                await self._await_change_from(
                    "authorizationStateWaitTdlibParameters", timeout=self._startup_timeout
                )
            self._raise_if_fatal()
        except BaseException:
            # A half-started gateway owns a native client and a task. Leaving
            # either behind would leak a thread on every failed connection.
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        """Stop the client and release the native handle. Idempotent."""
        dispatch, self._dispatch = self._dispatch, None
        if dispatch is not None:
            dispatch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatch

        if self._started:
            self._started = False
            await self._client.close()

        self._view.connection = ConnectionState.OFFLINE
        self._view.announce()

    # -- Authorization -----------------------------------------------------

    async def start_authorization(self, handler: AuthorizationHandler) -> None:
        """Drive the login flow to completion, asking ``handler`` for credentials.

        Returns immediately when the account is already authorized, so a caller
        need not check first.

        Raises:
            TdlibNotRunningError: If :meth:`connect` has not been called.
            AuthorizationError: If a credential was rejected and the handler
                chose not to retry, or if Telegram asked for a flow this
                application does not implement.
            TelegramError: If Telegram refused for another reason.
            TimeoutError: If a submission produced no state change.
        """
        self._require_started("start_authorization")

        async with self._auth_lock:
            for _ in range(MAX_AUTHORIZATION_STEPS):
                self._raise_if_fatal()
                state, raw = self._view.state, self._view.raw_type

                if state is AuthorizationState.READY:
                    return
                if self._view.closed:
                    msg = "The Telegram client closed during authorization"
                    raise TdlibNotRunningError(
                        msg, user_message="The connection to Telegram was closed."
                    )

                submitted = await self._submit_for(state, handler)
                if not submitted:
                    # A state nobody submits for: parameters being accepted, or
                    # a logout completing. Wait for TDLib to move on.
                    await self._await_change_from(raw, timeout=self._state_timeout)
                    continue
                await self._await_change_from(raw, timeout=self._state_timeout)

            msg = f"Authorization did not settle within {MAX_AUTHORIZATION_STEPS} steps"
            raise AuthorizationError(
                msg,
                user_message="Signing in did not complete. Try again.",
                context={"account_id": int(self._account_id)},
            )

    async def logout(self) -> None:
        """Sign this account out of Telegram and invalidate the session.

        Telegram-side only. The local store and the key outlive this call and
        are the caller's to destroy.
        """
        self._require_started("logout")
        try:
            await self._client.request({"@type": "logOut"})
        except TdlibRequestFailedError as exc:
            raise error_mapping.translate_failure(exc, operation="log_out") from exc

        with contextlib.suppress(TimeoutError):
            await self._await_state(AuthorizationState.LOGGED_OUT, timeout=self._state_timeout)

    # -- Reading -----------------------------------------------------------

    async def get_me(self) -> TelegramUser:
        """Return the authenticated user.

        Raises:
            TdlibNotRunningError: If there is no connection.
            AuthorizationError: If the account is not authorized. Asking who we
                are before signing in is a caller error, and answering it with
                a TDLib code would hide that.
        """
        self._require_authorized("get_me")
        try:
            frame = await self._client.request({"@type": "getMe"})
        except TdlibRequestFailedError as exc:
            raise error_mapping.translate_failure(exc, operation="get_me") from exc
        return mapping.telegram_user_from(frame)

    async def list_chats(self, *, limit: int = DEFAULT_CHAT_LIMIT) -> tuple[TelegramChatInfo, ...]:
        """Return this account's chats, most recently active first.

        Three TDLib calls, not one, and the order matters:

        1. ``loadChats`` asks the server to populate the client's chat list. It
           answers ``404`` when there is nothing more to load, which is an
           ordinary end condition rather than an error -- so it is absorbed.
        2. ``getChats`` returns *identifiers* from that list, already ordered by
           Telegram. Nothing is re-sorted here: the order is Telegram's opinion
           of recency, and recomputing it locally would need data this does not
           fetch.
        3. ``getChat`` returns each chat. TDLib serves these from its local
           database once loaded, so this is cheap despite looking like N+1.

        A chat that disappears between steps 2 and 3 is skipped rather than
        failing the listing: it was left or deleted while we were reading, and
        one vanished chat must not cost the user the other two hundred.
        """
        self._require_authorized("list_chats")
        await self._load_chats(limit)

        try:
            listing = await self._client.request(
                {"@type": "getChats", "chat_list": {"@type": "chatListMain"}, "limit": limit}
            )
        except TdlibRequestFailedError as exc:
            raise error_mapping.translate_failure(exc, operation="list_chats") from exc

        identifiers = listing.get("chat_ids")
        if not isinstance(identifiers, list):
            return ()

        chats: list[TelegramChatInfo] = []
        for raw in identifiers[:limit]:
            if not isinstance(raw, int) or isinstance(raw, bool):
                continue
            found = await self.get_chat(TelegramChatId(raw))
            if found is not None:
                chats.append(found)
        return tuple(chats)

    async def get_chat(self, chat_id: TelegramChatId) -> TelegramChatInfo | None:
        """Return one chat, or ``None`` if this account cannot see it."""
        self._require_authorized("get_chat")
        try:
            frame = await self._client.request({"@type": "getChat", "chat_id": int(chat_id)})
        except TdlibRequestFailedError as exc:
            if _is_absent(exc):
                # An ordinary answer to "does this exist for me", not a failure:
                # the account left the chat, or never had access.
                return None
            raise error_mapping.translate_failure(exc, operation="get_chat") from exc
        return mapping.chat_info_from(frame)

    async def fetch_history(
        self,
        chat_id: TelegramChatId,
        *,
        before_message_id: TelegramMessageId | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> HistoryPage:
        """Return one page of a chat's history, newest first.

        ``from_message_id=0`` starts at the newest, which is what
        ``before_message_id=None`` means. ``offset=0`` asks for messages
        strictly older than the cursor, so consecutive pages do not overlap and
        the cursor message is never returned twice.

        ``only_local`` is false: the point of a backfill is to reach messages
        this device has never seen.

        **The page's boundary is computed here, once.** It is the oldest
        identifier actually returned, or ``None`` when Telegram returned
        nothing. A caller that derived it would have to know that Telegram
        returns short pages for reasons of its own, and every caller that forgot
        would loop forever.
        """
        self._require_authorized("fetch_history")
        try:
            frame = await self._client.request(
                {
                    "@type": "getChatHistory",
                    "chat_id": int(chat_id),
                    "from_message_id": int(before_message_id or 0),
                    "offset": 0,
                    "limit": limit,
                    "only_local": False,
                }
            )
        except TdlibRequestFailedError as exc:
            raise error_mapping.translate_failure(exc, operation="fetch_history") from exc

        raw = frame.get("messages")
        messages = (
            tuple(mapping.message_from(item) for item in raw if isinstance(item, dict))
            if isinstance(raw, list)
            else ()
        )

        oldest = min((message.id for message in messages), default=None)
        return HistoryPage(messages=messages, oldest_message_id=oldest)

    async def _load_chats(self, limit: int) -> None:
        """Ask the server for more of the chat list, tolerating exhaustion.

        TDLib answers ``404`` when the list is already complete. That is the
        normal end of the list, not a failure, and treating it as one would make
        listing chats fail for every account that has few enough of them.
        """
        try:
            await self._client.request(
                {"@type": "loadChats", "chat_list": {"@type": "chatListMain"}, "limit": limit}
            )
        except TdlibRequestFailedError as exc:
            if not _is_absent(exc):
                raise error_mapping.translate_failure(exc, operation="load_chats") from exc

    # -- The dispatch loop -------------------------------------------------

    async def _dispatch_loop(self) -> None:
        """Consume every update and keep the state view current.

        The sole consumer of ``client.receive()``. Never raises: a dispatch loop
        that died would leave every waiter hanging on a state that can no longer
        change, which is worse than any single malformed update.
        """
        while True:
            frame = await self._client.receive()
            if frame is None:
                self._view.closed = True
                self._view.known.set()
                self._view.announce()
                return
            self._handle(frame)

    def _handle(self, frame: dict[str, Any]) -> None:
        """Route one update. Only its ``@type`` is ever logged."""
        kind = mapping.frame_type(frame)
        if kind == "updateAuthorizationState":
            self._on_authorization(frame)
        elif kind == "updateConnectionState":
            self._on_connection(frame)
        else:
            self._unhandled += 1

    def _on_authorization(self, frame: dict[str, Any]) -> None:
        """Record a new authorization state, or the reason there cannot be one."""
        raw = mapping.authorization_state_type(frame)
        state_object = frame.get("authorization_state")
        self._view.raw_type = raw
        self._view.payload = state_object if isinstance(state_object, dict) else {}
        self._view.known.set()

        if raw in mapping.CLOSING_AUTHORIZATION_STATES:
            # Not a logout. Closing is what every ordinary disconnect does, and
            # recording it as a logout would tell the user they had been signed
            # out every time they quit.
            self._view.closed = True
            self._view.announce()
            return

        if raw in mapping.UNSUPPORTED_AUTHORIZATION_STATES:
            msg = f"Telegram requires the unsupported authorization flow {raw}"
            self._view.fatal = AuthorizationError(
                msg,
                user_message=mapping.UNSUPPORTED_AUTHORIZATION_STATES[raw],
                context={"authorization_state": raw},
            )
            self._view.announce()
            return

        translated = mapping.authorization_state_from(raw)
        if translated is None:
            msg = f"TDLib reported an unrecognised authorization state: {raw}"
            self._view.fatal = TelegramError(
                msg,
                user_message="Telegram reported a sign-in state this version does not know.",
                context={"authorization_state": raw},
            )
            self._view.announce()
            return

        self._view.state = translated
        _logger.debug("telegram_authorization_state", state=translated.value)
        self._view.announce()

    def _on_connection(self, frame: dict[str, Any]) -> None:
        """Record a new connection state, ignoring ones we do not know."""
        state = frame.get("state")
        raw = mapping.frame_type(state) if isinstance(state, dict) else ""
        translated = mapping.connection_state_from(raw)
        if translated is None:
            self._unhandled += 1
            return
        self._view.connection = translated
        _logger.debug("telegram_connection_state", state=translated.value)
        self._view.announce()

    # -- Submissions -------------------------------------------------------

    async def _submit_for(self, state: AuthorizationState, handler: AuthorizationHandler) -> bool:
        """Collect and submit the credential this state calls for.

        Returns:
            Whether anything was submitted. ``False`` for states that resolve on
            their own, which the caller waits out rather than answering.
        """
        await handler.on_state_change(state)

        if state is AuthorizationState.WAITING_PHONE:
            await self._submit(
                handler,
                collect=handler.request_phone_number,
                build=lambda value: {
                    "@type": "setAuthenticationPhoneNumber",
                    "phone_number": value,
                },
                name="Phone number",
                operation="set_phone_number",
            )
            return True

        if state is AuthorizationState.WAITING_CODE:
            code_hint = mapping.code_hint_from(self._view.payload)
            await self._submit(
                handler,
                collect=lambda: handler.request_code(code_hint),
                build=lambda value: {"@type": "checkAuthenticationCode", "code": value},
                name="Login code",
                operation="check_code",
            )
            return True

        if state is AuthorizationState.WAITING_PASSWORD:
            password_hint = mapping.password_hint_from(self._view.payload)
            await self._submit(
                handler,
                collect=lambda: handler.request_password(password_hint),
                build=lambda value: {
                    "@type": "checkAuthenticationPassword",
                    "password": value,
                },
                name="Password",
                operation="check_password",
            )
            return True

        return False

    async def _submit(
        self,
        handler: AuthorizationHandler,
        *,
        collect: Callable[[], Awaitable[str]],
        build: Callable[[str], dict[str, Any]],
        name: str,
        operation: str,
    ) -> None:
        """Ask for a credential and send it, retrying while the handler agrees.

        The value is used once and never stored: it lives in a local for the
        duration of one request, and no branch here puts it in a log, an error
        or an attribute.
        """
        while True:
            value = require_credential(await collect(), name=name)
            try:
                await self._client.request(build(value))
            except TdlibRequestFailedError as exc:
                translated = error_mapping.translate_failure(exc, operation=operation)
                if not isinstance(translated, AuthorizationError):
                    raise translated from exc
                if await handler.on_error(translated) is not RetryDecision.RETRY:
                    raise translated from exc
                continue
            return

    async def _send_parameters(self) -> None:
        """Hand TDLib the parameters it will not work without."""
        try:
            await self._client.request(self._settings.to_request())
        except TdlibRequestFailedError as exc:
            translated = error_mapping.translate_failure(exc, operation="set_parameters")
            message = str(exc.context.get("message", "")) if exc.context else ""
            if message in error_mapping.CONFIGURATION_FAILURES:
                raise TelegramNotConfiguredError(
                    str(translated),
                    user_message=(
                        "Telegram rejected this application's credentials. Check "
                        "telegram.api_id and the api_hash in the credential store."
                    ),
                    context=translated.context,
                ) from exc
            raise translated from exc

    # -- Waiting -----------------------------------------------------------

    async def _await_known(self) -> None:
        """Wait for TDLib's first authorization update."""
        await asyncio.wait_for(self._view.known.wait(), self._startup_timeout)

    async def _await_change_from(self, previous: str, *, timeout: float) -> None:
        """Wait until the raw authorization state is no longer ``previous``.

        Compares the *raw* state rather than the domain one, because two
        different TDLib states collapse to ``UNAUTHORIZED`` and a wait that
        could not tell them apart would return before anything happened.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._view.raw_type == previous and not self._view.closed:
            self._raise_if_fatal()
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = f"Telegram reported no change from {previous} within {timeout:g}s"
                raise TimeoutError(msg)
            self._view.changed.clear()
            # Re-check after clearing: a change between the check above and the
            # clear would otherwise be waited for a second time.
            if self._view.raw_type != previous or self._view.closed:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._view.changed.wait(), remaining)
        self._raise_if_fatal()

    async def _await_state(self, target: AuthorizationState, *, timeout: float) -> None:
        """Wait until the domain authorization state reaches ``target``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self._view.state is not target:
            remaining = deadline - loop.time()
            if remaining <= 0:
                msg = f"Telegram did not reach {target.value} within {timeout:g}s"
                raise TimeoutError(msg)
            self._view.changed.clear()
            if self._view.state is target:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._view.changed.wait(), remaining)

    # -- Guards ------------------------------------------------------------

    def _raise_if_fatal(self) -> None:
        """Raise whatever the dispatch loop decided cannot be recovered from."""
        fatal = self._view.fatal
        if fatal is not None:
            self._view.fatal = None
            raise fatal

    def _require_authorized(self, operation: str) -> None:
        """Refuse a read that needs credentials when there are none.

        Every read has the same two preconditions, and reporting "not signed in"
        as a TDLib error code would hide a caller error behind a network one.
        """
        self._require_started(operation)
        if self._view.state is not AuthorizationState.READY:
            msg = f"{operation} requires an authorized account, state is {self._view.state.value}"
            raise AuthorizationError(
                msg,
                user_message="This account is not signed in to Telegram.",
                context={"operation": operation, "account_id": int(self._account_id)},
            )

    def _require_started(self, operation: str) -> None:
        """Refuse an operation that needs a connection when there is not one."""
        if not self._started:
            msg = f"{operation} requires a connected gateway"
            raise TdlibNotRunningError(
                msg,
                user_message="Not connected to Telegram.",
                context={"operation": operation, "account_id": int(self._account_id)},
            )


def _is_absent(error: TdlibRequestFailedError) -> bool:
    """Report whether TDLib said the thing asked for does not exist.

    Matched on the code rather than the message: TDLib returns ``404`` with a
    variety of texts, and the code is the stable part.
    """
    return (error.context or {}).get("code") == _NOT_FOUND


__all__ = [
    "DEFAULT_STARTUP_TIMEOUT",
    "DEFAULT_STATE_TIMEOUT",
    "MAX_AUTHORIZATION_STEPS",
    "GatewaySettings",
    "TdlibGateway",
]
