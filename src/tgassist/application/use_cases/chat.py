"""Chat use cases.

The first use cases to compose **two** account-scoped repositories in one
transaction: creating a private chat has to establish that the contact exists
and belongs to the same account. That is what the unit of work and the scoped
factories were built for, and it is worth noticing that the ownership check
costs nothing here -- the contact repository is scoped to the same account, so a
contact belonging to somebody else simply is not found.

The database enforces the same rule independently, through a composite foreign
key on ``(account_id, contact_id)`` (ADR-043). The application check exists to
produce a message naming the problem; the constraint exists so that a route
which skips the check cannot corrupt the graph.
"""

from __future__ import annotations

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.domain.errors import ConflictError, RecordNotFoundError
from tgassist.domain.model.chat import AiProcessingMode, Chat, ChatType
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.contact_repository import ContactRepository
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory


class _ScopedUseCase:
    """Shared plumbing for use cases operating on one account's chats."""

    __slots__ = ("_accounts", "_chats", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators every chat use case needs."""
        self._unit_of_work = unit_of_work
        self._chats = chats
        self._accounts = accounts

    async def _scoped(self, uow: UnitOfWork, account_id: AccountId | None) -> ChatRepository:
        """Return a repository scoped to the resolved account."""
        resolved = await resolve_account(self._accounts(uow), account_id)
        return self._chats(uow, resolved)


class OpenPrivateChat(_ScopedUseCase):
    """Records the conversation an account has with one contact."""

    __slots__ = ("_clock", "_contacts", "_ids")

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        chats: ScopedRepositoryFactory[ChatRepository],
        contacts: ScopedRepositoryFactory[ContactRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        super().__init__(unit_of_work, chats, accounts)
        self._contacts = contacts
        self._clock = clock
        self._ids = ids

    async def execute(
        self,
        *,
        contact_id: int,
        telegram_chat_id: int,
        account_id: AccountId | None = None,
        sync_enabled: bool = True,
        ai_processing_mode: AiProcessingMode = AiProcessingMode.LOCAL_ONLY,
    ) -> Chat:
        """Create the private chat with a contact.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such contact. A contact belonging to another
                account produces this same error, because the scoped repository
                genuinely cannot see it -- the ownership rule is not a check
                that could be skipped.
            ConflictError: If this contact already has a private chat, or if
                this account already has a chat with that Telegram identifier.
            DomainValidationError: If a value violates a Chat invariant.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            chats = self._chats(uow, resolved)
            contacts = self._contacts(uow, resolved)

            contact = await contacts.get(ContactId(contact_id))
            if contact is None:
                msg = f"No contact {contact_id} in account {int(resolved)}"
                raise RecordNotFoundError(
                    msg,
                    user_message="That contact was not found.",
                    context={"contact_id": contact_id, "account_id": int(resolved)},
                )

            existing = await chats.get_private_with(contact.id)
            if existing is not None:
                msg = f"Contact {contact_id} already has private chat {int(existing.id)}"
                raise ConflictError(
                    msg,
                    user_message=f"That contact already has a chat ({int(existing.id)}).",
                    context={"contact_id": contact_id, "chat_id": int(existing.id)},
                )

            clash = await chats.get_by_telegram_id(TelegramChatId(telegram_chat_id))
            if clash is not None:
                msg = f"Telegram chat {telegram_chat_id} is already chat {int(clash.id)}"
                raise ConflictError(
                    msg,
                    user_message=f"That Telegram chat is already recorded ({int(clash.id)}).",
                    context={"telegram_chat_id": telegram_chat_id, "chat_id": int(clash.id)},
                )

            chat = Chat.private_with(
                chat_id=ChatId(self._ids.new_id()),
                account_id=resolved,
                telegram_chat_id=TelegramChatId(telegram_chat_id),
                contact_id=contact.id,
                now=self._clock.now(),
                sync_enabled=sync_enabled,
                ai_processing_mode=ai_processing_mode,
            )
            await chats.add(chat)
            await uow.commit()

        return chat


class OpenGroupChat(_ScopedUseCase):
    """Records a chat that has a title rather than a single counterpart."""

    __slots__ = ("_clock", "_ids")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        super().__init__(unit_of_work, chats, accounts)
        self._clock = clock
        self._ids = ids

    async def execute(  # noqa: PLR0913 - one option per settable field
        self,
        *,
        telegram_chat_id: int,
        title: str,
        chat_type: ChatType = ChatType.GROUP,
        account_id: AccountId | None = None,
        sync_enabled: bool = True,
        ai_processing_mode: AiProcessingMode = AiProcessingMode.LOCAL_ONLY,
    ) -> Chat:
        """Create a group, supergroup, channel or saved-messages chat.

        Separate from :class:`OpenPrivateChat` because the two need different
        arguments and answer different questions: a private chat has to resolve
        a contact and cannot have a title, and the reverse holds here. One use
        case taking both shapes would validate half its parameters as unusable
        on every call.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
            ConflictError: If this account already has that Telegram chat.
            DomainValidationError: If ``chat_type`` is private, or the title is
                empty.
        """
        async with self._unit_of_work() as uow:
            chats = await self._scoped(uow, account_id)

            clash = await chats.get_by_telegram_id(TelegramChatId(telegram_chat_id))
            if clash is not None:
                msg = f"Telegram chat {telegram_chat_id} is already chat {int(clash.id)}"
                raise ConflictError(
                    msg,
                    user_message=f"That Telegram chat is already recorded ({int(clash.id)}).",
                    context={"telegram_chat_id": telegram_chat_id, "chat_id": int(clash.id)},
                )

            chat = Chat.group_titled(
                chat_id=ChatId(self._ids.new_id()),
                account_id=chats.account_id,
                telegram_chat_id=TelegramChatId(telegram_chat_id),
                chat_type=chat_type,
                title=title,
                now=self._clock.now(),
                sync_enabled=sync_enabled,
                ai_processing_mode=ai_processing_mode,
            )
            await chats.add(chat)
            await uow.commit()

        return chat


class GetChat(_ScopedUseCase):
    """Looks a chat up by identifier, or by the contact it is with."""

    __slots__ = ()

    async def execute(self, chat_id: int, *, account_id: AccountId | None = None) -> Chat | None:
        """Return a chat, or ``None`` if this account has no such chat."""
        async with self._unit_of_work() as uow:
            chats = await self._scoped(uow, account_id)
            return await chats.get(ChatId(chat_id))

    async def with_contact(
        self, contact_id: int, *, account_id: AccountId | None = None
    ) -> Chat | None:
        """Return the private chat with a contact, or ``None``.

        The graph traversal in the direction the application reads it: from a
        person to the conversation with them.
        """
        async with self._unit_of_work() as uow:
            chats = await self._scoped(uow, account_id)
            return await chats.get_private_with(ContactId(contact_id))


class ListChats(_ScopedUseCase):
    """Returns a page of this account's chats."""

    __slots__ = ()

    async def execute(
        self, request: PageRequest | None = None, *, account_id: AccountId | None = None
    ) -> Page[Chat]:
        """Return one page of chats, newest first."""
        async with self._unit_of_work() as uow:
            chats = await self._scoped(uow, account_id)
            return await chats.list_chats(request or PageRequest())


class SetChatPolicy(_ScopedUseCase):
    """Changes what may be done with a chat.

    One use case for both settings because they are one decision from the user's
    side -- how much this application should involve itself in a conversation --
    and because they are one transaction and one write.
    """

    __slots__ = ("_clock",)

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        super().__init__(unit_of_work, chats, accounts)
        self._clock = clock

    async def execute(
        self,
        chat_id: int,
        *,
        sync_enabled: bool | None = None,
        ai_processing_mode: AiProcessingMode | None = None,
        account_id: AccountId | None = None,
    ) -> Chat:
        """Apply the requested policy changes and return the resulting chat.

        ``None`` means "leave as it is", which is distinct from either value
        ``sync_enabled`` can take. Changing nothing is permitted and commits
        nothing.

        Raises:
            RecordNotFoundError: If no account matches, or no such chat.
        """
        async with self._unit_of_work() as uow:
            chats = await self._scoped(uow, account_id)
            chat = await chats.get(ChatId(chat_id))
            if chat is None:
                msg = f"No chat {chat_id} in account {int(chats.account_id)}"
                raise RecordNotFoundError(
                    msg,
                    user_message="That chat was not found.",
                    context={"chat_id": chat_id, "account_id": int(chats.account_id)},
                )

            now = self._clock.now()
            changed = chat
            if sync_enabled is not None:
                changed = changed.with_sync_enabled(sync_enabled, now)
            if ai_processing_mode is not None:
                changed = changed.with_ai_processing_mode(ai_processing_mode, now)

            if changed is chat:
                return chat

            await chats.update(changed)
            await uow.commit()

        return changed


__all__ = [
    "GetChat",
    "ListChats",
    "OpenGroupChat",
    "OpenPrivateChat",
    "SetChatPolicy",
]
