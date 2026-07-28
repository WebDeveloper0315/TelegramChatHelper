"""Contact mapper and repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, insert, select, update

from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.model.contact import Contact
from tgassist.domain.model.identifiers import AccountId, ContactId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.contact_repository import ContactRepository
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import contacts
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork

SORT_FIELD = "created_at"


class ContactMapper(EntityMapper[Contact]):
    """Converts between :class:`Contact` and its row.

    Both directions are total: every column the table declares is written by
    :meth:`to_params`, asserted by a column-coverage test that fails the moment
    a migration adds a column this mapper does not know about.
    """

    def to_domain(self, row: Any) -> Contact:
        """Build a contact from a row."""
        created_at = from_stored_datetime(_as_iso(row.created_at))
        updated_at = from_stored_datetime(_as_iso(row.updated_at))
        if created_at is None or updated_at is None:  # pragma: no cover - schema forbids
            msg = "A contact row is missing its timestamps"
            raise DomainValidationError(msg, user_message="That contact is incomplete.")

        return Contact(
            id=ContactId(row.id),
            account_id=AccountId(row.account_id),
            telegram_user_id=TelegramUserId(row.telegram_user_id),
            username=row.username,
            display_name=row.display_name,
            archived_at=from_stored_datetime(_as_iso(row.archived_at)),
            deleted_at=from_stored_datetime(_as_iso(row.deleted_at)),
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_params(self, entity: Contact) -> dict[str, Any]:
        """Build column values from a contact."""
        return {
            "id": int(entity.id),
            "account_id": int(entity.account_id),
            "telegram_user_id": int(entity.telegram_user_id),
            "username": entity.username,
            "display_name": entity.display_name,
            "archived_at": entity.archived_at,
            "deleted_at": entity.deleted_at,
            "created_at": entity.created_at,
            "updated_at": entity.updated_at,
        }


def _as_iso(value: Any) -> str | None:
    """Render a stored timestamp as ISO text, whichever form the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class SqlContactRepository(Repository[Contact], ContactRepository):
    """Stores the contacts of one account.

    Scoped at construction. No method takes an account identifier, so every
    query below is filtered by the account this repository was built for and
    there is no path that forgets to be (ADR-039).
    """

    __slots__ = ("_account_id", "_mapper", "_paginator")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = ContactMapper()
        self._paginator = KeysetPaginator(
            sort_column=contacts.c.created_at,
            tiebreak_column=contacts.c.id,
            sort_field=SORT_FIELD,
        )

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    def _scoped(self) -> Select[Any]:
        """Return the base query, already filtered to this account.

        Every read starts here. A read that did not would be the defect this
        design exists to prevent, so there is deliberately no unscoped variant
        to reach for.
        """
        return select(contacts).where(contacts.c.account_id == int(self._account_id))

    async def add(self, contact: Contact) -> None:
        """Persist a new contact."""
        self._require_own(contact, operation="add")
        await self.execute_write(
            insert(contacts).values(self._mapper.to_params(contact)),
            operation="add_contact",
            conflict_message="This account already knows that Telegram user.",
        )

    async def get(self, contact_id: ContactId, *, include_deleted: bool = False) -> Contact | None:
        """Return one of this account's contacts, or ``None`` if absent."""
        statement = self._scoped().where(contacts.c.id == int(contact_id))
        if not include_deleted:
            statement = statement.where(contacts.c.deleted_at.is_(None))

        row = await self.fetch_one(statement, operation="get_contact")
        return self._mapper.to_domain(row) if row is not None else None

    async def get_by_telegram_id(
        self, telegram_user_id: TelegramUserId, *, include_deleted: bool = False
    ) -> Contact | None:
        """Return this account's contact for a Telegram user, or ``None``."""
        statement = self._scoped().where(
            contacts.c.telegram_user_id == int(telegram_user_id),
        )
        if not include_deleted:
            statement = statement.where(contacts.c.deleted_at.is_(None))

        row = await self.fetch_one(statement, operation="get_contact_by_telegram_id")
        return self._mapper.to_domain(row) if row is not None else None

    async def list_contacts(
        self, request: PageRequest, *, include_archived: bool = False
    ) -> Page[Contact]:
        """Return one page of this account's contacts."""
        statement = self._scoped().where(contacts.c.deleted_at.is_(None))
        if not include_archived:
            statement = statement.where(contacts.c.archived_at.is_(None))

        return await self.fetch_page(
            statement,
            paginator=self._paginator,
            request=request,
            mapper=self._mapper.to_domain,
            operation="list_contacts",
        )

    async def update(self, contact: Contact) -> None:
        """Persist a changed contact.

        Raises:
            RecordNotFoundError: If no row matches.
        """
        self._require_own(contact, operation="update")
        params = self._mapper.to_params(contact)
        # Identity and creation time belong to the original row. Rewriting
        # created_at would make a contact appear to have been added when it was
        # last edited; rewriting the identifiers would move the row to another
        # person, or another account.
        for immutable in ("id", "account_id", "telegram_user_id", "created_at"):
            params.pop(immutable, None)

        result = await self.execute_write(
            update(contacts).where(self._owns(contact.id)).values(**params),
            operation="update_contact",
        )
        if result.rowcount == 0:
            msg = f"No contact {int(contact.id)} in account {int(self._account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That contact was not found.",
                context={"contact_id": int(contact.id), "account_id": int(self._account_id)},
            )

    def _owns(self, contact_id: ContactId) -> ColumnElement[bool]:
        """Return the predicate matching one of *this account's* rows.

        The account clause is not redundant with the primary key. Without it a
        caller holding another account's identifier would update that account's
        row, which is precisely the failure the scope exists to prevent.
        """
        return (contacts.c.id == int(contact_id)) & (contacts.c.account_id == int(self._account_id))

    def _require_own(self, contact: Contact, *, operation: str) -> None:
        """Refuse a contact belonging to a different account.

        The scope makes cross-account reads impossible, but a caller could still
        hand this repository an entity built for another account. On ``add``
        that would file somebody under the wrong owner; on ``update`` the row
        predicate would match nothing and the caller would get a confusing
        "not found" instead of the truth.
        """
        if contact.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a contact of account {int(contact.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That contact belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "contact_account": int(contact.account_id),
                },
            )


def contact_repository(uow: UnitOfWork, account_id: AccountId) -> SqlContactRepository:
    """Build a contact repository scoped to one account.

    Matches ``ScopedRepositoryFactory``, so a use case declares it as a
    dependency and supplies the account once, inside its transaction.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlContactRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlContactRepository(uow, account_id)


__all__ = [
    "ContactMapper",
    "SqlContactRepository",
    "contact_repository",
]
