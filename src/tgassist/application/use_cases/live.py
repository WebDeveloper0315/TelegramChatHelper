"""Live synchronisation: staying current once the history is stored.

Two phases in one run, and the order between them is the correctness argument.

**Catch-up** first. A process that was not running missed whatever arrived while
it was down, and backfill cannot recover it: backfill walks *downwards* from
``oldest_synced_message_id`` and would never look above the top of the stored
range. So a live run first pages forward from ``newest_synced_message_id`` until
it meets what is already stored. This is the reader that field was recorded for
(ADR-054).

**Draining** second. The gateway's update queue starts filling at ``connect()``,
before chat synchronisation, before backfill, and before this. So the window
between "we finished catching up" and "we started draining" contains nothing:
whatever arrived during the catch-up is already queued, and is delivered as soon
as the drain begins. That is why the ordering cannot lose an update (ADR-055).

One ingestion path
------------------

Both phases go through :class:`~tgassist.application.use_cases.message.IngestMessages`,
which is the same object the backfill uses. There is no second place that knows
how a Telegram message becomes a stored one, and therefore no second place for
the idempotency rule to be wrong.

Serial by construction
----------------------

Updates are processed one at a time. ADR-034 permits one transaction at a time
for the whole application, so per-chat or pooled concurrency would contend for
the same connection and buy nothing but nondeterministic ordering. A single
consumer task *is* the ingestion serialiser ADR-050 anticipated; a queue with
workers in front of one connection would be the same thing with more moving
parts.

Failure isolation
-----------------

One update failing rolls back that update's transaction and nothing else. The
drain continues, because a single message this application cannot store must not
end synchronisation for the account -- the same judgement chat synchronisation
makes about a chat it cannot describe. A failure of the *stream* ends the run,
because the next update will not arrive either.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tgassist.application.use_cases.account_scope import require_account, require_gateway_account
from tgassist.application.use_cases.backfill import DEFAULT_BATCH_SIZE, incoming_from
from tgassist.application.use_cases.message import IngestMessages
from tgassist.domain.errors import (
    ConflictError,
    ConstraintViolationError,
    DomainValidationError,
)
from tgassist.domain.events import MessagesIngested
from tgassist.domain.model.account import Account
from tgassist.domain.model.chat import Chat
from tgassist.domain.model.identifiers import AccountId, ChatId, TelegramChatId
from tgassist.domain.model.query import PageRequest
from tgassist.domain.model.sync_cursor import SyncCursor
from tgassist.domain.model.telegram import NewMessage, TelegramMessage, TelegramUpdate
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.sync_cursor_repository import SyncCursorRepository
from tgassist.domain.ports.telegram_gateway import TelegramGateway
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory

#: How many pages a catch-up will read before giving up on meeting the stored
#: range. A chat that received more than this while the process was down is
#: better served by re-running the backfill than by an unbounded forward walk
#: that holds the connection.
DEFAULT_CATCH_UP_PAGES = 20

#: Where messages came from, recorded on ``MessagesIngested`` so a subscriber
#: can tell a fifty-thousand-row backfill from one arriving message.
SOURCE_LIVE = "live"
SOURCE_CATCH_UP = "catch_up"

#: A problem with one update. Anything else -- the stream ending, the database
#: going away -- ends the run, because the next update would meet the same wall.
_ITEM_FAILURES = (DomainValidationError, ConflictError, ConstraintViolationError)


@dataclass(slots=True)
class LiveReport:
    """What one live run did.

    Mutable, unlike the other reports here, because a live run has no natural
    end: a caller reads it *while* the run is in progress and again after it
    stops. A frozen snapshot would have to be rebuilt on every update.

    Attributes:
        caught_up: Messages stored by the catch-up phase, before draining began.
        stored: Messages stored from live updates.
        skipped: Updates describing a message already stored. The number that
            shows a duplicate update costs nothing.
        ignored: Updates for a chat this account does not synchronise, or does
            not have. Not a failure: Telegram reports everything it sees.
        failed: Updates that could not be stored. The run continues past each
            one; the reason is kept in :attr:`failures` rather than only
            logged, so a caller can print what went wrong.
        failures: One safe sentence per rejected update, in order. Never a name
            and never any message content (``SECURITY.md`` section 9).
        events: How many ``MessagesIngested`` events were published.
        updates_seen: How many updates the drain has taken from the stream,
            including the ones it ignored. The number that distinguishes "the
            stream is quiet" from "we are not reading it".
    """

    caught_up: int = 0
    stored: int = 0
    skipped: int = 0
    ignored: int = 0
    failed: int = 0
    events: int = 0
    updates_seen: int = 0
    failures: list[str] = field(default_factory=list)


class SyncLive:
    """Keeps an account current with Telegram, one update at a time."""

    __slots__ = (
        "_accounts",
        "_batch_size",
        "_catch_up_pages",
        "_chats",
        "_clock",
        "_cursors",
        "_events",
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
        catch_up_pages: int = DEFAULT_CATCH_UP_PAGES,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory. One transaction per update.
            cursors: Sync cursor repository factory, scoped per account.
            chats: Chat repository factory, scoped per account.
            accounts: Account repository factory.
            ingest: The ingestion pipeline, shared with the backfill so that
                there is one place that knows how a Telegram message becomes a
                stored one.
            clock: Time source.
            events: Where ``MessagesIngested`` is published, after each commit.
            batch_size: Messages per catch-up page.
            catch_up_pages: How many pages a catch-up reads before giving up.
        """
        self._unit_of_work = unit_of_work
        self._cursors = cursors
        self._chats = chats
        self._accounts = accounts
        self._ingest = ingest
        self._clock = clock
        self._events = events
        self._batch_size = batch_size
        self._catch_up_pages = catch_up_pages

    async def execute(
        self,
        gateway: TelegramGateway,
        account_id: AccountId | None = None,
        *,
        report: LiveReport | None = None,
        catch_up: bool = True,
    ) -> LiveReport:
        """Catch up, then follow Telegram until the stream ends.

        Returns when the gateway disconnects, and raises if the run is
        cancelled -- which is what a caller stopping it looks like. In both
        cases everything committed stays committed and the cursors account for
        exactly it.

        Args:
            gateway: A connected gateway bound to this account.
            account_id: Account to follow. ``None`` selects the active one.
            report: A report to write into, so a caller watching a running
                synchronisation can read the counters as they move. One is
                created when none is given.
            catch_up: Whether to recover what arrived while nothing was running.
                Off only for a caller that has just back-filled and knows there
                is nothing above the stored range.

        Returns:
            What the run did.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
            AuthorizationError: If the gateway is bound to a different account.
            TelegramError: If Telegram could not be read.
        """
        async with self._unit_of_work() as uow:
            account = await require_account(self._accounts(uow), account_id)
        require_gateway_account(gateway, account.id)

        progress = report if report is not None else LiveReport()
        if catch_up:
            await self._catch_up(gateway, account, progress)
        await self._drain(gateway, account, progress)
        return progress

    # -- Catch-up ----------------------------------------------------------

    async def _catch_up(
        self, gateway: TelegramGateway, account: Account, report: LiveReport
    ) -> None:
        """Recover what arrived while nothing was consuming updates.

        Backfill cannot do this: it walks downwards from the *oldest* stored
        message and never looks above the top of the range. Without this pass, a
        restart would leave a permanent hole between the last run's newest
        message and the first update the new run happens to see.
        """
        for chat_id, cursor in await self._followable(account):
            await self._catch_up_chat(gateway, account, chat_id, cursor, report)

    async def _catch_up_chat(
        self,
        gateway: TelegramGateway,
        account: Account,
        chat_id: ChatId,
        cursor: SyncCursor,
        report: LiveReport,
    ) -> None:
        """Page forward from the top of one chat's stored range."""
        target = cursor.newest_synced_message_id
        if target is None:
            # Nothing is stored, so there is no gap above anything. The backfill
            # is what fills this chat, and it starts at the newest message.
            return

        async with self._unit_of_work() as uow:
            chat = await self._chats(uow, account.id).get(chat_id)
        if chat is None:  # pragma: no cover - the cursor named it a moment ago
            return

        before = None
        for _ in range(self._catch_up_pages):
            page = await gateway.fetch_history(
                chat.telegram_chat_id, before_message_id=before, limit=self._batch_size
            )
            if page.is_empty:
                return

            fresh = tuple(m for m in page.messages if int(m.id) > int(target))
            if fresh:
                stored = await self._store(account, chat, fresh, SOURCE_CATCH_UP, report)
                report.caught_up += stored
            if len(fresh) < len(page.messages):
                # This page reached back into what is already stored, so the gap
                # is closed. Stopping here is what keeps a catch-up bounded by
                # the size of the gap rather than by the size of the chat.
                return
            before = page.oldest_message_id

    async def _followable(self, account: Account) -> list[tuple[ChatId, SyncCursor]]:
        """Return every synchronised chat that has something stored, with its cursor.

        A chat with no cursor has never been back-filled, so there is no top of
        a range to catch up from; a chat with synchronisation switched off is
        the operator's decision and is left alone (ADR-053).
        """
        found: list[tuple[ChatId, SyncCursor]] = []
        async with self._unit_of_work() as uow:
            chats = self._chats(uow, account.id)
            cursors = self._cursors(uow, account.id)
            request = PageRequest()
            while True:
                page = await chats.list_chats(request)
                for chat in page.items:
                    if not chat.sync_enabled:
                        continue
                    cursor = await cursors.get(chat.id)
                    if cursor is not None and cursor.has_synced:
                        found.append((chat.id, cursor))
                if page.next_cursor is None:
                    return found
                request = PageRequest(cursor=page.next_cursor, limit=request.limit)

    # -- Draining ----------------------------------------------------------

    async def _drain(self, gateway: TelegramGateway, account: Account, report: LiveReport) -> None:
        """Process updates one at a time until the stream ends."""
        async for update in gateway.updates():
            report.updates_seen += 1
            await self._one(account, update, report)

    async def _one(self, account: Account, update: TelegramUpdate, report: LiveReport) -> None:
        """Process one update, in a transaction of its own."""
        if not isinstance(update, NewMessage):
            # Nothing else is consumed yet. Counted as ignored rather than
            # failed: an update kind with no consumer is a gap in this
            # application, not a fault in Telegram.
            report.ignored += 1
            return

        try:
            await self._on_message(account, update.message, report)
        except _ITEM_FAILURES as exc:
            report.failed += 1
            report.failures.append(f"chat {int(update.message.chat_id)}: {_explain(exc)}")

    async def _on_message(
        self, account: Account, message: TelegramMessage, report: LiveReport
    ) -> None:
        """Store one arriving message, if it belongs to a chat we follow."""
        chat = await self._chat_for(account, message.chat_id)
        if chat is None or not chat.sync_enabled:
            # A chat this account has not recorded, or has switched off. Telegram
            # reports everything it can see; what we keep is our decision.
            report.ignored += 1
            return

        stored = await self._store(account, chat, (message,), SOURCE_LIVE, report)
        report.stored += stored
        if stored == 0:
            report.skipped += 1

    async def _chat_for(self, account: Account, telegram_chat_id: TelegramChatId) -> Chat | None:
        """Return the local chat an update names, or ``None`` if we have none."""
        async with self._unit_of_work() as uow:
            return await self._chats(uow, account.id).get_by_telegram_id(telegram_chat_id)

    # -- The one write path ------------------------------------------------

    async def _store(
        self,
        account: Account,
        chat: Chat,
        messages: tuple[TelegramMessage, ...],
        source: str,
        report: LiveReport,
    ) -> int:
        """Store messages and advance the cursor, in one transaction.

        The same shape the backfill's ``_commit_batch`` uses, and for the same
        reason: a cursor advanced in a different transaction could commit while
        the messages did not.

        The event is published **after** the commit, never inside it. A handler
        observing a fact that then rolled back would be acting on something that
        never happened.

        Returns:
            How many messages were newly written.
        """
        now = self._clock.now()
        async with self._unit_of_work() as uow:
            ingestion = await self._ingest.ingest_within(
                uow, account.id, chat, [incoming_from(m) for m in messages]
            )
            if ingestion.stored == 0:
                # Every message was already there. Nothing to commit, and
                # nothing to announce.
                return 0

            cursors = self._cursors(uow, account.id)
            cursor = await cursors.get(chat.id)
            if cursor is None:
                cursor = SyncCursor.start(account_id=account.id, chat_id=chat.id, now=now)
            await cursors.save(
                cursor.with_batch(
                    oldest=min(m.id for m in messages),
                    newest=max(m.id for m in messages),
                    now=now,
                )
            )
            await uow.commit()

        await self._announce(account.id, chat.id, ingestion.stored, messages, source, report)
        return int(ingestion.stored)

    async def _announce(  # noqa: PLR0913, PLR0917 - one argument per event field
        self,
        account_id: AccountId,
        chat_id: ChatId,
        count: int,
        messages: tuple[TelegramMessage, ...],
        source: str,
        report: LiveReport,
    ) -> None:
        """Publish what was committed.

        Outside the transaction, and deliberately not guarded: the bus isolates
        a failing handler (``EventBus`` contract point 4), so a raise here would
        mean the bus itself is broken, and swallowing that would hide it. The
        messages are already committed either way, which is the guarantee that
        matters.
        """
        await self._events.publish(
            MessagesIngested(
                account_id=int(account_id),
                chat_id=int(chat_id),
                count=count,
                oldest_sent_at=min(m.sent_at for m in messages),
                newest_sent_at=max(m.sent_at for m in messages),
                source=source,
            )
        )
        report.events += 1


