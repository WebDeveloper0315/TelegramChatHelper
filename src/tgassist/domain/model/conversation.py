"""The Conversation aggregate.

A bounded episode of interaction within a Chat: a contiguous run of messages
separated from its neighbours by a period of silence. It is the unit that
summarisation, planning and analysis attach to, and it exists so that prompt
context is proportional to a coherent exchange rather than to an entire chat
history.

Derived, not reported
---------------------

Every other aggregate here records something a person or Telegram decided. A
Conversation records something *this application computed* from stored messages,
which changes three things about it.

**It has no external identifier**, and could not have one. Telegram has no
notion of a conversation.

**It can be recomputed at any time**, and must produce the same answer. The
segmentation rule (``domain/services/segmentation.py``) is a pure function of
message timestamps and counts, both of which are immutable once stored, so
re-segmenting a chat twice yields identical boundaries.

**Its extent is a claim about the past, not about now.** A Conversation
describes the messages that existed when segmentation last ran. Messages
ingested since are not in it until segmentation runs again, and
``tgassist conversation rebuild`` is how a caller asks.

Membership is the time range
----------------------------

A message belongs to the Conversation whose ``[started_at, ended_at]`` contains
its ``sent_at``. There is no ``conversation_id`` on ``messages`` and no join
table, for two reasons (ADR-056):

* ``Message`` is append-only and ``MessageRepository`` has no update path at all
  (ADR-046). Assigning a conversation to a stored message would be exactly the
  mutation that discipline exists to forbid.
* Conversations within a chat do not overlap, so the range *is* the membership.
  Storing it again would be storing a fact already implied by two timestamps,
  and re-segmentation would then have to rewrite fifty thousand rows to say what
  a handful of conversation rows already say.

Fields deferred
---------------

``DOMAIN_MODEL.md`` section 5.7 names three more, none implemented here:

* ``is_open`` -- **derived, and deliberately not stored**. Whether a
  conversation may still grow depends on how long ago it ended, which depends on
  *now*. A stored flag would be true when written and wrong an hour later, with
  no job to correct it. :meth:`Conversation.is_open_at` asks the question against
  a supplied instant instead.
* ``initiated_by`` -- ``operator`` or ``contact``, from the first message's
  sender. Nothing reads it until relationship metrics (Milestone 6), and it is
  recomputable from the messages at any time.
* ``dominant_language`` -- needs language detection, which is Milestone 6 and
  which this slice is explicitly not allowed to introduce.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ConversationId,
    require_positive_identifier,
)


@dataclass(frozen=True, slots=True)
class Conversation:
    """One bounded episode of interaction within a Chat.

    Immutable, like every entity here: an extension returns a new instance.

    Attributes:
        id: Local identifier. Stable across re-segmentation, which is a property
            of the matching rule rather than of the generator (ADR-056).
        account_id: The Account this belongs to. Present so the foreign key to
            ``chats`` can be composite, which is what makes a conversation in one
            account's chat unattachable to another's (ADR-043).
        chat_id: The Chat this episode happened in. A Conversation never spans
            Chats, which the composite key makes structural rather than checked.
        started_at: When its first message was sent, UTC.
        ended_at: When its last message was sent, UTC. Never null: a
            conversation is derived from messages that already exist, so it
            always has a last one. Equal to ``started_at`` for a single message.
        message_count: How many messages were in it when segmentation last ran.
            Stored rather than counted, because the listing needs it per row and
            the segmentation rule needs it to apply the message cap.
        created_at: When this conversation was first computed, UTC.
        updated_at: When its extent last changed, UTC.
    """

    id: ConversationId
    account_id: AccountId
    chat_id: ChatId
    started_at: datetime
    ended_at: datetime
    message_count: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="Conversation id")
        require_positive_identifier(self.account_id, name="Account id")
        require_positive_identifier(self.chat_id, name="Chat id")

        for value, name in (
            (self.started_at, "started_at"),
            (self.ended_at, "ended_at"),
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
        ):
            _require_utc(value, name=name)

        if self.ended_at < self.started_at:
            msg = f"A conversation cannot end before it began: {self.ended_at} < {self.started_at}"
            raise DomainValidationError(
                msg,
                user_message="That conversation has inconsistent dates.",
                context={"conversation_id": int(self.id)},
            )

        if self.message_count < 1:
            # A conversation is a run of messages. An empty one is not a shorter
            # conversation, it is a row that should have been deleted -- and
            # allowing it would let a segmentation bug survive as data.
            msg = f"A conversation contains at least one message, got {self.message_count}"
            raise DomainValidationError(
                msg,
                user_message="A conversation cannot be empty.",
                context={"conversation_id": int(self.id)},
            )

        if self.updated_at < self.created_at:
            msg = (
                f"A conversation cannot be updated before it was created: "
                f"{self.updated_at} < {self.created_at}"
            )
            raise DomainValidationError(
                msg, user_message="That conversation has inconsistent dates."
            )

    # -- Construction -----------------------------------------------------

    @classmethod
    def spanning(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        conversation_id: ConversationId,
        account_id: AccountId,
        chat_id: ChatId,
        started_at: datetime,
        ended_at: datetime,
        message_count: int,
        now: datetime,
    ) -> Conversation:
        """Build a Conversation covering a run of messages.

        Named for what it is rather than for what created it: segmentation
        computes the span, and this records it.
        """
        return cls(
            id=conversation_id,
            account_id=account_id,
            chat_id=chat_id,
            started_at=started_at,
            ended_at=ended_at,
            message_count=message_count,
            created_at=now,
            updated_at=now,
        )

    # -- Derived state ----------------------------------------------------

    @property
    def duration(self) -> timedelta:
        """How long this episode lasted.

        Derived rather than stored: it is exactly ``ended_at - started_at``, and
        a stored copy could disagree with the two fields it comes from.
        """
        return self.ended_at - self.started_at

    @property
    def is_single_message(self) -> bool:
        """Whether this episode is one message on its own."""
        return self.message_count == 1

    def contains(self, sent_at: datetime) -> bool:
        """Whether a message sent at this instant belongs to this conversation.

        Inclusive at both ends, which is unambiguous because conversations
        within a chat do not overlap: a boundary requires a gap, so no instant
        can fall inside two of them.
        """
        return self.started_at <= sent_at <= self.ended_at

    def is_open_at(self, now: datetime, gap: timedelta) -> bool:
        """Whether a message arriving now would extend this conversation.

        Asked against a supplied instant rather than stored as a flag, because
        the answer changes with the passage of time and nothing would be running
        to correct a stored one (ADR-056).

        This says only that the *gap* has not elapsed. Whether this is also the
        newest conversation in its chat is the caller's question, and the
        repository is what answers it.
        """
        return now - self.ended_at <= gap

    # -- Transitions ------------------------------------------------------

    def spanning_now(
        self, *, started_at: datetime, ended_at: datetime, message_count: int, now: datetime
    ) -> Conversation:
        """Return this Conversation covering a different run of messages.

        What re-segmentation writes when a stored conversation is matched to a
        recomputed segment. Returns ``self`` when the segment is identical, so a
        rebuild that changes nothing does not move ``updated_at`` and make a
        no-op look like a change -- which is most of what "re-segmentation
        changes only what newly arrived messages require" means in practice.

        Raises:
            DomainValidationError: If the new span is not a span.
        """
        if (
            started_at == self.started_at
            and ended_at == self.ended_at
            and message_count == self.message_count
        ):
            return self
        return replace(
            self,
            started_at=started_at,
            ended_at=ended_at,
            message_count=message_count,
            updated_at=now,
        )


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That conversation has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That conversation has an invalid timestamp.")


__all__ = ["Conversation"]
