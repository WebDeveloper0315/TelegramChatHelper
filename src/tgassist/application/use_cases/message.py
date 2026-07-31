"""Message ingestion.

The pipeline every future source feeds: the CLI today, Telegram synchronisation
in Milestone 3, import tools and tests thereafter.

Source-agnostic, and what that does and does not mean
-----------------------------------------------------

It means the pipeline has no Telegram vocabulary beyond an **optional** external
identifier. A caller describes what arrived -- who sent it, when, what it says --
and the pipeline stores it. Nothing in this module knows how the message got
here.

It does **not** mean there is a ``MessageSource`` port. There is one source, and
a protocol with one implementation is an interface designed against a guess.
When synchronisation arrives it will construct :class:`IncomingMessage` values
exactly as the CLI does; if a third source then shows a shape neither fits, the
abstraction can be extracted from two real examples rather than imagined from
none.

Idempotency
-----------

Re-running an ingestion must be safe: synchronisation retries, and a backfill
overlaps what live updates have already delivered. A message carrying an
external identifier is therefore looked up before it is written, and a repeat is
**reported as skipped rather than raised as a conflict** -- an error would make
every caller wrap the ordinary case in a try/except.

The same holds *within* one batch. Nothing is written until the batch has been
built, so the repository cannot answer for an identifier the batch itself has
already claimed; the identifiers seen so far are therefore tracked as the batch
is assembled. Without that, a source offering the same message twice in one call
would build two rows and the second would meet the unique index -- an error
raised over exactly the case this pipeline promises to absorb.

A message without an external identifier has nothing to deduplicate against, so
every ingestion of it stores a new message. That is correct rather than a gap:
two identical messages typed at a keyboard are two messages (ADR-045).

Announcing
----------

**Whoever commits, announces.** :meth:`IngestMessages.execute` owns its
transaction, so it publishes ``MessagesIngested`` once the batch is in;
:meth:`IngestMessages.ingest_within` leaves the commit to its caller, so the
caller publishes. Without that rule the two paths would either both announce --
producing an event for a transaction that had not committed -- or, as they did
until conversation segmentation first subscribed, one of them would quietly not.

Batches
-------

:meth:`IngestMessages.execute` takes a sequence and one transaction. A single
message is the degenerate case. The shape is chosen because the report -- how
many stored, how many already present -- is only meaningful across a batch, and
because a per-message transaction would make a backfill of a hundred thousand
messages a hundred thousand commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.domain.errors import RecordNotFoundError
from tgassist.domain.events import MessagesIngested
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    MessageId,
    TelegramMessageId,
)
from tgassist.domain.model.message import Message, MessageType, SenderKind
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.message_repository import MessageRepository
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One message as a source describes it.

    Deliberately not a :class:`Message`. A source knows what arrived; it does not
    know what local identifier the message will be given, or when this
    application stored it. Keeping the two types apart is what stops a caller
    inventing either.

    Attributes:
        sender_kind: Who sent it.
        sent_at: When it was sent, as the source reports, UTC.
        text: The content, or ``None`` for a message that carries none.
        message_type: What kind of message it is.
        telegram_message_id: Its identifier in its source, if the source issues
            them. Its presence is what makes re-ingestion recognisable.
    """

    sender_kind: SenderKind
    sent_at: datetime
    text: str | None = None
    message_type: MessageType = MessageType.TEXT
    telegram_message_id: int | None = None