@dataclass(frozen=True, slots=True)
class LiveOutcome:
    """How a supervised live run ended.

    Attributes:
        report: What was stored, read from the same object the run was writing
            into -- so a caller watching it while the run was going sees the
            final values in the same place.
        restarts: How many times the run was started again after a failure it
            could recover from.
        failure: What stopped it for good, or ``None`` when it was interrupted
            or the stream simply ended. Returned rather than raised, because a
            run that stored ten thousand messages and then failed did both, and
            an exception would report only the second half.
    """

    report: LiveReport
    restarts: int = 0
    failure: BaseException | None = None

    @property
    def is_clean(self) -> bool:
        """Whether the run ended without anything going wrong."""
        return self.failure is None and self.restarts == 0 and self.report.failed == 0


#: What the supervised consumer is called, in logs and in reports. Named here
#: rather than at the composition root so the report and the supervisor agree.
LIVE_TASK_NAME = "live-sync"


def _explain(error: Exception) -> str:
    """Return the safe, user-facing sentence an error carries.

    Deliberately not ``str(error)``: a developer message names values, and a
    report is both printed and logged.
    """
    message = getattr(error, "user_message", None)
    return message if isinstance(message, str) and message else "It could not be stored."


__all__ = [
    "DEFAULT_CATCH_UP_PAGES",
    "LIVE_TASK_NAME",
    "SOURCE_CATCH_UP",
    "SOURCE_LIVE",
    "LiveOutcome",
    "LiveReport",
    "SyncLive",
]
