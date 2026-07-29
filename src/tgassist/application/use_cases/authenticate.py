"""Authentication use cases.

Two operations, and the boundary between them is who owns what: the gateway
owns the conversation with Telegram, and these own what is written down about
it. The gateway never touches the database, so if a login is to survive a
restart, something above it must record the outcome. That is this module.

The identity check
------------------

An Account records the Telegram user it belongs to. A login that authenticated
as somebody else must be refused rather than recorded: the account already owns
chats, contacts and messages belonging to the first person, and attaching a
second person's session to them would mix two histories that can never be
separated again.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.application.use_cases.session import PrepareSession
from tgassist.domain.errors import AuthorizationError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.session import AuthorizationState, Session
from tgassist.domain.model.telegram import TelegramUser
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.domain.ports.session_repository import SessionRepository
from tgassist.domain.ports.telegram_gateway import AuthorizationHandler, TelegramGateway
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class LoginResult:
    """What a completed login established.

    Attributes:
        session: The stored session, now authorized.
        user: Who Telegram says we are. Returned so the caller can confirm the
            login to the person who asked for it, which is the one moment where
            "signed in as somebody else" is still cheap to notice.
        was_already_authorized: Whether this login had nothing to do because the
            stored session was still valid. The ordinary case after a restart,
            and worth reporting rather than implying a fresh sign-in happened.
    """

    session: Session
    user: TelegramUser
    was_already_authorized: bool


class AuthenticateAccount:
    """Signs an account in to Telegram and records the result.

    Idempotent in the way that matters: a session that is still valid is
    recognised rather than replaced, so running this after a restart connects
    and returns instead of asking for a code that is not needed.
    """

    __slots__ = ("_accounts", "_clock", "_prepare", "_sessions", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        sessions: ScopedRepositoryFactory[SessionRepository],
        accounts: RepositoryFactory[AccountRepository],
        prepare: PrepareSession,
        clock: Clock,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._sessions = sessions
        self._accounts = accounts
        self._prepare = prepare
        self._clock = clock

    async def execute(
        self,
        gateway: TelegramGateway,
        handler: AuthorizationHandler,
        account_id: AccountId | None = None,
    ) -> LoginResult:
        """Connect, authorize if necessary, verify identity and store the outcome.

        The gateway is a parameter rather than a constructor dependency because
        it holds a live connection. Something has to open and close it, and a
        use case built once per call has no lifetime to hang that on
        (``TELEGRAM_ARCHITECTURE.md`` section 7.3).

        Args:
            gateway: A connected-or-connectable gateway bound to this account.
            handler: Supplies credentials. Never asked for any if the stored
                session is still valid.
            account_id: Account to sign in. ``None`` selects the active one.

        Returns:
            What the login established.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
            AuthorizationError: If the gateway is bound to a different account,
                or the login authenticated as a different Telegram user.
            SecretStoreUnavailableError: If no credential backend is available.
        """
        resolved = await self._resolve(account_id)
        self._require_same_account(gateway, resolved)

        # Preparing first: the gateway cannot connect without the store path and
        # the encryption key, and this is what creates them.
        await self._prepare.execute(resolved)

        await gateway.connect()
        was_authorized = await gateway.authorization_state() is AuthorizationState.READY

        await gateway.start_authorization(handler)
        user = await gateway.get_me()

        await self._require_same_person(resolved, user)
        session = await self._record(resolved, gateway)
        return LoginResult(session=session, user=user, was_already_authorized=was_authorized)

    async def _resolve(self, account_id: AccountId | None) -> AccountId:
        """Return the account to sign in."""
        async with self._unit_of_work() as uow:
            return await resolve_account(self._accounts(uow), account_id)

    async def _require_same_person(self, account_id: AccountId, user: TelegramUser) -> None:
        """Refuse a login that authenticated as somebody else.

        Raises:
            AuthorizationError: If Telegram's identity is not the one this
                account records. The account's existing history belongs to the
                first person, and there is no way to unmix two.
        """
        async with self._unit_of_work() as uow:
            account = await self._accounts(uow).get(account_id)

        if account is not None and account.telegram_user_id != user.id:
            msg = (
                f"Account {int(account_id)} records Telegram user "
                f"{int(account.telegram_user_id)}, but the login authenticated as "
                f"{int(user.id)}"
            )
            raise AuthorizationError(
                msg,
                user_message=(
                    "That sign-in is for a different Telegram account than this "
                    "one holds data for. Create a separate account to use it."
                ),
                context={
                    "account_id": int(account_id),
                    "expected": int(account.telegram_user_id),
                    "actual": int(user.id),
                },
            )

    async def _record(self, account_id: AccountId, gateway: TelegramGateway) -> Session:
        """Store what the gateway now reports.

        Both axes are written from the gateway rather than assumed: an authorized
        account whose connection is still catching up is an ordinary state, and
        recording it as fully ready would be a lie the next command believes.
        """
        authorization = await gateway.authorization_state()
        connection = await gateway.connection_state()
        now = self._clock.now()

        async with self._unit_of_work() as uow:
            sessions = self._sessions(uow, account_id)
            stored = await sessions.get()
            if stored is None:  # pragma: no cover - prepare guarantees one
                msg = f"Account {int(account_id)} has no session to record a login against"
                raise AuthorizationError(msg, user_message="That account has no session.")

            updated = (
                stored.with_authorization(authorization, now)
                .with_connection(connection, now)
                .with_activity(now)
            )
            await sessions.update(updated)
            await uow.commit()
            return updated

    @staticmethod
    def _require_same_account(gateway: TelegramGateway, account_id: AccountId) -> None:
        """Refuse a gateway bound to a different account."""
        if gateway.account_id != account_id:
            msg = f"Gateway is bound to account {int(gateway.account_id)}, not {int(account_id)}"
            raise AuthorizationError(
                msg,
                user_message="That connection belongs to a different account.",
                context={"gateway": int(gateway.account_id), "account": int(account_id)},
            )


class LogOutAccount:
    """Signs an account out and destroys what the session left behind.

    Three things happen, in an order chosen so that a failure part-way through
    leaves nothing usable rather than something half-usable:

    1. Telegram is told, so the session is invalid everywhere rather than only
       here.
    2. The record is updated, so the application knows what happened.
    3. The local store and its key are destroyed, because the store is worthless
       without the key and the key is dangerous without a purpose.
    """

    __slots__ = ("_accounts", "_clock", "_secrets", "_sessions", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        sessions: ScopedRepositoryFactory[SessionRepository],
        accounts: RepositoryFactory[AccountRepository],
        secret_store: SecretStore,
        clock: Clock,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._sessions = sessions
        self._accounts = accounts
        self._secrets = secret_store
        self._clock = clock

    async def execute(
        self, gateway: TelegramGateway, account_id: AccountId | None = None
    ) -> Session | None:
        """Sign out, record it, and destroy the local session material.

        Args:
            gateway: A gateway bound to this account. Connected if Telegram is
                to be told; a disconnected one still lets the local material be
                destroyed.
            account_id: Account to sign out. ``None`` selects the active one.

        Returns:
            The stored session, now logged out, or ``None`` if the account never
            had one — signing out of nothing is not an error.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            stored = await self._sessions(uow, resolved).get()

        if stored is None:
            return None

        await gateway.logout()
        updated = await self._mark_logged_out(resolved, stored)
        _destroy_directory(stored.session_path)
        await self._secrets.delete(stored.encryption_key_ref)
        return updated

    async def _mark_logged_out(self, account_id: AccountId, stored: Session) -> Session:
        """Record the logout before anything is destroyed."""
        updated = stored.logged_out(self._clock.now())
        async with self._unit_of_work() as uow:
            await self._sessions(uow, account_id).update(updated)
            await uow.commit()
        return updated


def _destroy_directory(path: Path) -> None:
    """Remove a session store, tolerating one that is already gone.

    Errors are ignored deliberately: on Windows a file the native library still
    holds open cannot be removed, and failing here would leave the session
    marked as logged out but refuse to complete. The key is deleted either way,
    which is what makes the remaining bytes unreadable.
    """
    shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "AuthenticateAccount",
    "LogOutAccount",
    "LoginResult",
]
