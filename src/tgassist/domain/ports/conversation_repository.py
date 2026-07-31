"""Conversation repository port.

Scoped to one Account at construction (ADR-039), like every repository over
account-owned data.

Six operations, each traceable to a caller that exists:

* :meth:`get` -- ``conversation show``.
* :meth:`list_by_chat` -- ``conversation list``.
* :meth:`list_from` -- the read a segmentation pass begins with, bounding the
  window it will rewrite.
* :meth:`latest_before` -- what tells that pass where its window starts: the
  conversation an arriving message lands in. A boundary depends on the message
  before it, so a window opening mid-conversation would recompute a first gap it
  cannot see and split an episode at whatever instant the caller named.
* :meth:`add` -- a segment no stored conversation matched.
* :meth:`update` -- a stored conversation whose extent changed.
* :meth:`delete` -- a stored conversation the new segmentation left with no
  messages.

**There is a ``delete`` here, unlike on every other repository in this system**,
and the difference is not an inconsistency. A Message, a Contact and a Chat each
record something somebody decided, so removing one destroys information nothing
else holds. A Conversation records something *this application computed* from
messages that are still there. Deleting a stale one loses nothing, and refusing
to would leave rows describing runs of messages that no longer exist.

That freedom ends the moment something references a Conversation. Summaries,
plans and analyses arrive in Milestone 8 and must cascade, or this delete has to
start refusing; ``DATABASE.md`` records the requirement on the table rather than
leaving it to be discovered.

There is no ``get_open``. Whether a conversation may still grow depends on how
long ago it ended -- on *now* -- so it is a question asked of an entity with an
instant, not a column to query (ADR-056). "The newest conversation in this chat"
is the first page of :meth:`list_by_chat`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import AccountId, ChatId, ConversationId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest


@runtime_checkable
class ConversationRepository(Protocol):
    """Stores and retrieves the conversations of one account.

    Satisfies the repository contract in ``domain/ports/repository.py`` and is
    verified against it by the shared contract suite: absence returns ``None``
    rather than raising, the repository never commits, and results are domain
    objects rather than rows.
    """

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        ...

    async def get(self, conversation_id: ConversationId) -> Conversation | None:
        """Return one of this account's conversations, or ``None`` if absent."""
        ...

    async def list_by_chat(self, chat_id: ChatId, request: PageRequest) -> Page[Conversation]:
        """Return one page of a chat's conversations, newest first by default.

        Ordered by ``started_at`` rather than by creation order: a rebuild after
        a backfill creates old conversations after new ones, and a listing in
        insertion order would be nonsense in exactly the way a chat's message
        history would be.
        """
        ...

    async def list_from(
        self, chat_id: ChatId, started_at: datetime | None = None
    ) -> tuple[Conversation, ...]:
        """Return a chat's conversations beginning at or after an instant, in order.

        The window a segmentation pass is about to rewrite. It is a tuple rather
        than a page because the pass needs all of them at once -- it matches
        computed segments against them -- and because that window is bounded by
        how much new history arrived rather than by how long the chat is.

        ``None`` returns every conversation in the chat, which is what a full
        rebuild asks for.
        """
        ...

    async def latest_before(self, chat_id: ChatId, instant: datetime) -> Conversation | None:
        """Return the last conversation beginning at or before an instant.

        The one a message sent then would fall in, or the one immediately
        before it if the message opens a new episode. ``None`` when the chat has
        no conversation that early -- which means the arriving message is older
        than everything segmented so far, and the window is the whole chat.
        """
        ...

    async def add(self, conversation: Conversation) -> None:
        """Persist a new conversation.

        Raises:
            DomainValidationError: If it belongs to another account.
            ConstraintViolationError: If the identifier is taken, if the chat
                does not belong to this account, or if the chat already has a
                conversation starting at that instant -- which would mean two
                conversations overlapping, the one thing the model says cannot
                happen.
        """
        ...

    async def update(self, conversation: Conversation) -> None:
        """Persist a conversation whose extent changed.

        Takes the whole entity rather than a set of fields, so the invariants
        checked when it was constructed are the invariants written.

        Raises:
            DomainValidationError: If it belongs to another account.
            RecordNotFoundError: If no row matches.
        """
        ...

    async def delete(self, conversation_id: ConversationId) -> None:
        """Remove a conversation that no longer describes any messages.

        Deleting one that is absent is not an error: a pass that computed the
        same removal twice has made no mistake, and raising would make every
        caller wrap the ordinary case.
        """
        ...
