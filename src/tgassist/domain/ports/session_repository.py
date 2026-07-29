"""Session repository port.

Scoped to one Account at construction (ADR-039), like every repository over
account-owned data — even though a Session is a single row, because the scope
is what makes "which account's session is this" unanswerable incorrectly.

Three operations, the same shape ``UserProfileRepository`` has and for the same
reason: exactly one row per account, so there is nothing to page and nothing to
look up by.

There is no ``delete``. A session is removed with its account, by cascade.
Logging out is a *transition* — it destroys the local store and the key, and
leaves a record saying so — because "this account was signed out" is a fact
worth keeping, and a deleted row cannot express it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.session import Session


@runtime_checkable
class SessionRepository(Protocol):
    """Stores the Telegram session record of one account.

    Satisfies the repository contract in ``domain/ports/repository.py``:
    absence returns ``None`` rather than raising, the repository never commits,
    and results are domain objects rather than rows.

    **It never holds key material.** ``Session.encryption_key_ref`` is a name in
    the ``SecretStore``; the key itself lives in the operating system credential
    store and never reaches this table (``SECURITY.md`` section 7).
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def get(self) -> Session | None:
        """Return this account's session, or ``None`` if it has none yet.

        ``None`` is an ordinary state: a session record is written when login is
        first prepared, not when the account is created.
        """
        ...

    async def add(self, session: Session) -> None:
        """Persist a new session record.

        Raises:
            DomainValidationError: If the session belongs to another account.
            ConstraintViolationError: If the account already has a session, or
                does not exist.
        """
        ...

    async def update(self, session: Session) -> None:
        """Persist a changed session record.

        Takes the whole entity, so the invariants checked when it was
        constructed are the invariants written.

        Raises:
            DomainValidationError: If the session belongs to another account.
            RecordNotFoundError: If the account has no session record.
        """
        ...
