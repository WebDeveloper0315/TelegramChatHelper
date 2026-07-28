"""In-memory account repository.

Written independently of the SQL implementation rather than wrapping it, so the
shared contract suite genuinely tests both. It is also the template for later
aggregate fakes: honour the invariant, sort with a tiebreaker, decode cursors.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from tgassist.domain.errors import ConstraintViolationError, RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.infrastructure.persistence.cursor import Cursor
from tgassist.infrastructure.persistence.pagination import SORT_KEY, TIEBREAK_KEY


class InMemoryAccountRepository(AccountRepository):
    """Stores accounts in a dictionary.

    Enforces the same invariants the schema does -- unique identifier, unique
    Telegram user, at most one active account -- because a fake that permits
    what the real store forbids lets a test pass on data that could never exist.
    """

    __slots__ = ("_accounts",)

    def __init__(self) -> None:
        """Create an empty repository."""
        self._accounts: dict[int, Account] = {}

    async def add(self, account: Account) -> None:
        """Persist a new account."""
        if int(account.id) in self._accounts:
            msg = f"An account with identifier {int(account.id)} already exists"
            raise ConstraintViolationError(msg, user_message="That account already exists.")
        if any(a.telegram_user_id == account.telegram_user_id for a in self._accounts.values()):
            msg = f"An account for Telegram user {int(account.telegram_user_id)} already exists"
            raise ConstraintViolationError(msg, user_message="That account already exists.")
        if account.is_active and any(a.is_active for a in self._accounts.values()):
            msg = "Another account is already active"
            raise ConstraintViolationError(msg, user_message="Another account is already active.")
        self._accounts[int(account.id)] = account

    async def get(self, account_id: AccountId) -> Account | None:
        """Return the account with this identifier, or ``None`` if absent."""
        found = self._accounts.get(int(account_id))
        # A distinct object, matching the no-identity-map contract.
        return replace(found) if found is not None else None

    async def get_by_telegram_id(self, telegram_user_id: TelegramUserId) -> Account | None:
        """Return the account for this Telegram user, or ``None`` if absent."""
        for account in self._accounts.values():
            if account.telegram_user_id == telegram_user_id:
                return replace(account)
        return None

    async def get_active(self) -> Account | None:
        """Return the account currently being operated, or ``None`` if none is."""
        for account in self._accounts.values():
            if account.is_active:
                return replace(account)
        return None

    async def list_accounts(self, request: PageRequest) -> Page[Account]:
        """Return one page of accounts."""
        descending = request.sort is None or request.sort.direction.is_descending
        ordered = sorted(
            self._accounts.values(),
            key=lambda a: (a.created_at, int(a.id)),
            reverse=descending,
        )

        position = Cursor.decode(request.cursor)
        if position is not None and SORT_KEY in position and TIEBREAK_KEY in position:
            marker = (
                datetime.fromisoformat(str(position[SORT_KEY])),
                int(position[TIEBREAK_KEY]),
            )
            ordered = [
                a
                for a in ordered
                if (
                    (a.created_at, int(a.id)) < marker
                    if descending
                    else (a.created_at, int(a.id)) > marker
                )
            ]

        limit = request.effective_limit()
        items = ordered[:limit]
        next_cursor = (
            Cursor.encode(
                {SORT_KEY: items[-1].created_at.isoformat(), TIEBREAK_KEY: int(items[-1].id)}
            )
            if len(ordered) > limit and items
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def set_active(self, account_id: AccountId, now: datetime) -> Account:
        """Make this account the active one, deactivating any other."""
        account = self._accounts.get(int(account_id))
        if account is None:
            msg = f"No account with identifier {int(account_id)}"
            raise RecordNotFoundError(msg, user_message="That account was not found.")
        if account.is_active:
            return account

        for key, other in list(self._accounts.items()):
            if other.is_active:
                self._accounts[key] = other.deactivated(now)

        activated = account.activated(now)
        self._accounts[int(account_id)] = activated
        return activated
