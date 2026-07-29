"""Session use cases.

One operation, and it exists before authentication does: preparing the storage
a login will need.

Logging in, collecting a code and handling a password belong to the
authentication slice and are deliberately absent here. What this module
establishes is the thing authentication cannot do without — a directory to
write to and a key to encrypt it with, recorded so that both survive a restart.

Reading a session back has no caller yet: nothing can display an authorization
state that nothing can change. It arrives with the login command rather than
being written now against a guess at what that command will want.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.domain.errors import SecretStoreUnavailableError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.model.session import Session
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.domain.ports.session_repository import SessionRepository
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory

#: Bytes of entropy in a generated session key. TDLib accepts an arbitrary key
#: for its local store; this is well above the 128 bits at which brute force
#: stops being the attack anyone would choose.
KEY_BYTES = 32

#: Prefix for the credential-store name under which a session key is held. The
#: name is derived from the account, not random, so a key can still be found
#: after a crash that lost the row -- and so a stale key is overwritten rather
#: than orphaned.
KEY_NAME_PREFIX = "telegram-session-key"


def session_key_name(account_id: AccountId) -> str:
    """Return the credential-store name for an account's session key."""
    return f"{KEY_NAME_PREFIX}-{int(account_id)}"


def generate_session_key() -> SecretValue:
    """Generate a new session encryption key.

    Uses :mod:`secrets` directly rather than through an injectable port. A seam
    here would let a test -- or anything else -- substitute a predictable
    generator for the one protecting every message the user has ever sent, and
    nothing needs a second implementation. Tests assert the properties that
    matter (length, and that two keys differ) rather than an exact value.
    """
    return SecretValue(secrets.token_urlsafe(KEY_BYTES))


class PrepareSession:
    """Gives an account the storage and key a login will need.

    Idempotent: an account that already has a session gets the existing one
    back, unchanged. Generating a second key would be worse than useless — it
    would make the store the first key encrypted permanently unreadable.
    """

    __slots__ = ("_accounts", "_clock", "_secrets", "_sessions", "_sessions_dir", "_unit_of_work")

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        sessions: ScopedRepositoryFactory[SessionRepository],
        accounts: RepositoryFactory[AccountRepository],
        secret_store: SecretStore,
        clock: Clock,
        sessions_dir: Path,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory.
            sessions: Builds a repository scoped to one account.
            accounts: Resolves which account is meant.
            secret_store: Where the generated key is put. The only component
                permitted to hold it.
            clock: Supplies the creation time.
            sessions_dir: Root under which each account's store lives. A
                plain path rather than a configuration object, so this use case
                depends on the one value it needs; the composition root passes
                ``config.paths.sessions_dir``.
        """
        self._unit_of_work = unit_of_work
        self._sessions = sessions
        self._accounts = accounts
        self._secrets = secret_store
        self._clock = clock
        self._sessions_dir = sessions_dir

    async def execute(self, account_id: AccountId | None = None) -> Session:
        """Return the account's session, creating it on first use.

        The key is written to the credential store *before* the row is written,
        and the transaction commits only afterwards. Taking that order the other
        way round would allow a row naming a key that was never stored, which
        looks exactly like a working session until the first login fails.

        The reverse leftover — a stored key with no row — costs nothing: the
        name is derived from the account, so the next attempt overwrites it.

        Args:
            account_id: Account to prepare. ``None`` selects the active one.

        Returns:
            The session, whether it was just created or already existed.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
            SecretStoreUnavailableError: If no credential backend is available.
                There is nowhere else a session key may go — an unencrypted
                fallback does not exist (``SECURITY.md`` section 8) — so this is
                fatal regardless of ``security.require_secret_store``, which
                governs startup rather than this operation.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            sessions = self._sessions(uow, resolved)

            existing = await sessions.get()
            if existing is not None:
                return existing

            if not await self._secrets.is_available():
                msg = "No credential store is available to hold the session key"
                raise SecretStoreUnavailableError(
                    msg,
                    user_message=(
                        "The system credential store is unavailable, so a Telegram "
                        "session cannot be created."
                    ),
                    context={"account_id": int(resolved)},
                )

            key_name = session_key_name(resolved)
            await self._secrets.set(key_name, generate_session_key())

            session = Session.prepare(
                account_id=resolved,
                session_path=self._session_path(resolved),
                encryption_key_ref=key_name,
                now=self._clock.now(),
            )
            await sessions.add(session)
            await uow.commit()
            return session

    def _session_path(self, account_id: AccountId) -> Path:
        """Return where this account's encrypted store belongs.

        One directory per account under the configured root. The directory
        itself is not created here: TDLib creates its own store when it opens
        it, and the root already carries owner-only permissions applied at
        startup.
        """
        return self._sessions_dir / str(int(account_id))


__all__ = [
    "KEY_BYTES",
    "KEY_NAME_PREFIX",
    "PrepareSession",
    "generate_session_key",
    "session_key_name",
]
