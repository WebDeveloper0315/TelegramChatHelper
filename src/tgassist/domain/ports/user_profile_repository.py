"""UserProfile repository port.

**The account scope is a constructor parameter, not a method argument.**

This is the first aggregate owned by an Account, so it is where the scoping
rule stops being a convention. Every other repository in this system will hold
account-owned data, and the failure mode is severe: a query that forgets its
scope returns another person's conversations, and returns them silently, with
no error and no obvious symptom.

The obvious interface takes the scope per call::

    async def get(self, account_id: AccountId) -> UserProfile | None

That relies on every caller passing the right value every time. It is correct
until someone passes a variable that is stale, or reuses one from an outer loop,
and then it is quietly wrong.

Scoping at construction removes the possibility. The repository is built for one
account and its methods take no account argument, so there is no value to get
wrong. Reaching another account's data requires constructing a second repository
with a different scope -- which is a visible act in the code, not an oversight.

See ADR-039.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.user_profile import UserProfile


@runtime_checkable
class UserProfileRepository(Protocol):
    """Stores the profile for exactly one Account.

    Three operations, each with a caller:

    * :meth:`get` -- ``profile show``, and every later component that needs the
      operator's preferences.
    * :meth:`add` -- creation on first access.
    * :meth:`update` -- ``profile set``.

    There is no ``delete``. A profile is deleted with its Account, by the
    database cascade, so an application-level deletion would be a way to leave
    an Account without the profile it is required to have.

    There is no ``list``. Exactly one profile exists per Account, and this
    repository is scoped to one Account, so a list would return either zero or
    one row -- which :meth:`get` already answers more clearly.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to.

        Exposed so a caller can assert what it is working with, and so a test
        can prove that two repositories are genuinely separate.
        """
        ...

    async def get(self) -> UserProfile | None:
        """Return this account's profile, or ``None`` if it has none yet.

        ``None`` is an ordinary state: a profile is created on first access
        rather than as part of account creation, so that adding an account does
        not require deciding preferences.
        """
        ...

    async def add(self, profile: UserProfile) -> None:
        """Persist a new profile.

        Raises:
            ConstraintViolationError: If this account already has a profile, or
                if the account does not exist. Both are enforced by the schema:
                the account identifier is the primary key and a foreign key.
            DomainValidationError: If the profile belongs to another account.
        """
        ...

    async def update(self, profile: UserProfile) -> None:
        """Replace this account's profile.

        Raises:
            RecordNotFoundError: If the account has no profile. Unlike
                :meth:`get`, this method promises to change something.
            DomainValidationError: If the profile belongs to another account.
        """
        ...
