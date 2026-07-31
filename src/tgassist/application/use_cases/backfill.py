"""Historical message backfill.

Reads a chat's history backwards from the newest message and stores it, one
bounded batch at a time, resuming exactly where the last run stopped.

The five guarantees, and where each one lives
----------------------------------------------

**Resumability** is :class:`~tgassist.domain.model.sync_cursor.SyncCursor`. The
cursor names the oldest message stored, and the next fetch continues from it.
Nothing else is needed: there is no reconciliation pass and no repair logic.

**Crash safety** is the transaction boundary. Messages and cursor are written in
*one* unit of work, so a process that dies mid-batch leaves neither. The cursor
therefore always names a message that is stored, and the invariant holds without
anything checking it.

**Idempotency** is the partial unique index on
``(account_id, chat_id, telegram_message_id)`` (ADR-045), plus the lookup
:class:`~tgassist.application.use_cases.message.IngestMessages` performs before
each write. A re-run stores nothing and reports every message as already
present.

**Bounded transactions** are the batch size, default 100 and configured at
``telegram.backfill_batch_size``. ADR-034 permits one transaction at a time for
the whole application, so a run-long transaction would be a freeze rather than a
sync (ADR-050).

**Deterministic progress** is the fact that every batch either advances the
cursor or ends the run. A page that stored nothing new still moves the cursor
down, because the messages it described are present -- so the loop cannot
revisit the same page twice, whatever Telegram returns.

What ends a run
---------------

The **first** of: an empty page (the beginning of the chat), a page older than
the configured horizon, the caller's batch limit, or an error. The per-chat
message cap in ``PROJECT_SPEC.md`` section 4.1 is **not** implemented here: it
needs a count of stored messages per chat, which needs a repository method and
an index that should be chosen by the query using it. The horizon already bounds
every run, and ``--max-batches`` bounds one.

What this is not
----------------

It is not live synchronisation. Nothing here consumes ``updates()``, and a
message that arrives while a backfill is running is not seen by it -- the
backfill walks *downwards* from where it started. Slice 7 owns the other
direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from tgassist.application.use_cases.account_scope import require_account, require_gateway_account
from tgassist.application.use_cases.message import (
    IncomingMessage,
    IngestionReport,
    IngestMessages,
)
from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.events import MessagesIngested
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.identifiers import AccountId, ChatId
from tgassist.domain.model.message import MessageType, SenderKind
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.sync_cursor import SyncCursor
from tgassist.domain.model.telegram import TelegramMessage
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.sync_cursor_repository import SyncCursorRepository
from tgassist.domain.ports.telegram_gateway import DEFAULT_HISTORY_LIMIT, TelegramGateway
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

#: How many messages one batch fetches and one transaction writes.
#:
#: Aligned with TDLib's practical history page size, which is what makes a batch
#: one round trip rather than a fraction of one. At the documented per-chat cap
#: of 50 000 messages this is roughly 500 short transactions instead of one long
#: one (ADR-050).
DEFAULT_BATCH_SIZE = DEFAULT_HISTORY_LIMIT

#: How far back a backfill reaches when nothing says otherwise, in days.
#: ``PROJECT_SPEC.md`` section 4.1. Zero means no limit.
DEFAULT_HORIZON_DAYS = 365

#: What a backfill calls itself on the events it publishes, so a subscriber can
#: tell fifty thousand stored rows from one arriving message.
SOURCE_BACKFILL = "backfill"

#: A guard against a loop that never terminates because Telegram keeps answering
#: with pages that store nothing and advance nothing. The cursor advance makes
#: that impossible in principle; this makes it impossible in practice.
MAX_BATCHES_PER_RUN = 10_000


class BackfillStop:
    """Why a run stopped. Values are stable text, so a caller can match on them."""

    BEGINNING = "beginning"
    """The chat has no messages older than what is stored."""

    HORIZON = "horizon"
    """Everything back to the configured horizon is stored."""

    ALREADY_COMPLETE = "already_complete"
    """The chat was finished before this run started."""

    BATCH_LIMIT = "batch_limit"
    """The caller asked for a bounded number of batches, and got them."""

    NO_PROGRESS = "no_progress"
    """A batch left the bookmark where it was, so another would fetch the same
    page. Unreachable against a gateway that honours ``before_message_id``, and
    the reason the loop cannot spin against one that does not."""


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """What one backfill run did.

    Attributes:
        chat_id: The chat that was synchronised.
        batches: How many batches committed.
        stored: Messages written by this run.
        skipped: Messages Telegram returned that were already present. The
            number that shows a re-run is idempotent rather than merely
            harmless.
        stop_reason: Why the run ended, from :class:`BackfillStop`.
        cursor: Where the chat now stands. The authority on what to do next, and
            returned so a caller need not read it back.
    """

    chat_id: ChatId
    batches: int
    stored: int
    skipped: int
    stop_reason: str
    cursor: SyncCursor

    @property
    def fetched(self) -> int:
        """How many messages Telegram returned across the run."""
        return self.stored + self.skipped

    @property
    def is_complete(self) -> bool:
        """Whether there is nothing further back to fetch for this horizon."""
        return self.cursor.backfill_complete


class SyncHistory:
    """Stores a chat's history, backwards, resumably.

    One instance per call, like every use case here. The gateway is a parameter
    rather than a constructor dependency because it holds a live connection, and
    a use case built once per call has no lifetime to hang that on
    (``TELEGRAM_ARCHITECTURE.md`` section 7.3).
    """

    __slots__ = (
        "_accounts",
        "_batch_size",
        "_chats",
        "_clock",
        "_cursors",
        "_events",
        "_horizon_days",
        "_ingest",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        cursors: ScopedRepositoryFactory[SyncCursorRepository],
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
        ingest: IngestMessages,
        clock: Clock,
        events: EventBus,
        batch_size: int = DEFAULT_BATCH_SIZE,
        horizon_days: int = DEFAULT_HORIZON_DAYS,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory. One transaction per batch.
            cursors: Sync cursor repository factory, scoped per account.
            chats: Chat repository factory, scoped per account.
            accounts: Account repository factory.
            ingest: The ingestion pipeline. Reused rather than reimplemented,
                so the idempotency rule has one home -- and its
                ``ingest_within`` is what lets messages and cursor share a
                transaction.
            clock: Time source. Also what the horizon is measured from.
            events: Where ``MessagesIngested`` is published, once per committed
                batch (ADR-050). One event per message would run every handler
                fifty thousand times inside the loop.
            batch_size: Messages per fetch and per transaction.
            horizon_days: How far back to reach. Zero means no limit.
        """
        self._unit_of_work = unit_of_work
        self._cursors = cursors
        self._chats = chats
        self._accounts = accounts
        self._ingest = ingest
        self._clock = clock
        self._events = events
        self._batch_size = batch_size
        self._horizon_days = horizon_days

    async def execute(
        self,
        gateway: TelegramGateway,
        chat_id: int,
        account_id: AccountId | None = None,
        *,
        reset: bool = False,
        max_batches: int | None = None,
    ) -> BackfillReport:
        """Fetch and store this chat's history, continuing where the last run stopped.

        Args:
            gateway: A connected gateway bound to this account.
            chat_id: The **local** chat identifier, from ``tgassist chat list``.
            account_id: Account to synchronise. ``None`` selects the active one.
            reset: Discard the bookmark and start again from the newest message.
                Stored messages are not deleted; they are recognised and skipped,
                which is what makes a reset cheap in everything but network.
            max_batches: Stop after this many batches. What ``--max-batches``
                passes, and what a test uses to interrupt a run at a known point.

        Returns:
            What the run did, and where the chat now stands.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such chat.
            DomainValidationError: If the chat has synchronisation switched off,
                or ``max_batches`` is not positive.
            AuthorizationError: If the gateway is bound to a different account,
                or the account is not signed in.
            TelegramError: If Telegram could not be read. Whatever committed
                before that stays committed, and the cursor accounts for exactly
                it.
        """
        if max_batches is not None and max_batches < 1:
            msg = f"A run must be allowed at least one batch, got {max_batches}"
            raise DomainValidationError(
                msg, user_message="Ask for at least one batch, or none at all."
            )

        account, chat, cursor = await self._begin(gateway, chat_id, account_id, reset=reset)

        horizon = self._horizon()
        if cursor.backfill_complete:
            if cursor.reaches_back_to(horizon):
                return self._report(cursor, BackfillStop.ALREADY_COMPLETE)
            cursor = await self._save(
                account.id, cursor.reopened(self._clock.now(), horizon=horizon)
            )

        return await self._run(gateway, account, chat, cursor, horizon, max_batches)

    async def execute_all(
        self,
        gateway: TelegramGateway,
        account_id: AccountId | None = None,
        *,
        reset: bool = False,
        max_batches: int | None = None,
    ) -> tuple[BackfillReport, ...]:
        """Back-fill every chat this account has synchronisation switched on for.

        Chats are taken one at a time and in listing order, so an interrupted
        run leaves earlier chats complete rather than every chat part-done.
        ``max_batches`` is **per chat**, not per run: it bounds how long any one
        chat holds the connection, which is what it is for.

        The syncable chats are filtered here rather than by a repository query.
        There is no ``list_syncable`` on the port, because the index that should
        serve one belongs to the scheduler that will run it thousands of times a
        day (``DATABASE.md`` section 20) rather than to a command a person types.

        Returns:
            One report per chat attempted, in the order attempted.
        """
        account, chats = await self._syncable(account_id)
        require_gateway_account(gateway, account.id)

        reports: list[BackfillReport] = []
        for chat_id in chats:
            reports.append(
                await self.execute(
                    gateway, int(chat_id), account.id, reset=reset, max_batches=max_batches
                )
            )
        return tuple(reports)

    async def _syncable(self, account_id: AccountId | None) -> tuple[Account, tuple[ChatId, ...]]:
        """Return the account and every chat it synchronises, in listing order.

        Pages to exhaustion rather than taking the first page. A silent cap here
        would look exactly like an account with fewer chats, and the chats it
        dropped would be the ones nobody noticed were never synchronised.
        """
        found: list[ChatId] = []
        async with self._unit_of_work() as uow:
            account = await require_account(self._accounts(uow), account_id)
            chats = self._chats(uow, account.id)
            request = PageRequest()
            while True:
                page = await chats.list_chats(request)
                found.extend(chat.id for chat in page.items if chat.sync_enabled)
                if page.next_cursor is None:
                    return account, tuple(found)
                request = PageRequest(cursor=page.next_cursor, limit=request.limit)

    # -- The loop ---------------------------------------------------------

    async def _run(  # noqa: PLR0913, PLR0917 - one argument per thing the loop needs
        self,
        gateway: TelegramGateway,
        account: Account,
        chat: Chat,
        cursor: SyncCursor,
        horizon: datetime | None,
        max_batches: int | None,
    ) -> BackfillReport:
        """Fetch and commit batches until something ends the run."""
        limit = min(max_batches or MAX_BATCHES_PER_RUN, MAX_BATCHES_PER_RUN)
        batches = stored = skipped = 0
        reason = BackfillStop.BATCH_LIMIT

        for _ in range(limit):
            page = await gateway.fetch_history(
                chat.telegram_chat_id,
                before_message_id=cursor.resume_from,
                limit=self._batch_size,
            )
            if page.is_empty:
                # The beginning of the chat, reported by the page rather than
                # inferred from its length -- Telegram returns short pages for
                # reasons of its own (slice 4).
                cursor = await self._save(
                    account.id, cursor.completed(self._clock.now(), horizon=horizon)
                )
                reason = BackfillStop.BEGINNING
                break

            keep = _within(page.messages, horizon)
            if not keep:
                # Every message on this page is older than we intend to reach.
                # The cursor does not advance: nothing was stored, and moving it
                # would claim messages that are not there.
                cursor = await self._save(
                    account.id, cursor.completed(self._clock.now(), horizon=horizon)
                )
                reason = BackfillStop.HORIZON
                break

            reached_horizon = len(keep) < len(page.messages)
            previous = cursor.resume_from
            cursor, report = await self._commit_batch(
                account.id, chat, keep, cursor, horizon if reached_horizon else None
            )
            batches += 1
            stored += report.stored
            skipped += report.skipped

            if reached_horizon:
                reason = BackfillStop.HORIZON
                break
            if cursor.resume_from == previous:
                # The next fetch would ask the same question and get the same
                # answer. Stopping is the only honest response: every batch must
                # move the bookmark down or end the run.
                reason = BackfillStop.NO_PROGRESS
                break
        return BackfillReport(
            chat_id=chat.id,
            batches=batches,
            stored=stored,
            skipped=skipped,
            stop_reason=reason,
            cursor=cursor,
        )

    async def _commit_batch(
        self,
        account_id: AccountId,
        chat: Chat,
        batch: tuple[TelegramMessage, ...],
        cursor: SyncCursor,
        complete_at: datetime | None,
    ) -> tuple[SyncCursor, IngestionReport]:
        """Store one batch and advance the cursor, in one transaction.

        The order inside the transaction is the order ADR-050 requires:
        ingest, then advance, then commit. Nothing between the ingest and the
        commit can leave messages stored and the cursor behind them, because
        there is no intermediate commit for a crash to land in.

        ``complete_at`` is the horizon to record when this batch is also the
        last one -- passed in rather than decided here, because whether the run
        is ending is the loop's knowledge and not this method's.

        Returns:
            The advanced cursor and what the ingestion did.
        """
        advanced = cursor.with_batch(
            oldest=min(message.id for message in batch),
            newest=max(message.id for message in batch),
            now=self._clock.now(),
        )
        if complete_at is not None:
            advanced = advanced.completed(self._clock.now(), horizon=complete_at)

        async with self._unit_of_work() as uow:
            report = await self._ingest.ingest_within(
                uow, account_id, chat, [incoming_from(message) for message in batch]
            )
            await self._cursors(uow, account_id).save(advanced)
            await uow.commit()

        if report.stored:
            # After the commit, never inside it: a handler observing a fact that
            # then rolled back would be acting on something that never happened.
            await self._events.publish(
                MessagesIngested(
                    account_id=int(account_id),
                    chat_id=int(chat.id),
                    count=report.stored,
                    oldest_sent_at=min(message.sent_at for message in batch),
                    newest_sent_at=max(message.sent_at for message in batch),
                    source=SOURCE_BACKFILL,
                )
            )
        return advanced, report

    # -- Reading and writing the bookmark ---------------------------------

    async def _begin(
        self,
        gateway: TelegramGateway,
        chat_id: int,
        account_id: AccountId | None,
        *,
        reset: bool,
    ) -> tuple[Account, Chat, SyncCursor]:
        """Resolve the account and chat, and load or create the cursor.

        One transaction, and it commits only when a cursor had to be created --
        a run against a chat that has been synchronised before writes nothing
        until its first batch.

        The gateway is checked here, immediately after the account resolves and
        **before any of that account's data is read**. A mis-wired call must not
        be able to learn that another account has a chat, let alone which one.
        """
        async with self._unit_of_work() as uow:
            account = await require_account(self._accounts(uow), account_id)
            require_gateway_account(gateway, account.id)
            chat = await self._require_chat(uow, account.id, chat_id)
            _require_syncable(chat)

            cursors = self._cursors(uow, account.id)
            existing = None if reset else await cursors.get(chat.id)
            if existing is not None:
                return account, chat, existing

            fresh = SyncCursor.start(
                account_id=account.id,
                chat_id=chat.id,
                now=self._clock.now(),
                backfill_horizon=self._horizon(),
            )
            await cursors.save(fresh)
            await uow.commit()
            return account, chat, fresh

    async def _save(self, account_id: AccountId, cursor: SyncCursor) -> SyncCursor:
        """Write a cursor that accounts for no new messages, in its own transaction.

        Only for the transitions that store nothing -- reaching the beginning,
        reaching the horizon, reopening. A cursor that *does* account for
        messages is written beside them in :meth:`_commit_batch`, never here.
        """
        async with self._unit_of_work() as uow:
            await self._cursors(uow, account_id).save(cursor)
            await uow.commit()
        return cursor

    async def _require_chat(self, uow: UnitOfWork, account_id: AccountId, chat_id: int) -> Chat:
        """Return the chat to synchronise, raising if this account has none."""
        chat = await self._chats(uow, account_id).get(ChatId(chat_id))
        if chat is None:
            msg = f"No chat {chat_id} in account {int(account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That chat was not found.",
                context={"chat_id": chat_id, "account_id": int(account_id)},
            )
        return chat

    def _horizon(self) -> datetime | None:
        """Return the oldest instant this run intends to reach, or ``None``."""
        if self._horizon_days <= 0:
            return None
        return self._clock.now() - timedelta(days=self._horizon_days)

    @staticmethod
    def _report(cursor: SyncCursor, reason: str) -> BackfillReport:
        """Build a report for a run that fetched nothing."""
        return BackfillReport(
            chat_id=cursor.chat_id,
            batches=0,
            stored=0,
            skipped=0,
            stop_reason=reason,
            cursor=cursor,
        )


