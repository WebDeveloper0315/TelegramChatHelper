"""The SyncCursor aggregate.

Where a chat's history backfill has got to. One row per chat, and the whole of
what makes an interrupted backfill resumable rather than restartable.

The mechanism, in one sentence
------------------------------

The cursor is written **in the same transaction as the messages it accounts
for** (ADR-050). A committed batch is accounted for; an uncommitted one never
happened. There is therefore no reconciliation pass, no repair logic and no
state in which the cursor and the stored messages disagree -- resumability is a
property of the transaction boundary rather than a feature built on top of one.

Why a message identifier and not a timestamp
--------------------------------------------

``oldest_synced_message_id`` is what the next fetch continues from, and it is a
Telegram message identifier because that is what Telegram's history API takes.
Three properties make it the right cursor and a timestamp the wrong one:

* Telegram pages history **by message identifier**. A timestamp cursor would
  need converting on every resume, through a second call that can land on a
  different message when two share a second.
* Message identifiers are unique and totally ordered **within a chat**, which is
  exactly the scope of one cursor. Timestamps are neither: two messages can
  share a second, so a timestamp cursor either re-reads or skips at the
  boundary, and which one it does depends on whether the comparison is
  inclusive.
* The identifier is *ours to hold*. An opaque continuation token from TDLib
  would make resumability depend on that token surviving a restart and a
  library upgrade, neither of which Telegram promises.

The cursor always names a message that **is** stored. It advances only inside
the transaction that stored the batch, so a crash before commit leaves both the
messages and the cursor untouched.

Direction
---------

Backfill runs **backwards**: newest first, which is the only direction
Telegram's history API supports efficiently and the only one where an
interruption leaves a contiguous stored range. So ``oldest_synced_message_id``
moves down with every batch, and ``newest_synced_message_id`` is established by
the first batch and never lowered.

Fields deferred
---------------

``DOMAIN_MODEL.md`` section 5.22 names two more, and neither is implemented
here:

* ``consecutive_failures`` -- exists to drive exponential backoff and, past a
  threshold, to disable a chat and raise a Notification. Neither backoff nor
  notifications exist, so the counter would be written and never consulted,
  which is the placeholder this project does not write. Unlike the fields below
  it records nothing historical: it starts at zero whenever it is added.
* ``last_error`` -- dropped by ADR-050 rather than deferred. An error string on
  a row is a log entry in the wrong place.

There is no ``created_at`` either. Nothing asks when a chat was first
synchronised, and ``last_sync_at`` is the time that means something.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    TelegramMessageId,
    require_positive_identifier,
)


@dataclass(frozen=True, slots=True)
class SyncCursor:
    """How far a chat's history has been synchronised.

    Immutable, like every entity here: advancing returns a new instance, so a
    cursor held by a running batch cannot change underneath it.

    Attributes:
        account_id: The account this chat belongs to. Present so the composite
            foreign key to ``chats`` can exist (ADR-043), which is what makes a
            cursor for one account's chat unattachable to another's.
        chat_id: The chat this cursor tracks, and its identity. Exactly one
            cursor per chat, so this is the key rather than a surrogate beside
            one (ADR-054, applying ADR-038's reasoning).
        oldest_synced_message_id: The oldest Telegram message stored for this
            chat, and where the next fetch continues from. ``None`` before
            anything has been stored, which is also what "start at the newest"
            means to the gateway.
        newest_synced_message_id: The newest Telegram message stored. Set by the
            first batch of a backfill and never lowered, so the pair states the
            range this cursor accounts for rather than only its floor.
        backfill_complete: Whether there is nothing further back worth fetching.
            True on reaching the beginning of the chat *or* the horizon, which
            is why :attr:`backfill_horizon` is stored beside it.
        backfill_horizon: The oldest instant this backfill intends to reach, or
            ``None`` for no limit. Recorded because a later run configured to
            reach further back must be able to tell that its predecessor stopped
            early rather than finished.
        last_sync_at: When a batch last committed, or ``None``.
        updated_at: When this cursor last changed, UTC.
    """

    account_id: AccountId
    chat_id: ChatId
    oldest_synced_message_id: TelegramMessageId | None
    newest_synced_message_id: TelegramMessageId | None
    backfill_complete: bool
    backfill_horizon: datetime | None
    last_sync_at: datetime | None
    updated_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.account_id, name="Account id")
        require_positive_identifier(self.chat_id, name="Chat id")

        for value, name in (
            (self.oldest_synced_message_id, "oldest_synced_message_id"),
            (self.newest_synced_message_id, "newest_synced_message_id"),
        ):
            if value is not None:
                require_positive_identifier(value, name=name)

        for instant, name in (
            (self.backfill_horizon, "backfill_horizon"),
            (self.last_sync_at, "last_sync_at"),
            (self.updated_at, "updated_at"),
        ):
            if instant is not None:
                _require_utc(instant, name=name)

        # The two ends of one range. Either both are set or neither is: a floor
        # with no ceiling would describe a range whose extent nobody can state,
        # and a ceiling with no floor would send the next fetch to the newest
        # message all over again.
        if (self.oldest_synced_message_id is None) != (self.newest_synced_message_id is None):
            msg = (
                f"A cursor names one end of its range and not the other: "
                f"oldest={self.oldest_synced_message_id}, "
                f"newest={self.newest_synced_message_id}"
            )
            raise DomainValidationError(
                msg,
                user_message="That synchronisation bookmark is inconsistent.",
                context={"chat_id": int(self.chat_id)},
            )

        if (
            self.oldest_synced_message_id is not None
            and self.newest_synced_message_id is not None
            and self.oldest_synced_message_id > self.newest_synced_message_id
        ):
            msg = (
                f"A cursor's oldest message {int(self.oldest_synced_message_id)} is newer "
                f"than its newest {int(self.newest_synced_message_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That synchronisation bookmark is inconsistent.",
                context={"chat_id": int(self.chat_id)},
            )

    # -- Construction -----------------------------------------------------

    @classmethod
    def start(
        cls,
        *,
        account_id: AccountId,
        chat_id: ChatId,
        now: datetime,
        backfill_horizon: datetime | None = None,
    ) -> SyncCursor:
        """Build a cursor for a chat nothing has been stored from yet.

        Also what ``--reset`` produces. Resetting builds a *new* cursor rather
        than clearing fields on the old one, because "start again" and "forget
        what happened" are the same thing here and expressing it as a
        constructor leaves no field to forget.
        """
        return cls(
            account_id=account_id,
            chat_id=chat_id,
            oldest_synced_message_id=None,
            newest_synced_message_id=None,
            backfill_complete=False,
            backfill_horizon=backfill_horizon,
            last_sync_at=None,
            updated_at=now,
        )

    # -- Derived state ----------------------------------------------------

    @property
    def has_synced(self) -> bool:
        """Whether any message has been stored for this chat."""
        return self.oldest_synced_message_id is not None

    @property
    def resume_from(self) -> TelegramMessageId | None:
        """Where the next fetch should continue from.

        ``None`` means "start at the newest", which is what the gateway's
        ``before_message_id=None`` means. Naming it here rather than passing
        ``cursor.oldest_synced_message_id`` at the call site keeps "where do we
        resume" from being answered by reading a field and knowing a convention.
        """
        return self.oldest_synced_message_id

    def reaches_back_to(self, horizon: datetime | None) -> bool:
        """Whether a completed backfill already covers this horizon.

        ``None`` means unlimited, which only an unlimited backfill satisfies.
        A run configured to reach further back than its predecessor did has more
        to fetch even though ``backfill_complete`` is set, and this is what tells
        it so (ADR-054).
        """
        if self.backfill_horizon is None:
            return True
        if horizon is None:
            return False
        return horizon >= self.backfill_horizon

    # -- Transitions ------------------------------------------------------

    def with_batch(
        self,
        *,
        oldest: TelegramMessageId,
        newest: TelegramMessageId,
        now: datetime,
    ) -> SyncCursor:
        """Return this cursor advanced over one committed batch.

        Called **inside the transaction that stored the batch**. That is the
        whole mechanism: a cursor advanced in a separate transaction could
        commit while the messages did not, and the next run would resume past
        messages nobody had stored.

        The floor moves down and the ceiling only ever moves up. Backfill runs
        backwards, so a batch never contains anything newer than the first one
        did -- but expressing the ceiling as a maximum rather than as
        "set once" means live updates (slice 7) advance it through the same
        method rather than needing another.

        Args:
            oldest: The oldest Telegram identifier in the committed batch.
            newest: The newest Telegram identifier in the committed batch.
            now: Current instant, from the injected clock.

        Raises:
            DomainValidationError: If the batch names a range that is not one.
        """
        if oldest > newest:
            msg = f"A batch cannot end at {int(newest)} and begin at {int(oldest)}"
            raise DomainValidationError(msg, user_message="That batch of messages is inconsistent.")

        floor = (
            oldest
            if self.oldest_synced_message_id is None
            else min(self.oldest_synced_message_id, oldest)
        )
        ceiling = (
            newest
            if self.newest_synced_message_id is None
            else max(self.newest_synced_message_id, newest)
        )
        return replace(
            self,
            oldest_synced_message_id=floor,
            newest_synced_message_id=ceiling,
            last_sync_at=now,
            updated_at=now,
        )

    def completed(self, now: datetime, *, horizon: datetime | None = None) -> SyncCursor:
        """Return this cursor marked as having nothing further back to fetch.

        The horizon is recorded at the same moment, because "complete" without
        it cannot distinguish *we reached the beginning of the chat* from *we
        stopped where we were told to*. A later run configured to reach further
        back asks :meth:`reaches_back_to` and reopens.

        Returns ``self`` when already complete for this horizon, so a run over a
        finished chat does not move ``updated_at``.
        """
        if self.backfill_complete and self.backfill_horizon == horizon:
            return self
        return replace(self, backfill_complete=True, backfill_horizon=horizon, updated_at=now)

    def reopened(self, now: datetime, *, horizon: datetime | None) -> SyncCursor:
        """Return this cursor open again, to reach further back than it did.

        Keeps both ends of the stored range. A reopened backfill continues from
        the same floor -- everything above it is already stored, and re-reading
        it would be work whose only outcome is recognising what is already
        there.
        """
        if not self.backfill_complete and self.backfill_horizon == horizon:
            return self
        return replace(self, backfill_complete=False, backfill_horizon=horizon, updated_at=now)


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That bookmark has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That bookmark has an invalid timestamp.")


__all__ = ["SyncCursor"]
