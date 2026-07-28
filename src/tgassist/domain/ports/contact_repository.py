"""Contact repository port.

Scoped to one Account at construction (ADR-039). No method takes an account
identifier, so there is no scope for a caller to get wrong and every query this
repository issues is filtered by the account it was built for. That matters more
here than it did for the profile: a profile is one row and a mistake is visible
immediately, whereas a contact list that quietly includes somebody else's
contacts looks exactly like a correct one.

Five operations, each traceable to a caller that exists:

* :meth:`add` -- ``contact add``.
* :meth:`get` -- ``contact show``, and the read before every state change.
* :meth:`get_by_telegram_id` -- the duplicate check on creation, and the lookup
  synchronisation performs in Milestone 3 for every message it ingests.
* :meth:`list_contacts` -- ``contact list``.
* :meth:`update` -- archive, restore, delete and rename, which are the same
  write with a different entity state.

There is no ``archive``, ``restore`` or ``delete`` method. Each would be
:meth:`update` with the transition decided by the repository, and deciding a
transition is the entity's work: ``Contact.archived`` already refuses to archive
a deleted contact, and a repository reimplementing that rule would be a second
place for it to be wrong.

There is no hard ``delete`` either. Removing a Contact must also remove every
Memory, Goal, Relationship Profile, Style Profile and Suggestion referencing it
(``PRIVACY.md`` section 7). A partial version now would appear to work while
leaving orphans in tables that do not exist yet. Milestone 11 owns it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import AccountId, ContactId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest


@runtime_checkable
class ContactRepository(Protocol):
    """Stores and retrieves the contacts of one account.

    Satisfies the repository contract in ``domain/ports/repository.py`` and is
    verified against it by the shared contract suite. Notably: absence returns
    ``None`` rather than raising, the repository never commits, results are
    domain objects rather than rows, and soft-deleted contacts are excluded
    unless explicitly requested.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def add(self, contact: Contact) -> None:
        """Persist a new contact.

        Raises:
            DomainValidationError: If the contact belongs to another account.
            ConstraintViolationError: If the identifier is taken, or if this
                account already knows this Telegram user -- including when the
                existing contact is soft-deleted, because a soft-deleted row
                still occupies the natural key. That is the point of soft
                deletion: the history is still there to be restored.
        """
        ...

    async def get(self, contact_id: ContactId, *, include_deleted: bool = False) -> Contact | None:
        """Return one of this account's contacts, or ``None`` if absent.

        Archived contacts are returned; deleted ones are not, unless asked for.
        The asymmetry follows what the two states mean: archived is "not in my
        way", deleted is "gone". Restoring a deleted contact is the one caller
        that needs to see past that, and it says so.
        """
        ...

    async def get_by_telegram_id(
        self, telegram_user_id: TelegramUserId, *, include_deleted: bool = False
    ) -> Contact | None:
        """Return this account's contact for a Telegram user, or ``None``.

        The duplicate check on creation passes ``include_deleted=True``: the
        unique index covers deleted rows, so a caller that looked only at live
        ones would report a constraint violation instead of the truth, which is
        that this person is already known and was deleted.
        """
        ...

    async def list_contacts(
        self, request: PageRequest, *, include_archived: bool = False
    ) -> Page[Contact]:
        """Return one page of this account's contacts, newest first by default.

        Deleted contacts are never listed. Archived ones are listed only when
        asked for, because the reason to archive somebody is to stop seeing
        them.
        """
        ...

    async def update(self, contact: Contact) -> None:
        """Persist a changed contact.

        Takes the whole entity rather than a set of fields, so the invariants
        checked when it was constructed are the invariants written.

        Raises:
            DomainValidationError: If the contact belongs to another account.
            RecordNotFoundError: If no row matches. An update that matches
                nothing is a caller mistake worth reporting, rather than a
                silent success.
        """
        ...
