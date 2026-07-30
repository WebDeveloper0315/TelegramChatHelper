"""Contact use cases.

Four operations. Three of them -- archive, restore and delete -- are one use
case parameterised by the transition, because they are genuinely the same
transaction: resolve the account, read the contact, ask the entity for its new
state, write it back. Three near-identical classes would have differed only in
which entity method they called, and the duplication would have been the kind
that drifts.

That is not a generic service. ``ChangeContactStatus`` does one thing, and the
enumeration names the three values its one argument can take.
"""

from __future__ import annotations

from enum import StrEnum

from tgassist.application.use_cases.account_scope import require_account, resolve_account
from tgassist.domain.errors import ConflictError, RecordNotFoundError
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import AccountId, ContactId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.contact_repository import ContactRepository
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tgassist.domain.services.operator_identity import require_not_operator


class ContactTransition(StrEnum):
    """A lifecycle move a caller can ask for."""

    ARCHIVE = "archive"
    RESTORE = "restore"
    DELETE = "delete"


class _ScopedUseCase:
    """Shared plumbing for use cases operating on one account's contacts.

    Inheritance for code reuse, not polymorphism: nothing holds one of these and
    calls it generically. What it removes is four copies of "open a transaction,
    resolve the account, build a scoped repository", which is exactly the
    sequence where forgetting the scope would be a data-leak defect.
    """

    __slots__ = ("_accounts", "_contacts", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        contacts: ScopedRepositoryFactory[ContactRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators every contact use case needs."""
        self._unit_of_work = unit_of_work
        self._contacts = contacts
        self._accounts = accounts

    async def _scoped(self, uow: UnitOfWork, account_id: AccountId | None) -> ContactRepository:
        """Return a repository scoped to the resolved account."""
        resolved = await resolve_account(self._accounts(uow), account_id)
        return self._contacts(uow, resolved)


class CreateContact(_ScopedUseCase):
    """Records a person this account knows."""

    __slots__ = ("_clock", "_ids")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        contacts: ScopedRepositoryFactory[ContactRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        super().__init__(unit_of_work, contacts, accounts)
        self._clock = clock
        self._ids = ids

    async def execute(
        self,
        *,
        telegram_user_id: int,
        display_name: str,
        username: str | None = None,
        account_id: AccountId | None = None,
    ) -> Contact:
        """Create and persist a contact.

        Raises:
            ConflictError: If this account already knows this Telegram user.
                Checked before writing so the message names the conflict rather
                than a column -- and the check includes deleted contacts,
                because a soft-deleted row still holds the natural key. Telling
                somebody "already exists" when the row is invisible to them
                would be useless, so the message says it was deleted and can be
                restored.
            RecordNotFoundError: If no account matches, or none is active.
            DomainValidationError: If a value violates a Contact invariant, or
                if the Telegram user is the account's own operator identity
                (ADR-052).
        """
        async with self._unit_of_work() as uow:
            account = await require_account(self._accounts(uow), account_id)
            contacts = self._contacts(uow, account.id)
            telegram_id = TelegramUserId(telegram_user_id)

            require_not_operator(account, telegram_id)

            existing = await contacts.get_by_telegram_id(telegram_id, include_deleted=True)
            if existing is not None:
                raise _already_known(existing)

            contact = Contact.create(
                contact_id=ContactId(self._ids.new_id()),
                account_id=contacts.account_id,
                telegram_user_id=telegram_id,
                display_name=display_name,
                username=username,
                now=self._clock.now(),
            )
            await contacts.add(contact)
            await uow.commit()

        return contact


class GetContact(_ScopedUseCase):
    """Looks a contact up by identifier."""

    __slots__ = ()

    async def execute(
        self,
        contact_id: int,
        *,
        account_id: AccountId | None = None,
        include_deleted: bool = False,
    ) -> Contact | None:
        """Return a contact, or ``None`` if this account has no such contact.

        Raises:
            RecordNotFoundError: If no account matches, or none is active. The
                account not existing is a different failure from the contact not
                existing, and conflating them would tell the caller the wrong
                thing.
        """
        async with self._unit_of_work() as uow:
            contacts = await self._scoped(uow, account_id)
            return await contacts.get(ContactId(contact_id), include_deleted=include_deleted)


class ListContacts(_ScopedUseCase):
    """Returns a page of this account's contacts."""

    __slots__ = ()

    async def execute(
        self,
        request: PageRequest | None = None,
        *,
        account_id: AccountId | None = None,
        include_archived: bool = False,
    ) -> Page[Contact]:
        """Return one page of contacts, newest first."""
        async with self._unit_of_work() as uow:
            contacts = await self._scoped(uow, account_id)
            return await contacts.list_contacts(
                request or PageRequest(), include_archived=include_archived
            )


class ChangeContactStatus(_ScopedUseCase):
    """Archives, restores or deletes a contact."""

    __slots__ = ("_clock",)

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        contacts: ScopedRepositoryFactory[ContactRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        super().__init__(unit_of_work, contacts, accounts)
        self._clock = clock

    async def execute(
        self,
        contact_id: int,
        transition: ContactTransition,
        *,
        account_id: AccountId | None = None,
    ) -> Contact:
        """Apply a lifecycle transition and return the resulting contact.

        A transition that changes nothing -- archiving an archived contact --
        commits nothing and returns the contact unchanged, because the caller
        has asked for a state the contact is already in and has made no mistake.

        Raises:
            RecordNotFoundError: If no account matches, or no such contact.
            DomainValidationError: If the transition is not legal from the
                contact's current state, such as archiving a deleted contact.
        """
        async with self._unit_of_work() as uow:
            contacts = await self._scoped(uow, account_id)
            # Deleted contacts are included for every transition, not only for
            # restore. An earlier version excluded them except when restoring,
            # which meant archiving a deleted contact reported "not found" -- a
            # lie, and one that hid the entity's own rule about the illegal
            # transition. Whether a transition is legal is the entity's
            # judgement to make, so the lookup must not pre-empt it by hiding
            # the subject.
            contact = await _require_contact(contacts, contact_id, include_deleted=True)

            changed = self._apply(contact, transition)
            if changed is contact:
                return contact

            await contacts.update(changed)
            await uow.commit()

        return changed

    def _apply(self, contact: Contact, transition: ContactTransition) -> Contact:
        """Ask the entity for its new state.

        The rules -- what may follow what -- belong to the entity, so this only
        chooses which question to ask.
        """
        now = self._clock.now()
        match transition:
            case ContactTransition.ARCHIVE:
                return contact.archived(now)
            case ContactTransition.RESTORE:
                return contact.restored(now)
            case ContactTransition.DELETE:
                return contact.deleted(now)


async def _require_contact(
    contacts: ContactRepository, contact_id: int, *, include_deleted: bool
) -> Contact:
    """Return a contact, raising if this account has no such contact."""
    contact = await contacts.get(ContactId(contact_id), include_deleted=include_deleted)
    if contact is None:
        msg = f"No contact {contact_id} in account {int(contacts.account_id)}"
        raise RecordNotFoundError(
            msg,
            user_message="That contact was not found.",
            context={"contact_id": contact_id, "account_id": int(contacts.account_id)},
        )
    return contact


def _already_known(existing: Contact) -> ConflictError:
    """Build the error reporting that this account already knows somebody."""
    msg = (
        f"Account {int(existing.account_id)} already knows Telegram user "
        f"{int(existing.telegram_user_id)} as contact {int(existing.id)}"
    )
    user_message = (
        f"That Telegram user was already added and then deleted "
        f"(contact {int(existing.id)}). Restore it instead."
        if existing.is_deleted
        else f"That Telegram user is already a contact ({int(existing.id)})."
    )
    return ConflictError(
        msg,
        user_message=user_message,
        context={
            "contact_id": int(existing.id),
            "telegram_user_id": int(existing.telegram_user_id),
            "deleted": existing.is_deleted,
        },
    )


__all__ = [
    "ChangeContactStatus",
    "ContactTransition",
    "CreateContact",
    "GetContact",
    "ListContacts",
]
