"""In-memory contact repository.

Written independently of the SQL implementation and sharing one store across
scoped instances, for the same reason the profile fake does: a fake holding only
its own account's rows would pass an isolation test by having nothing to leak.

The store also stands in for the two constraints that would otherwise be
untested here -- the foreign key to ``accounts`` and the unique index on
``(account_id, telegram_user_id)``. A fake that accepted rows the schema refuses
would make every use-case test built on it a false positive.
"""

from __future__ import annotations

from dataclasses import replace

from tests.fakes.pagination import paginate
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import AccountId, ContactId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.contact_repository import ContactRepository


class InMemoryContactStore:
    """Shared storage behind the in-memory repositories."""

    __slots__ = ("_known_accounts", "contacts")

    def __init__(self, known_accounts: set[int] | None = None) -> None:
        """Create a store, optionally with a set of accounts that exist."""
        self.contacts: dict[int, Contact] = {}
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
        """Delete an account and cascade to its contacts, as the schema does."""
        if self._known_accounts is not None:
            self._known_accounts.discard(int(account_id))
        for contact_id in [
            key for key, value in self.contacts.items() if value.account_id == account_id
        ]:
            del self.contacts[contact_id]


class InMemoryContactRepository(ContactRepository):
    """Stores one account's contacts in a shared dictionary."""

    __slots__ = ("_account_id", "_store")

    def __init__(self, store: InMemoryContactStore, account_id: AccountId) -> None:
        """Bind to a store and an account scope."""
        self._store = store
        self._account_id = account_id

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def add(self, contact: Contact) -> None:
        """Persist a new contact."""
        self._require_own(contact, operation="add")
        if not self._store.account_exists(contact.account_id):
            msg = f"No account {int(contact.account_id)} to own this contact"
            raise ConstraintViolationError(msg, user_message="That account does not exist.")
        if int(contact.id) in self._store.contacts:
            msg = f"Contact {int(contact.id)} already exists"
            raise ConstraintViolationError(msg, user_message="That contact already exists.")
        # The unique index covers soft-deleted rows, so this search must too.
        if self._by_telegram_id(contact.telegram_user_id) is not None:
            msg = (
                f"Account {int(contact.account_id)} already knows Telegram user "
                f"{int(contact.telegram_user_id)}"
            )
            raise ConstraintViolationError(
                msg, user_message="This account already knows that Telegram user."
            )
        self._store.contacts[int(contact.id)] = contact

    async def get(self, contact_id: ContactId, *, include_deleted: bool = False) -> Contact | None:
        """Return one of this account's contacts, or ``None`` if absent."""
        found = self._store.contacts.get(int(contact_id))
        if found is None or found.account_id != self._account_id:
            return None
        if found.is_deleted and not include_deleted:
            return None
        # A distinct object, matching the no-identity-map contract.
        return replace(found)

    async def get_by_telegram_id(
        self, telegram_user_id: TelegramUserId, *, include_deleted: bool = False
    ) -> Contact | None:
        """Return this account's contact for a Telegram user, or ``None``."""
        found = self._by_telegram_id(telegram_user_id)
        if found is None or (found.is_deleted and not include_deleted):
            return None
        return replace(found)

    async def list_contacts(
        self, request: PageRequest, *, include_archived: bool = False
    ) -> Page[Contact]:
        """Return one page of this account's contacts."""
        visible = [
            contact
            for contact in self._store.contacts.values()
            if contact.account_id == self._account_id
            and not contact.is_deleted
            and (include_archived or not contact.is_archived)
        ]
        return paginate(
            visible,
            request,
            sort_key=lambda contact: (contact.created_at, int(contact.id)),
            identity=lambda contact: int(contact.id),
        )

    async def update(self, contact: Contact) -> None:
        """Persist a changed contact."""
        self._require_own(contact, operation="update")
        existing = self._store.contacts.get(int(contact.id))
        if existing is None or existing.account_id != self._account_id:
            msg = f"No contact {int(contact.id)} in account {int(self._account_id)}"
            raise RecordNotFoundError(msg, user_message="That contact was not found.")
        # Identity and creation time belong to the original row, as in SQL.
        self._store.contacts[int(contact.id)] = replace(
            contact,
            telegram_user_id=existing.telegram_user_id,
            created_at=existing.created_at,
        )

    def _by_telegram_id(self, telegram_user_id: TelegramUserId) -> Contact | None:
        """Return this account's contact for a Telegram user, deleted or not."""
        for contact in self._store.contacts.values():
            if (
                contact.account_id == self._account_id
                and contact.telegram_user_id == telegram_user_id
            ):
                return contact
        return None

    def _require_own(self, contact: Contact, *, operation: str) -> None:
        """Refuse a contact belonging to a different account."""
        if contact.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a contact of account {int(contact.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg, user_message="That contact belongs to a different account."
            )
