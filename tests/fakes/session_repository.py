"""In-memory session repository.

Written independently of the SQL implementation, and sharing a storage
dictionary across instances for the reason given in
``tests/fakes/user_profile_repository.py``: a fake that holds only its own
account's data would pass a leakage test by having nothing to leak.
"""

from __future__ import annotations

from dataclasses import replace

from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.session import Session
from tgassist.domain.ports.session_repository import SessionRepository


class InMemorySessionStore:
    """Shared storage behind the in-memory repositories.

    Standing in for the table: several scoped repositories read and write one
    store, so account scoping is genuinely tested rather than assumed.
    """

    __slots__ = ("_known_accounts", "sessions")

    def __init__(self, known_accounts: set[int] | None = None) -> None:
        """Create a store, optionally with a set of accounts that exist."""
        self.sessions: dict[int, Session] = {}
        # Stands in for the foreign key. Without it the fake would accept a
        # session for an account that does not exist, which the schema refuses.
        self._known_accounts = known_accounts

    def account_exists(self, account_id: AccountId) -> bool:
        """Report whether the referenced account exists."""
        if self._known_accounts is None:
            return True
        return int(account_id) in self._known_accounts

    def register_account(self, account_id: AccountId) -> None:
        """Record an account as existing."""
        if self._known_accounts is not None:
            self._known_accounts.add(int(account_id))

    def delete_account(self, account_id: AccountId) -> None:
        """Delete an account and cascade to its session, as the schema does."""
        if self._known_accounts is not None:
            self._known_accounts.discard(int(account_id))
        self.sessions.pop(int(account_id), None)


class InMemorySessionRepository(SessionRepository):
    """Stores one account's session record in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemorySessionStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def get(self) -> Session | None:
        """Return this account's session, or ``None`` if it has none yet."""
        found = self._store.sessions.get(int(self._account_id))
        # A distinct object, matching the no-identity-map contract.
        return replace(found) if found is not None else None

    async def add(self, session: Session) -> None:
        """Persist a new session record."""
        self._require_own(session, operation="add")
        if not self._store.account_exists(session.account_id):
            msg = f"No account {int(session.account_id)} to own this session"
            raise ConstraintViolationError(msg, user_message="That account does not exist.")
        if int(session.account_id) in self._store.sessions:
            msg = f"Account {int(session.account_id)} already has a session"
            raise ConstraintViolationError(msg, user_message="That account already has a session.")
        self._store.sessions[int(session.account_id)] = session

    async def update(self, session: Session) -> None:
        """Replace this account's session record."""
        self._require_own(session, operation="update")
        existing = self._store.sessions.get(int(self._account_id))
        if existing is None:
            msg = f"Account {int(self._account_id)} has no session to update"
            raise RecordNotFoundError(msg, user_message="That account has no session yet.")
        # created_at belongs to the original row, exactly as in SQL.
        self._store.sessions[int(self._account_id)] = replace(
            session, created_at=existing.created_at
        )

    def _require_own(self, session: Session, *, operation: str) -> None:
        """Refuse a session belonging to a different account."""
        if session.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a session for account {int(session.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That session belongs to a different account."
            )
