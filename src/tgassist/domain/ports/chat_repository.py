"""Chat repository port.

Scoped to one Account at construction (ADR-039), like every repository over
account-owned data.

Six operations, each traceable to a caller that exists:

* :meth:`add` -- ``chat add``.
* :meth:`get` -- ``chat show``, and the read before every change.
* :meth:`get_by_telegram_id` -- the duplicate check on creation, and the lookup
  synchronisation performs for every chat it encounters.
* :meth:`get_private_with` -- the graph query, "the chat I have with this
  person", and the check that a contact does not acquire two private chats.
* :meth:`list_chats` -- ``chat list``.
* :meth:`update` -- changing synchronisation or the AI processing mode.

There is no ``delete``. A chat is not something a user removes: it exists
because a conversation exists in Telegram. What they control is whether it is
synchronised and what may be done with its content, and both are ``update``.
Removal happens by cascade -- from the account, or from the contact purge in
``PRIVACY.md`` section 7, which Milestone 11 owns.

There is no ``list_syncable`` either, though synchronisation will obviously want
one. It would have no caller until then, and the index that serves it should be
chosen by the query that needs it rather than guessed a milestone early.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tgassist.domain.model.chat import Chat
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
)
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest


@runtime_checkable
class ChatRepository(Protocol):
    """Stores and retrieves the chats of one account.

    Satisfies the repository contract in ``domain/ports/repository.py`` and is
    verified against it by the shared contract suite.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def add(self, chat: Chat) -> None:
        """Persist a new chat.

        Raises:
            DomainValidationError: If the chat belongs to another account.
            ConstraintViolationError: If the identifier is taken, if this
                account already has a chat with this Telegram chat identifier,
                if the contact already has a private chat, or if the contact
                belongs to a different account.
        """
        ...

    async def get(self, chat_id: ChatId) -> Chat | None:
        """Return one of this account's chats, or ``None`` if absent."""
        ...

    async def get_by_telegram_id(self, telegram_chat_id: TelegramChatId) -> Chat | None:
        """Return this account's chat with a Telegram identifier, or ``None``."""
        ...

    async def get_private_with(self, contact_id: ContactId) -> Chat | None:
        """Return this account's private chat with a contact, or ``None``.

        The graph traversal in the direction the application actually reads it:
        from a person to the conversation with them. A contact has at most one
        private chat, enforced by a partial unique index, so this returns a
        single chat rather than a page.
        """
        ...

    async def list_chats(self, request: PageRequest) -> Page[Chat]:
        """Return one page of this account's chats, newest first by default."""
        ...

    async def update(self, chat: Chat) -> None:
        """Persist a changed chat.

        Takes the whole entity rather than a set of fields, so the invariants
        checked when it was constructed are the invariants written.

        Raises:
            DomainValidationError: If the chat belongs to another account.
            RecordNotFoundError: If no row matches.
        """
        ...
