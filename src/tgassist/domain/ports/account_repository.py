"""Account repository port.

Six operations, each traceable to a caller that exists. There is no ``update``,
no ``delete`` and no ``exists``, because nothing needs them yet -- and a method
with no caller is a method with no test, no measured query and no index.

* :meth:`add` -- account creation.
* :meth:`get` -- lookup by identifier.
* :meth:`get_by_telegram_id` -- the duplicate check on creation, and the lookup
  that authentication will perform in Milestone 2 once it learns who logged in.
* :meth:`get_active` -- almost every later operation needs to know which account
  is being operated, because every query is account-scoped.
* :meth:`list_accounts` -- the account list, and the multi-account picker later.
* :meth:`set_active` -- the only way to move the single-active invariant, which
  is otherwise unmaintainable.

Deletion is deliberately absent. Removing an Account must remove everything it
owns, transactionally and across every table -- the purge operation described in
``PRIVACY.md`` section 7. Implementing a partial version now would be a
liability: it would appear to work while leaving orphans in tables that do not
exist yet. Milestone 11 owns it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest


@runtime_checkable
class AccountRepository(Protocol):
    """Stores and retrieves accounts.

    Satisfies the repository contract in ``domain/ports/repository.py``, and is
    verified against it by the shared contract suite. Notably: absence returns
    ``None`` rather than raising, the repository never commits, and results are
    domain objects rather than rows.

    Account is the ownership root, so unlike every other repository this one is
    **not** account-scoped -- there is no outer scope to scope it to.
    """

    async def add(self, account: Account) -> None:
        """Persist a new account.

        Raises:
            ConstraintViolationError: If the identifier or Telegram user id is
                already taken, or if adding a second active account would break
                the single-active invariant.
        """
        ...

    async def get(self, account_id: AccountId) -> Account | None:
        """Return the account with this identifier, or ``None`` if absent."""
        ...

    async def get_by_telegram_id(self, telegram_user_id: TelegramUserId) -> Account | None:
        """Return the account for this Telegram user, or ``None`` if absent."""
        ...

    async def get_active(self) -> Account | None:
        """Return the account currently being operated, or ``None`` if none is.

        ``None`` is an ordinary state: a fresh installation has no account until
        the user creates one.
        """
        ...

    async def list_accounts(self, request: PageRequest) -> Page[Account]:
        """Return one page of accounts, newest first by default."""
        ...

    async def set_active(self, account_id: AccountId, now: datetime) -> Account:
        """Make this account the active one, deactivating any other.

        Both halves happen in the caller's transaction, so the single-active
        invariant is never briefly violated and never left broken by a failure
        between the two writes.

        Raises:
            RecordNotFoundError: If no account has this identifier. Unlike a
                lookup, this method promises a result: a caller asking to
                activate a nonexistent account has made a mistake worth
                reporting rather than silently ignoring.
        """
        ...
