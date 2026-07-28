"""In-memory user profile repository.

Written independently of the SQL implementation, and — importantly — sharing a
storage dictionary across instances. A fake whose scope is enforced by holding
only its own account's data would pass a leakage test for the wrong reason: it
would have nothing to leak. Sharing the store means the scope has to do real
work, exactly as the ``WHERE account_id = ?`` clause does in SQL.
"""

from __future__ import annotations

from dataclasses import replace

from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.user_profile import UserProfile
from tgassist.domain.ports.user_profile_repository import UserProfileRepository


class InMemoryProfileStore:
    """Shared storage behind the in-memory repositories.

    Standing in for the table: several scoped repositories read and write one
    store, so account scoping is genuinely tested rather than assumed.
    """

    __slots__ = ("_known_accounts", "profiles")

    def __init__(self, known_accounts: set[int] | None = None) -> None:
        """Create a store, optionally with a set of accounts that exist."""
        self.profiles: dict[int, UserProfile] = {}
        # Stands in for the foreign key. Without it the fake would accept a
        # profile for an account that does not exist, which the schema refuses.
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
        """Delete an account and cascade to its profile, as the schema does."""
        if self._known_accounts is not None:
            self._known_accounts.discard(int(account_id))
        self.profiles.pop(int(account_id), None)


class InMemoryUserProfileRepository(UserProfileRepository):
    """Stores one account's profile in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemoryProfileStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def get(self) -> UserProfile | None:
        """Return this account's profile, or ``None`` if it has none yet."""
        found = self._store.profiles.get(int(self._account_id))
        # A distinct object, matching the no-identity-map contract.
        return replace(found) if found is not None else None

    async def add(self, profile: UserProfile) -> None:
        """Persist a new profile."""
        self._require_own(profile, operation="add")
        if not self._store.account_exists(profile.account_id):
            msg = f"No account {int(profile.account_id)} to own this profile"
            raise ConstraintViolationError(msg, user_message="That account does not exist.")
        if int(profile.account_id) in self._store.profiles:
            msg = f"Account {int(profile.account_id)} already has a profile"
            raise ConstraintViolationError(msg, user_message="That account already has a profile.")
        self._store.profiles[int(profile.account_id)] = profile

    async def update(self, profile: UserProfile) -> None:
        """Replace this account's profile."""
        self._require_own(profile, operation="update")
        existing = self._store.profiles.get(int(self._account_id))
        if existing is None:
            msg = f"Account {int(self._account_id)} has no profile to update"
            raise RecordNotFoundError(msg, user_message="That account has no profile yet.")
        # created_at belongs to the original row, exactly as in SQL.
        self._store.profiles[int(self._account_id)] = replace(
            profile, created_at=existing.created_at
        )

    def _require_own(self, profile: UserProfile, *, operation: str) -> None:
        """Refuse a profile belonging to a different account."""
        if profile.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a profile for account {int(profile.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That profile belongs to a different account."
            )