#: What a direct ingestion calls itself on the event it publishes. Distinct
#: from ``backfill``, ``catch_up`` and ``live`` because a subscriber may
#: reasonably treat a hand-written message differently from fifty thousand
#: back-filled ones.
SOURCE_MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class IngestionReport:
    """What one ingestion run did.

    Counts rather than a boolean, because the interesting question after a
    synchronisation run is how much of it was new.

    Attributes:
        stored: Messages written.
        skipped: Messages already present, recognised by their external
            identifier.
        message_ids: Identifiers of the messages written, in the order given.
    """

    stored: int = 0
    skipped: int = 0
    message_ids: tuple[MessageId, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        """How many messages were offered."""
        return self.stored + self.skipped

    @property
    def changed(self) -> bool:
        """Whether anything was actually written."""
        return self.stored > 0


class IngestMessages:
    """Stores messages arriving from any source, once each."""

    __slots__ = (
        "_accounts",
        "_chats",
        "_clock",
        "_events",
        "_ids",
        "_messages",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        messages: ScopedRepositoryFactory[MessageRepository],
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
        ids: IdGenerator,
        events: EventBus | None = None,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory.
            messages: Message repository factory, scoped per account.
            chats: Chat repository factory, scoped per account.
            accounts: Account repository factory.
            clock: Time source.
            ids: Local identifier generator.
            events: Where ``MessagesIngested`` is published once a batch this
                use case committed is in. Optional because
                :meth:`ingest_within` needs none -- its caller owns the commit
                and therefore the announcement.
        """
        self._unit_of_work = unit_of_work
        self._messages = messages
        self._chats = chats
        self._accounts = accounts
        self._clock = clock
        self._ids = ids
        self._events = events

    async def execute(
        self,
        chat_id: int,
        incoming: Sequence[IncomingMessage],
        *,
        account_id: AccountId | None = None,
    ) -> IngestionReport:
        """Ingest a batch into one chat, in one transaction.

        The whole batch commits or none of it does. A partial ingestion would
        leave a synchronisation run unable to say where it got to, which is the
        problem the batch boundary exists to prevent.

        An empty batch is permitted and commits nothing: a synchronisation run
        that found no new messages has not failed.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such chat. A chat belonging to another account
                produces the same error, because the scoped repository cannot
                see it.
            DomainValidationError: If a message violates an invariant. The
                transaction rolls back, so a batch containing one bad message
                stores none of it -- better than a partial ingestion whose
                extent nobody can determine.
        """
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            chat = await self._require_chat(uow, resolved, chat_id)
            report = await self.ingest_within(uow, resolved, chat, incoming)
            if report.changed:
                await uow.commit()

        if report.changed and self._events is not None:
            # After the commit, never inside it: a handler observing a fact that
            # then rolled back would be acting on something that never happened.
            stored = [item for item in incoming if item.telegram_message_id is not None] or list(
                incoming
            )
            await self._events.publish(
                MessagesIngested(
                    account_id=int(resolved),
                    chat_id=int(chat.id),
                    count=report.stored,
                    oldest_sent_at=min(item.sent_at for item in stored),
                    newest_sent_at=max(item.sent_at for item in stored),
                    source=SOURCE_MANUAL,
                )
            )
        return report

    async def ingest_within(
        self,
        uow: UnitOfWork,
        account_id: AccountId,
        chat: Chat,
        incoming: Sequence[IncomingMessage],
    ) -> IngestionReport:
        """Ingest a batch into an **already open** transaction, without committing.

        The body :meth:`execute` wraps, exposed because a backfill needs its
        messages and its cursor to move together: a cursor advanced in a
        different transaction could commit while the messages did not, and the
        next run would resume past messages nobody stored (ADR-050).

        The caller owns the transaction and therefore the commit. Nothing here
        commits, rolls back or opens anything, which is what lets the caller add
        its own writes to the same unit.

        Args:
            uow: An open transaction the caller will commit or discard.
            account_id: The account being written to, already resolved.
            chat: The chat being written into, already read from this account
                scoped repository, so it cannot belong to another account.
            incoming: What to store.

        Returns:
            What will have been written once the caller commits.

        Raises:
            DomainValidationError: If a message violates an invariant.
        """
        repository = self._messages(uow, account_id)
        ingested_at = self._clock.now()
        to_store: list[Message] = []
        # Identifiers this batch has already claimed. The repository cannot
        # answer for them: nothing is written until the loop ends, so a batch
        # naming the same message twice would build two rows and the second
        # would hit the unique index. Recognising a repeat *within* a batch is
        # the same guarantee as recognising one across runs, and a caller
        # should not have to know which kind it has.
        claimed: set[int] = set()
        skipped = 0

        # Build and validate the whole batch before writing any of it. The
        # transaction would roll back a partial write anyway, but failing
        # before the first insert makes the guarantee independent of the
        # store: a batch containing one malformed message is refused whole,
        # whatever it is being written into.
        for item in incoming:
            if item.telegram_message_id is not None and item.telegram_message_id in claimed:
                skipped += 1
                continue
            if await self._already_present(repository, chat.id, item) is not None:
                skipped += 1
                continue
            if item.telegram_message_id is not None:
                claimed.add(item.telegram_message_id)

            to_store.append(
                Message.record(
                    message_id=MessageId(self._ids.new_id()),
                    account_id=account_id,
                    chat_id=chat.id,
                    sender_kind=item.sender_kind,
                    message_type=item.message_type,
                    text=item.text,
                    sent_at=item.sent_at,
                    ingested_at=ingested_at,
                    telegram_message_id=(
                        TelegramMessageId(item.telegram_message_id)
                        if item.telegram_message_id is not None
                        else None
                    ),
                )
            )

        for message in to_store:
            await repository.add(message)

        return IngestionReport(
            stored=len(to_store),
            skipped=skipped,
            message_ids=tuple(message.id for message in to_store),
        )

    @staticmethod
    async def _already_present(
        repository: MessageRepository, chat_id: ChatId, item: IncomingMessage
    ) -> Message | None:
        """Return the message this one repeats, if the source issues identifiers.

        A source-less message has nothing to match on, so this returns ``None``
        and the message is stored -- which is why two identical typed messages
        are two messages.
        """
        if item.telegram_message_id is None:
            return None
        return await repository.get_by_telegram_id(
            chat_id, TelegramMessageId(item.telegram_message_id)
        )

    async def _require_chat(self, uow: UnitOfWork, account_id: AccountId, chat_id: int) -> Chat:
        """Return the chat to ingest into, raising if this account has none."""
        chat = await self._chats(uow, account_id).get(ChatId(chat_id))
        if chat is None:
            msg = f"No chat {chat_id} in account {int(account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That chat was not found.",
                context={"chat_id": chat_id, "account_id": int(account_id)},
            )
        return chat


class ReadChatHistory:
    """Returns a page of one chat's messages."""

    __slots__ = ("_accounts", "_messages", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        messages: ScopedRepositoryFactory[MessageRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._messages = messages
        self._accounts = accounts

    async def execute(
        self,
        chat_id: int,
        request: PageRequest | None = None,
        *,
        account_id: AccountId | None = None,
    ) -> Page[Message]:
        """Return one page of a chat's history, newest first."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            repository = self._messages(uow, resolved)
            return await repository.list_by_chat(ChatId(chat_id), request or PageRequest())


class GetMessage:
    """Looks a message up by identifier."""

    __slots__ = ("_accounts", "_messages", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        messages: ScopedRepositoryFactory[MessageRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._messages = messages
        self._accounts = accounts

    async def execute(
        self, message_id: int, *, account_id: AccountId | None = None
    ) -> Message | None:
        """Return a message, or ``None`` if this account has no such message."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._messages(uow, resolved).get(MessageId(message_id))


__all__ = [
    "SOURCE_MANUAL",
    "GetMessage",
    "IncomingMessage",
    "IngestMessages",
    "IngestionReport",
    "ReadChatHistory",
]
