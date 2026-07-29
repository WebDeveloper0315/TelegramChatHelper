"""A scriptable Telegram gateway.

The second implementation of :class:`TelegramGateway`, and the reason the port
exists at all. It is a *fake*, not a stub: it runs the same state machine TDLib
does, so a test that passes against it is evidence about the real flow rather
than about a mock's return values.

Scripting is by state sequence. A test says what Telegram will ask for and what
it will reject, and the fake walks that script as credentials arrive — which is
how a wrong code, a two-factor prompt or an already-valid session are all
expressed without special cases.
"""

from __future__ import annotations

from tgassist.domain.errors import (
    AuthorizationError,
    TdlibNotRunningError,
    TelegramError,
)
from tgassist.domain.model.identifiers import (
    AccountId,
    TelegramChatId,
    TelegramMessageId,
    TelegramUserId,
)
from tgassist.domain.model.session import AuthorizationState, ConnectionState
from tgassist.domain.model.telegram import (
    CodeHint,
    HistoryPage,
    PasswordHint,
    TelegramChatInfo,
    TelegramMessage,
    TelegramUser,
    require_credential,
)
from tgassist.domain.ports.telegram_gateway import (
    DEFAULT_CHAT_LIMIT,
    DEFAULT_HISTORY_LIMIT,
    AuthorizationHandler,
    RetryDecision,
    TelegramGateway,
)

DEFAULT_USER = TelegramUser(
    id=TelegramUserId(1001), first_name="Test", last_name="User", username="testuser"
)

#: The credentials this fake accepts. Named constants rather than literals in a
#: default, so a scanner reading "password=..." in a signature does not have to
#: guess whether it found a real one.
ACCEPTED_CODE = "12345"
ACCEPTED_PASSWORD = "not-a-real-password"  # noqa: S105 - a fake's scripted answer


