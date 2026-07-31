"""The Telegram boundary.

Two protocols. :class:`TelegramGateway` is the only way anything reaches
Telegram; :class:`AuthorizationHandler` is the only way a credential reaches the
gateway.

`TelegramGateway` is declared one slice at a time (ADR-051). It now covers
lifecycle, authorization, the operator's own identity, reading chats, contacts
and history, and the live update stream. Sending arrives with the code that
calls it: a protocol listing methods no caller uses cannot be verified by a
contract suite, and a fake would have to invent behaviour for them.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

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
    TelegramUpdate,
    TelegramUser,
)

#: How many chats to return when a caller does not say. Generous, because the
#: answer a person expects from "list my chats" is all of them, and Telegram
#: returning fewer is not an error.
DEFAULT_CHAT_LIMIT: Final = 200

#: How many messages to ask for in one history page. Aligned with TDLib's own
#: practical page size, and with the transaction granularity the sync engine
#: will use (``TELEGRAM_ARCHITECTURE.md`` section 8.4).
DEFAULT_HISTORY_LIMIT: Final = 100

#: How many updates to hold before backpressure reaches TDLib.
#:
#: Generous enough that an ordinary ingestion pause -- one transaction, tens of
#: milliseconds -- never stalls the dispatch loop, and small enough that a
#: consumer which has genuinely stopped is noticed rather than absorbed into
#: memory. Configured at ``telegram.update_queue_size``.
DEFAULT_UPDATE_QUEUE_SIZE: Final = 1000

#: How many address-book entries to resolve when a caller does not say.
#:
#: Telegram caps a personal address book at 5 000, and returns the whole thing
#: in one answer. This is a ceiling on how many of those entries are *resolved*
#: into users, so that a single command cannot turn into thousands of lookups
#: without the caller having asked for it.
DEFAULT_CONTACT_LIMIT: Final = 1000


class RetryDecision(StrEnum):
    """What to do after Telegram rejected a credential."""

    RETRY = "retry"
    """Ask for it again. The ordinary answer to a mistyped code."""

    ABORT = "abort"
    """Give up. The login ends and the error reaches the caller."""


@runtime_checkable
class AuthorizationHandler(Protocol):
    """Supplies credentials during login, without the gateway knowing any UI.

    Contract, guaranteed by every implementation:

    1. Each ``request_*`` method returns the value the user supplied. Returning
       an empty string is rejected by the caller rather than sent to Telegram.
    2. **Nothing here stores, logs or retains what it returns.** A code or a
       password exists for one submission. An implementation that caches one
       for a retry has kept a credential, which is the thing this protocol
       exists to avoid.
    3. :meth:`on_state_change` is informational and must not block on user
       input. It is called from the gateway's dispatch path, so a handler that
       waits there stops every other update.
    4. :meth:`on_error` decides whether to retry. It receives the *reason*
       Telegram gave, never the value that was rejected.
    5. Cancellation propagates. A handler waiting on input must let
       ``asyncio.CancelledError`` through so shutdown is not delayed by a prompt
       nobody is answering.
    """

    async def request_phone_number(self) -> str:
        """Return the phone number to log in with, in international format."""
        ...

    async def request_code(self, hint: CodeHint) -> str:
        """Return the login code Telegram has just sent."""
        ...

    async def request_password(self, hint: PasswordHint) -> str:
        """Return the two-factor password."""
        ...

    async def on_state_change(self, state: AuthorizationState) -> None:
        """Report that the authorization state moved. Must not block."""
        ...

    async def on_error(self, error: Exception) -> RetryDecision:
        """Decide whether to retry after Telegram rejected a credential."""
        ...


@runtime_checkable
class TelegramGateway(Protocol):
    """The sole boundary to Telegram. One instance per Account.

    Contract, guaranteed by every implementation and verified by the shared
    contract suite:

    1. **It never writes to the database.** Persisting what it returns is the
       application layer's job, which is what makes the sync engine testable
       with a fake gateway and a real database.
    2. It is bound to one account at construction, like every repository over
       account-owned data (ADR-039). No method takes an account identifier.
    3. :meth:`connect` and :meth:`disconnect` are idempotent. Connecting twice
       is not an error, and disconnecting something already stopped is how
       every cleanup path ends.
    4. Operations that need a connection raise ``TdlibNotRunningError`` when
       there is not one, rather than connecting implicitly. A method that
       silently opens a network connection is a method whose cost is invisible.
    5. **There is no method for sending typing indicators.** Its absence is the
       structural expression of ADR-023 section 2: what cannot be called cannot
       be used to imitate a human being.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this gateway is bound to."""
        ...

    async def connect(self) -> None:
        """Start the client and bring it to a usable state. Idempotent.

        Raises:
            TelegramNotConfiguredError: If the application credentials Telegram
                requires are absent.
            TdlibNotFoundError: If no verified native library is available.
        """
        ...

    async def disconnect(self) -> None:
        """Stop the client and release the native handle. Idempotent."""
        ...

    async def is_connected(self) -> bool:
        """Report whether a usable connection exists right now."""
        ...

    async def authorization_state(self) -> AuthorizationState:
        """Return where this account stands with Telegram's login flow."""
        ...

    async def connection_state(self) -> ConnectionState:
        """Return whether a connection to Telegram currently exists."""
        ...

    async def start_authorization(self, handler: AuthorizationHandler) -> None:
        """Drive the login flow to completion, asking ``handler`` for credentials.

        Returns when the account is authorized. Returns immediately when it
        already was, so a caller need not check first — which matters because
        the common case after a restart is a session that is still valid.

        Raises:
            AuthorizationError: If a credential was rejected and the handler
                chose not to retry.
            TelegramError: If Telegram refused for any other reason.
        """
        ...

    async def logout(self) -> None:
        """Sign this account out of Telegram and invalidate the session.

        Telegram-side only. Destroying the local store and the key is the
        caller's job: a gateway that deleted files would be reaching past its
        own boundary.
        """
        ...

    async def get_me(self) -> TelegramUser:
        """Return the authenticated user.

        Raises:
            TdlibNotRunningError: If there is no connection.
            AuthorizationError: If the account is not authorized.
        """
        ...

    async def list_chats(self, *, limit: int = DEFAULT_CHAT_LIMIT) -> tuple[TelegramChatInfo, ...]:
        """Return this account's chats, most recently active first.

        Args:
            limit: How many to return at most. A ceiling rather than a page
                size: this returns what Telegram has, and asking for more than
                exists is not an error.

        Raises:
            TdlibNotRunningError: If there is no connection.
            AuthorizationError: If the account is not authorized.
            TelegramError: If Telegram refused.
        """
        ...

    async def get_chat(self, chat_id: TelegramChatId) -> TelegramChatInfo | None:
        """Return one chat, or ``None`` if this account cannot see it.

        ``None`` rather than an error: a chat the account has left, or never had
        access to, is an ordinary answer to "does this exist for me".

        Raises:
            TdlibNotRunningError: If there is no connection.
            AuthorizationError: If the account is not authorized.
        """
        ...

    def updates(self) -> AsyncGenerator[TelegramUpdate]:
        """Yield what Telegram reports without being asked, in arrival order.

        The stream begins filling at :meth:`connect`, **before** anything starts
        consuming it, and it is buffered. That ordering is the whole of why a
        run cannot lose an update to its own start-up: whatever arrives while
        chats are being synchronised and history is being back-filled is already
        held, and is delivered when a consumer first asks (ADR-055).

        Contract, guaranteed by every implementation:

        1. **Arrival order is preserved.** Telegram's order is the only order
           there is; nothing here re-sorts.
        2. **At most one consumer.** The stream is a queue, not a broadcast, so
           a second iterator would not see a copy -- it would take turns, and
           each would silently miss what the other took.
        3. **The buffer is bounded, and full means slow rather than lost.** A
           consumer that falls behind eventually stops the dispatch loop, which
           stops the receive thread, which makes TDLib buffer. Backpressure
           reaches the far end rather than the queue growing without limit.
        4. **Unknown update kinds never appear and never stop the stream.** They
           are counted (:attr:`unhandled_updates`) and dropped, because a
           TDLib version that added a kind must not take the application down.
        5. **Iteration ends when the gateway disconnects**, and updates still
           queued at that moment are abandoned. Recovering them is the caller's
           catch-up pass on the next run, not a durable queue here (ADR-055).
        6. **Closing releases the consumer slot.** A generator rather than a
           plain iterator for exactly this reason: a caller that takes a few
           updates and stops must be able to let go, or nothing could ever
           stream again.

        Raises:
            TdlibNotRunningError: If there is no connection.
        """
        ...

    async def get_contact(self, user_id: TelegramUserId) -> TelegramUser | None:
        """Return one Telegram user, or ``None`` if this account cannot see them.

        ``None`` rather than an error, for the same reason :meth:`get_chat`
        answers that way: an account that has been deleted, or that this account
        has no visibility of, is an ordinary answer to "who is this".

        A chat carries its counterpart's *name*, but not their handle. This is
        what supplies the handle, which is why it exists now and did not before.

        Raises:
            TdlibNotRunningError: If there is no connection.
            AuthorizationError: If the account is not authorized.
        """
        ...

    async def list_contacts(
        self, *, limit: int = DEFAULT_CONTACT_LIMIT
    ) -> tuple[TelegramUser, ...]:
        """Return the people in this account's Telegram address book.

        A different population from :meth:`list_chats`: the address book holds
        people this account has saved, including ones it has never messaged,
        and excludes everybody it has only ever been messaged *by*. Neither set
        contains the other, which is why both are read.

        Args:
            limit: How many entries to resolve at most. A ceiling rather than a
                page size; Telegram returns the whole book in one answer.

        Raises:
            TdlibNotRunningError: If there is no connection.
            AuthorizationError: If the account is not authorized.
            TelegramError: If Telegram refused.
        """
        ...

    async def fetch_history(
        self,
        chat_id: TelegramChatId,
        *,
        before_message_id: TelegramMessageId | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> HistoryPage:
        """Return one page of a chat's history, newest first.

        Backwards from newest is the only direction Telegram's history API
        supports efficiently, and the only one where an interruption leaves a
        contiguous stored range.

        Args:
            chat_id: The chat to read.
            before_message_id: Continue from just before this message, or
                ``None`` to start at the newest.
            limit: How many messages to ask for. Telegram may return fewer for
                reasons of its own, which is why the page carries its own
                boundary rather than leaving the caller to infer one.

        Raises:
            TdlibNotRunningError: If there is no connection.
            AuthorizationError: If the account is not authorized.
            TelegramError: If Telegram refused, including for a chat this
                account cannot read.
        """
        ...


__all__ = [
    "DEFAULT_CHAT_LIMIT",
    "DEFAULT_CONTACT_LIMIT",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_UPDATE_QUEUE_SIZE",
    "AuthorizationHandler",
    "RetryDecision",
    "TelegramGateway",
]
