"""Turning stored messages into conversations.

The segmentation *rule* is a pure function in the domain
(``domain/services/segmentation.py``). This module is what gives its output
**identity** and writes it down, and those two jobs are where every interesting
decision in this slice lives.

Identity is matched, not generated
----------------------------------

Re-segmenting a chat recomputes its boundaries from scratch. If each computed
segment were simply given a fresh identifier, a rebuild would replace every
conversation in the chat with an identical-looking new one -- and anything that
had referenced a conversation would be pointing at a row that no longer exists.

So a computed segment does not get a new conversation if a stored one already
describes it. Each segment claims **the stored conversation that owns the
plurality of its messages**, and a stored conversation can be claimed by at most
one segment -- the earliest, in order. Ties break to the lowest identifier.

That rule is short, deterministic, and gets the four interesting cases right:

* **Extension** (a live message continues the last conversation): every message
  the stored conversation owns is still in the segment, so the segment claims
  it and the row is updated rather than replaced.
* **A new episode** (a live message after a long silence): the segment contains
  no stored message, so nothing is claimed and a conversation is created. The
  earlier ones are untouched.
* **Merge** (backfill delivers a message that joins two episodes, or extends the
  oldest one backwards): the merged segment claims whichever stored
  conversation contributed more of it; the other is left owning nothing and is
  deleted.
* **Split** (a smaller configured gap divides one episode in two): the earlier
  segment claims the stored conversation, and the later one is created --
  because a stored conversation can only be claimed once.

Membership needs no writing
---------------------------

There is no step that links messages to conversations, and the transaction
therefore has one fewer thing to get wrong. A message belongs to the
conversation whose time range contains its ``sent_at``; conversations do not
overlap, so that is exact. It is also what keeps a rebuild cheap: fifty thousand
messages produce a few hundred conversation rows, and only those are written
(ADR-056).

Incremental by window
---------------------

A boundary depends only on the gap to the message before it, so inserting a
message can change nothing earlier than the conversation it lands in. A pass
therefore re-segments from the start of that conversation onwards and leaves
everything before it alone -- which is what makes ingestion able to trigger
segmentation on every batch without rebuilding the chat each time.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.domain.model.conversation import Conversation
from tgassist.domain.model.identifiers import AccountId, ChatId, ConversationId
from tgassist.domain.model.message import Message
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.conversation_repository import ConversationRepository
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.message_repository import MessageRepository
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory
from tgassist.domain.services.segmentation import Segment, SegmentationRules, segment


@dataclass(frozen=True, slots=True)
class SegmentationReport:
    """What one segmentation pass did.

    Attributes:
        chat_id: The chat that was segmented.
        created: Conversations that did not exist before.
        updated: Stored conversations whose extent changed.
        unchanged: Stored conversations the pass recomputed identically. The
            number that shows a rebuild is idempotent, and the one that should
            be everything after a second run.
        deleted: Stored conversations left describing no messages.
        messages: How many messages the pass read.
    """

    chat_id: ChatId
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    messages: int = 0

    @property
    def conversations(self) -> int:
        """How many conversations the window now holds."""
        return self.created + self.updated + self.unchanged

    @property
    def changed(self) -> bool:
        """Whether this pass wrote anything."""
        return bool(self.created or self.updated or self.deleted)


class SegmentConversations:
    """Divides a chat's stored messages into conversations, repeatably."""

    __slots__ = (
        "_accounts",
        "_clock",
        "_conversations",
        "_ids",
        "_messages",
        "_rules",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        conversations: ScopedRepositoryFactory[ConversationRepository],
        messages: ScopedRepositoryFactory[MessageRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
        rules: SegmentationRules | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory. One transaction per pass.
            conversations: Conversation repository factory, scoped per account.
            messages: Message repository factory, scoped per account.
            accounts: Account repository factory.
            clock: Time source, for the timestamps on rows this writes. **Not**
                for the rule: segmentation reads only ``sent_at``, so the same
                messages segment the same way whenever they are read.
            ids: Local identifier generator, for conversations no stored one
                matched.
            rules: What decides a boundary. Defaults to the documented gap and
                cap.
        """
        self._unit_of_work = unit_of_work
        self._conversations = conversations
        self._messages = messages
        self._accounts = accounts
        self._clock = clock
        self._ids = ids
        self._rules = rules if rules is not None else SegmentationRules()

    async def execute(
        self,
        chat_id: int,
        account_id: AccountId | None = None,
        *,
        since: datetime | None = None,
    ) -> SegmentationReport:
        """Segment a chat, in one transaction.

        Args:
            chat_id: The local chat to segment.
            account_id: Account to operate on. ``None`` selects the active one.
            since: Re-segment only from this instant onwards. The window is
                widened to the start of the conversation containing it, because
                a boundary depends on the message before it and a window that
                began mid-conversation would recompute a first gap it cannot
                see. ``None`` rebuilds the whole chat.

        Returns:
            What the pass did.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
            DomainValidationError: If a computed conversation is not one, which
                would mean the rule and the entity disagree.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            conversations = self._conversations(uow, resolved)
            chat = ChatId(chat_id)

            window = await self._window(conversations, chat, since)
            stored = await conversations.list_from(chat, window)
            found = await self._messages(uow, resolved).list_since(chat, window)

            report = await self._reconcile(
                conversations, resolved, chat, found, stored, self._clock.now()
            )
            if report.changed:
                await uow.commit()
            return report

    # -- The window --------------------------------------------------------

    async def _window(
        self,
        conversations: ConversationRepository,
        chat_id: ChatId,
        since: datetime | None,
    ) -> datetime | None:
        """Return the instant this pass re-segments from.

        Widened backwards to the start of the conversation that already contains
        ``since``. A window beginning mid-conversation would see its first
        message with nothing before it and would therefore always call it a
        boundary -- splitting an episode at whatever point the caller happened
        to name.

        ``None`` means the whole chat -- what a rebuild asks for, and also what a
        message older than everything segmented so far gets, because in that
        case there is nothing before it to preserve.
        """
        if since is None:
            return None
        containing = await conversations.latest_before(chat_id, since)
        return containing.started_at if containing is not None else None

    # -- Reconciliation ----------------------------------------------------

    async def _reconcile(  # noqa: PLR0913, PLR0917 - one argument per thing the pass needs
        self,
        conversations: ConversationRepository,
        account_id: AccountId,
        chat_id: ChatId,
        found: tuple[Message, ...],
        stored: tuple[Conversation, ...],
        now: datetime,
    ) -> SegmentationReport:
        """Compute segments and make the stored conversations agree with them."""
        segments = segment(found, self._rules)
        by_id = {conversation.id: conversation for conversation in stored}
        claimed: set[ConversationId] = set()

        created = updated = unchanged = 0
        for piece in segments:
            owner = _claimant(piece, by_id, claimed)
            if owner is None:
                await conversations.add(self._build(account_id, chat_id, piece, now))
                created += 1
                continue

            claimed.add(owner.id)
            revised = owner.spanning_now(
                started_at=piece.started_at,
                ended_at=piece.ended_at,
                message_count=piece.message_count,
                now=now,
            )
            if revised is owner:
                unchanged += 1
                continue
            await conversations.update(revised)
            updated += 1

        deleted = 0
        for conversation in stored:
            if conversation.id not in claimed:
                # Nothing in the recomputed window describes this run any more.
                await conversations.delete(conversation.id)
                deleted += 1

        return SegmentationReport(
            chat_id=chat_id,
            created=created,
            updated=updated,
            unchanged=unchanged,
            deleted=deleted,
            messages=len(found),
        )

    def _build(
        self, account_id: AccountId, chat_id: ChatId, piece: Segment, now: datetime
    ) -> Conversation:
        """Build a conversation for a segment nothing stored described."""
        return Conversation.spanning(
            conversation_id=ConversationId(self._ids.new_id()),
            account_id=account_id,
            chat_id=chat_id,
            started_at=piece.started_at,
            ended_at=piece.ended_at,
            message_count=piece.message_count,
            now=now,
        )


def _claimant(
    piece: Segment,
    by_id: dict[ConversationId, Conversation],
    claimed: set[ConversationId],
) -> Conversation | None:
    """Return the stored conversation this segment inherits, or ``None``.

    The one that owns the plurality of the segment's messages -- where "owns"
    means its stored time range contains the message. Ties break to the lowest
    identifier, and a conversation already claimed by an earlier segment is not
    available, which is what makes a split give the earlier half the existing
    identity and the later half a new one.

    Both tie-breaks exist to make the answer a function of the arguments alone.
    Without them a rebuild could produce different identities on two runs over
    the same data, which is the failure this whole matching rule exists to
    prevent.
    """
    votes: Counter[ConversationId] = Counter()
    for message in piece.messages:
        for conversation in by_id.values():
            if conversation.id not in claimed and conversation.contains(message.sent_at):
                votes[conversation.id] += 1
                break

    if not votes:
        return None
    best = max(votes.values())
    winner = min(identifier for identifier, count in votes.items() if count == best)
    return by_id[winner]


class GetConversation:
    """Looks a conversation up, with the messages it covers."""

    __slots__ = ("_accounts", "_conversations", "_messages", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        conversations: ScopedRepositoryFactory[ConversationRepository],
        messages: ScopedRepositoryFactory[MessageRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._conversations = conversations
        self._messages = messages
        self._accounts = accounts

    async def execute(
        self, conversation_id: int, *, account_id: AccountId | None = None
    ) -> tuple[Conversation, tuple[Message, ...]] | None:
        """Return a conversation and its messages, or ``None`` if absent.

        The messages are read by time range rather than by a stored link, which
        is the whole of what membership means here (ADR-056). They are filtered
        to the conversation's own end, because the range read returns everything
        from its start onwards.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            conversation = await self._conversations(uow, resolved).get(
                ConversationId(conversation_id)
            )
            if conversation is None:
                return None

            found = await self._messages(uow, resolved).list_since(
                conversation.chat_id, conversation.started_at
            )
            return conversation, tuple(m for m in found if conversation.contains(m.sent_at))


class ListConversations:
    """Returns a page of a chat's conversations."""

    __slots__ = ("_accounts", "_conversations", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        conversations: ScopedRepositoryFactory[ConversationRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._conversations = conversations
        self._accounts = accounts

    async def execute(
        self,
        chat_id: int,
        request: PageRequest | None = None,
        *,
        account_id: AccountId | None = None,
    ) -> Page[Conversation]:
        """Return one page of a chat's conversations, newest first."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._conversations(uow, resolved).list_by_chat(
                ChatId(chat_id), request or PageRequest()
            )


__all__ = [
    "GetConversation",
    "ListConversations",
    "SegmentConversations",
    "SegmentationReport",
]