class FakeTelegramGateway(TelegramGateway):
    """A Telegram gateway that answers from a script.

    Reads are scripted separately from the login: :meth:`script_chats` and
    :meth:`script_history` say what this account has, and the paging in
    :meth:`fetch_history` behaves as TDLib's does, so a backfill loop written
    against this fake is one that works against Telegram.

    Attributes:
        connect_calls: How many times :meth:`connect` was called, so idempotency
            is observable rather than assumed.
        list_calls: How many times the chat list was requested, so a caller that
            fetches it twice is visible.
        submitted: Which credential kinds were asked for, in order. Deliberately
            **not** what was submitted: a fake that recorded codes and passwords
            would be a test fixture that stores credentials, which is the thing
            the port exists to prevent.
    """

    __slots__ = (
        "_account_id",
        "_authorized",
        "_chats",
        "_code",
        "_connected",
        "_connection",
        "_history",
        "_password",
        "_requires_password",
        "_starts_authorized",
        "_user",
        "connect_calls",
        "disconnect_calls",
        "list_calls",
        "logout_calls",
        "submitted",
    )

    def __init__(  # noqa: PLR0913 - one argument per scripted behaviour
        self,
        account_id: AccountId,
        *,
        user: TelegramUser = DEFAULT_USER,
        starts_authorized: bool = False,
        requires_password: bool = False,
        code: str = ACCEPTED_CODE,
        password: str = ACCEPTED_PASSWORD,
    ) -> None:
        """Build a gateway for one account.

        Args:
            account_id: The account this gateway is bound to.
            user: Who :meth:`get_me` will report.
            starts_authorized: Whether the stored session is still valid, which
                is the ordinary case after a restart.
            requires_password: Whether two-factor authentication is enabled.
            code: The code that will be accepted. Anything else is rejected the
                way Telegram rejects one.
            password: The password that will be accepted.
        """
        self._account_id = account_id
        self._user = user
        self._starts_authorized = starts_authorized
        self._requires_password = requires_password
        self._code = code
        self._password = password

        self._connected = False
        self._authorized = AuthorizationState.UNAUTHORIZED
        self._connection = ConnectionState.OFFLINE
        self._chats: list[TelegramChatInfo] = []
        self._history: dict[int, list[TelegramMessage]] = {}
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.logout_calls = 0
        self.list_calls = 0
        self.submitted: list[str] = []

    # -- Identity ---------------------------------------------------------

    @property
    def account_id(self) -> AccountId:
        """Return the account this gateway is bound to."""
        return self._account_id

    # -- Lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Start the fake client. Idempotent, like the real one."""
        self.connect_calls += 1
        if self._connected:
            return
        self._connected = True
        self._connection = ConnectionState.READY
        self._authorized = (
            AuthorizationState.READY
            if self._starts_authorized
            else AuthorizationState.WAITING_PHONE
        )

    async def disconnect(self) -> None:
        """Stop the fake client. Idempotent."""
        self.disconnect_calls += 1
        self._connected = False
        self._connection = ConnectionState.OFFLINE

    async def is_connected(self) -> bool:
        """Report whether a usable connection exists."""
        return self._connected and self._connection in {
            ConnectionState.UPDATING,
            ConnectionState.READY,
        }

    async def authorization_state(self) -> AuthorizationState:
        """Return where this account stands with the login flow."""
        return self._authorized

    async def connection_state(self) -> ConnectionState:
        """Return whether a connection exists."""
        return self._connection

    # -- Authorization ----------------------------------------------------

    async def start_authorization(self, handler: AuthorizationHandler) -> None:
        """Walk the scripted login, asking ``handler`` for each credential."""
        self._require_connected("start_authorization")

        while self._authorized is not AuthorizationState.READY:
            await handler.on_state_change(self._authorized)

            if self._authorized is AuthorizationState.WAITING_PHONE:
                self.submitted.append("phone")
                require_credential(await handler.request_phone_number(), name="Phone number")
                self._authorized = AuthorizationState.WAITING_CODE
                continue

            if self._authorized is AuthorizationState.WAITING_CODE:
                self.submitted.append("code")
                value = require_credential(
                    await handler.request_code(CodeHint(delivery="sms", length=5)),
                    name="Login code",
                )
                if value != self._code:
                    if not await self._retries(handler, "That code is not correct."):
                        return
                    continue
                self._authorized = (
                    AuthorizationState.WAITING_PASSWORD
                    if self._requires_password
                    else AuthorizationState.READY
                )
                continue

            if self._authorized is AuthorizationState.WAITING_PASSWORD:
                self.submitted.append("password")
                value = require_credential(
                    await handler.request_password(PasswordHint(hint="the usual")),
                    name="Password",
                )
                if value != self._password:
                    if not await self._retries(handler, "That password is not correct."):
                        return
                    continue
                self._authorized = AuthorizationState.READY
                continue

            msg = f"Cannot authorize from {self._authorized.value}"
            raise TelegramError(msg, user_message="Signing in cannot continue.")

    async def logout(self) -> None:
        """Sign out on the Telegram side only, as the real gateway does."""
        self.logout_calls += 1
        self._authorized = AuthorizationState.LOGGED_OUT
        self._connection = ConnectionState.OFFLINE

    # -- Reading ----------------------------------------------------------

    async def get_me(self) -> TelegramUser:
        """Return the authenticated user."""
        self._require_authorized("get_me")
        return self._user

    async def list_chats(self, *, limit: int = DEFAULT_CHAT_LIMIT) -> tuple[TelegramChatInfo, ...]:
        """Return the scripted chats, in the order they were given."""
        self._require_authorized("list_chats")
        self.list_calls += 1
        return tuple(self._chats[:limit])

    async def get_chat(self, chat_id: TelegramChatId) -> TelegramChatInfo | None:
        """Return one scripted chat, or ``None`` if it is not among them."""
        self._require_authorized("get_chat")
        return next((chat for chat in self._chats if chat.id == chat_id), None)

    async def fetch_history(
        self,
        chat_id: TelegramChatId,
        *,
        before_message_id: TelegramMessageId | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> HistoryPage:
        """Return one page of the scripted history, newest first.

        Pages the same way TDLib does -- strictly older than the cursor, newest
        first -- so a backfill loop written against this fake is a backfill loop
        that works against Telegram.
        """
        self._require_authorized("fetch_history")
        stored = sorted(
            (m for m in self._history.get(int(chat_id), ()) if m.chat_id == chat_id),
            key=lambda m: int(m.id),
            reverse=True,
        )
        if before_message_id is not None:
            stored = [m for m in stored if int(m.id) < int(before_message_id)]

        page = tuple(stored[:limit])
        oldest = min((m.id for m in page), default=None)
        return HistoryPage(messages=page, oldest_message_id=oldest)

    def script_chats(self, *chats: TelegramChatInfo) -> None:
        """Replace the chats this gateway will report."""
        self._chats = list(chats)

    def script_history(self, chat_id: TelegramChatId, *messages: TelegramMessage) -> None:
        """Replace one chat's history."""
        self._history[int(chat_id)] = list(messages)

    def _require_authorized(self, operation: str) -> None:
        """Refuse a read that needs credentials when there are none."""
        self._require_connected(operation)
        if self._authorized is not AuthorizationState.READY:
            msg = f"{operation} requires an authorized account, state is {self._authorized.value}"
            raise AuthorizationError(msg, user_message="This account is not signed in to Telegram.")

    # -- Internals --------------------------------------------------------

    async def _retries(self, handler: AuthorizationHandler, explanation: str) -> bool:
        """Ask the handler whether to try again, raising when it declines."""
        error = AuthorizationError(explanation, user_message=explanation)
        if await handler.on_error(error) is RetryDecision.RETRY:
            return True
        raise error

    def _require_connected(self, operation: str) -> None:
        """Refuse an operation that needs a connection when there is not one."""
        if not self._connected:
            msg = f"{operation} requires a connected gateway"
            raise TdlibNotRunningError(msg, user_message="Not connected to Telegram.")


class ScriptedHandler(AuthorizationHandler):
    """An authorization handler that answers from a queue.

    Values are consumed in order, so a test can express "wrong code, then the
    right one" as a list rather than as a callback with state.
    """

    __slots__ = ("_codes", "_passwords", "_phones", "_retry", "errors", "states")

    def __init__(
        self,
        *,
        phones: list[str] | None = None,
        codes: list[str] | None = None,
        passwords: list[str] | None = None,
        retry: bool = True,
    ) -> None:
        """Queue the answers this handler will give."""
        self._phones = list(phones or ["+441234567890"])
        self._codes = list(codes or [ACCEPTED_CODE])
        self._passwords = list(passwords or [ACCEPTED_PASSWORD])
        self._retry = retry
        self.states: list[AuthorizationState] = []
        self.errors: list[Exception] = []

    async def request_phone_number(self) -> str:
        """Return the next queued phone number."""
        return self._next(self._phones, "phone number")

    async def request_code(self, hint: CodeHint) -> str:
        """Return the next queued code."""
        del hint
        return self._next(self._codes, "code")

    async def request_password(self, hint: PasswordHint) -> str:
        """Return the next queued password."""
        del hint
        return self._next(self._passwords, "password")

    async def on_state_change(self, state: AuthorizationState) -> None:
        """Record the state, without blocking."""
        self.states.append(state)

    async def on_error(self, error: Exception) -> RetryDecision:
        """Record the error and answer as configured."""
        self.errors.append(error)
        return RetryDecision.RETRY if self._retry else RetryDecision.ABORT

    @staticmethod
    def _next(values: list[str], what: str) -> str:
        """Pop the next queued answer, or fail loudly rather than looping."""
        if not values:
            msg = f"The scripted handler ran out of answers for the {what}"
            raise AssertionError(msg)
        return values.pop(0)
