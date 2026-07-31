"""Message repository port.

Scoped to one Account at construction (ADR-039).

**Append-only, expressed in the interface.** There is no ``update`` and no
``delete``. A Message is the immutable factual record everything else is derived
from, so there is no route by which a stored one can change -- the same
arrangement ``AuditRepository`` uses, and for the same reason: a guarantee that
exists only as a convention is a guarantee somebody eventually breaks.

``DOMAIN_MODEL.md`` section 5.6 names four eventual exceptions, all written by
synchronisation. Adding an update path now would settle by accident a question
that belongs to the code performing the edit -- whether an edited message mutates
its row or supersedes it.

Five operations, each traceable to a caller that exists:

* :meth:`add` -- ingestion.
* :meth:`get` -- ``message show``.
* :meth:`get_by_telegram_id` -- **the idempotency check**. This is what makes the
  pipeline re-runnable: an ingestion that has already happened is recognised
  rather than rejected by a constraint.
* :meth:`list_by_chat` -- the conversation history, newest first.
* :meth:`list_since` -- what conversation segmentation reads: one chat's
  messages from an instant onwards, **oldest first**. A separate method rather
  than an option on the one above, because it answers a different question and
  a boolean that reversed a query's meaning would be the kind of parameter every
  caller has to look up.

Still no ``update`` and no ``delete``. Segmentation might have been expected to
need one -- to write a ``conversation_id`` onto each message -- and does not:
membership is the conversation's time range, so nothing about a stored message
changes when it is segmented (ADR-056).

There is no ``search``: it is dialect-specific and belongs to
``MessageSearchPort`` (ADR-016 section 4). There is no ``list_by_account``
either -- messages are read per conversation, and a cross-chat query should
arrive with the feature that needs it so its index can be measured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    MessageId,
    TelegramMessageId,
)
from tgassist.domain.model.message import Message
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest


@runtime_checkable
class MessageRepository(Protocol):
    """Stores and retrieves the messages of one account."""

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def add(self, message: Message) -> None:
        """Persist a message.

        Raises:
            DomainValidationError: If the message belongs to another account.
            ConstraintViolationError: If the identifier is taken, if the chat
                does not belong to this account, or if this chat already holds a
                message with that Telegram identifier. Callers ingesting from a
                source with identifiers should ask :meth:`get_by_telegram_id`
                first: arriving at the constraint means the caller could not
                tell a repeat from a new message.
        """
        ...

    async def get(self, message_id: MessageId) -> Message | None:
        """Return one of this account's messages, or ``None`` if absent."""
        ...

    async def get_by_telegram_id(
        self, chat_id: ChatId, telegram_message_id: TelegramMessageId
    ) -> Message | None:
        """Return a message by its identifier in its chat, or ``None``.

        Takes the chat because a Telegram message identifier is unique only
        within one: the same number names a different message in every chat, so
        a lookup without the chat would be a lookup for the wrong thing.
        """
        ...

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Message]:
        """Return one page of a chat's messages, newest first by default.

        Ordered by ``sent_at`` rather than by ingestion order, because a
        backfill inserts old messages after new ones and history read in
        insertion order would be nonsense.
        """
        ...

    async def list_since(
        self, chat_id: ChatId, since: datetime | None = None, *, limit: int = 10_000
    ) -> tuple[Message, ...]:
        """Return a chat's messages from an instant onwards, oldest first.

        The read a segmentation pass begins with. Ordered ascending because that
        is the order boundaries are computed in, and returned whole rather than
        paged because the pass needs the window at once -- a boundary depends on
        the message before it, so a page split would need the caller to carry
        the previous page's tail.

        Args:
            chat_id: The chat to read.
            since: Include messages sent at or after this instant. ``None``
                reads the chat from its beginning, which is what a full rebuild
                asks for.
            limit: A ceiling on how many to return, so one call cannot load an
                unbounded chat into memory. A caller that hits it has asked for
                a window too wide to segment in one pass, and the count it gets
                back is what tells it so.
        """
        ...