def _within(
    messages: tuple[TelegramMessage, ...], horizon: datetime | None
) -> tuple[TelegramMessage, ...]:
    """Return the messages at or after the horizon.

    Filtering rather than truncating at the first old message: Telegram returns
    a page newest-first, so the old ones are at the end -- but a page whose
    ordering ever differed would silently drop the tail if this trusted the
    order. It costs one comparison per message to not depend on that.
    """
    if horizon is None:
        return messages
    return tuple(message for message in messages if message.sent_at >= horizon)


def incoming_from(message: TelegramMessage) -> IncomingMessage:
    """Translate what Telegram said into what ingestion accepts.

    Public because live synchronisation performs the same translation, and two
    copies of "what does an arriving message mean" would eventually disagree
    about the one thing that matters here -- whose side of the conversation it
    is on.

    The one place the operator's side of a conversation is decided, and it is
    read from Telegram rather than inferred: ``is_outgoing`` is stated directly,
    which matters for a message sent from another device, where comparing the
    sender against the operator would be the only alternative and would be
    right for the wrong reason.
    """
    return IncomingMessage(
        sender_kind=sender_kind_of(message),
        sent_at=message.sent_at,
        text=message.text,
        message_type=message.message_type,
        telegram_message_id=int(message.id),
    )


def sender_kind_of(message: TelegramMessage) -> SenderKind:
    """Return who sent a message, in this application's three-way vocabulary."""
    if message.message_type is MessageType.SERVICE or message.sender_id is None:
        # Telegram itself produced it. Attributing it to either party would put
        # words in somebody's mouth.
        return SenderKind.SYSTEM
    return SenderKind.OPERATOR if message.is_outgoing else SenderKind.CONTACT


def _require_syncable(chat: Chat) -> None:
    """Refuse a chat whose synchronisation the operator has switched off.

    ``sync_enabled`` is the operator's decision (ADR-053), and a backfill that
    ignored it would ingest history for a conversation somebody had excluded.

    Raises:
        DomainValidationError: If the chat has synchronisation switched off.
    """
    if chat.sync_enabled:
        return
    msg = f"Chat {int(chat.id)} has synchronisation switched off"
    raise DomainValidationError(
        msg,
        user_message=(
            "That chat has synchronisation switched off. Turn it on with "
            "`tgassist chat policy` first."
        ),
        context={"chat_id": int(chat.id)},
    )


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_HORIZON_DAYS",
    "MAX_BATCHES_PER_RUN",
    "SOURCE_BACKFILL",
    "BackfillReport",
    "BackfillStop",
    "SyncHistory",
    "incoming_from",
    "sender_kind_of",
]
