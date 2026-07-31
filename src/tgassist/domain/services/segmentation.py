"""Where one conversation ends and the next begins.

A pure function over stored messages. No AI, no configuration lookup, no clock:
everything it needs arrives as an argument, and the same arguments always give
the same answer. That is not a stylistic preference -- it is the whole of why
re-segmentation is safe to run at any time.

The rule
--------

A new segment begins when either holds:

1. **The gap since the previous message exceeds ``gap``.** Six hours by default
   (``DOMAIN_MODEL.md`` section 5.7). A period of silence is the only signal
   available without reading the messages, and it is the one a person would use.
2. **The current segment has reached ``max_messages``.** Two hundred by default.
   This is not a semantic boundary -- an exchange does not stop being one
   exchange at message two hundred and one -- it is a bound on how much context
   a later AI feature can be asked to hold. It is included because an unbounded
   segment makes every downstream token budget unbounded too.

Why this rule and not another
-----------------------------

*Calendar day* would cut an evening conversation in half at midnight, and would
need a timezone this application does not know: a contact's zone is not
something Telegram reports.

*Sender changes* would make an ordinary back-and-forth into dozens of
conversations, which is the opposite of what the unit is for.

*Telegram reply chains* are not stored -- ``Message`` has no
``reply_to_message_id``, deliberately -- and most messages are not replies. A
rule that used them would also splice a reply to a month-old message onto that
old episode, joining two things a person would never call one conversation.

*Any hybrid of the above* multiplies the cases in which a rebuild's answer
depends on something other than what is stored, which is precisely the property
this module exists to guarantee.

Determinism rests on the ordering
---------------------------------

Messages are ordered by ``(sent_at, telegram_message_id, id)``, and every part
of that key is immutable once stored. ``sent_at`` alone is not a total order --
two messages can share a second -- so a tiebreak is needed, and it must not be
insertion order: a backfill stores an *older* message *later*, so ordering by
local identifier alone would put history in the sequence it happened to be
fetched in. Telegram's own identifier is monotonic within a chat, which is the
true order for anything Telegram issued; the local identifier settles the rest.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.message import Message

#: How long a silence has to be before it separates two conversations.
#: ``DOMAIN_MODEL.md`` section 5.7; configured at
#: ``conversation.gap_minutes``.
DEFAULT_GAP_MINUTES: Final = 360

#: How many messages one conversation may hold before the next begins.
#: A bound on downstream context, not a claim about meaning.
DEFAULT_MAX_MESSAGES: Final = 200


@dataclass(frozen=True, slots=True)
class SegmentationRules:
    """What decides a boundary.

    A value object rather than two loose arguments, so the pair travels together
    and a caller cannot supply one and forget the other.

    Attributes:
        gap: Silence longer than this begins a new conversation.
        max_messages: A conversation holds at most this many messages.
    """

    gap: timedelta = timedelta(minutes=DEFAULT_GAP_MINUTES)
    max_messages: int = DEFAULT_MAX_MESSAGES

    def __post_init__(self) -> None:
        """Validate the rules.

        Raises:
            DomainValidationError: If either value would make segmentation
                meaningless.
        """
        if self.gap <= timedelta(0):
            msg = f"A conversation gap must be positive, got {self.gap}"
            raise DomainValidationError(
                msg, user_message="The conversation gap must be longer than nothing."
            )
        if self.max_messages < 1:
            msg = f"A conversation holds at least one message, got {self.max_messages}"
            raise DomainValidationError(
                msg, user_message="A conversation must be allowed at least one message."
            )

    def separates(self, earlier: datetime, later: datetime) -> bool:
        """Whether the silence between two messages begins a new conversation.

        Strictly greater than: a gap of *exactly* the threshold continues the
        conversation. The choice matters only at the boundary, and it is made
        here rather than at the comparison site so that both directions of the
        test have one place to read it.
        """
        return later - earlier > self.gap


@dataclass(frozen=True, slots=True)
class Segment:
    """One run of messages the rule considers a single conversation.

    Not a :class:`~tgassist.domain.model.conversation.Conversation`: this is the
    shape of an episode, with no identity. Giving it one is the application's
    work, and it is where stability across re-segmentation is decided (ADR-056).

    Attributes:
        messages: The run, in order, never empty.
    """

    messages: tuple[Message, ...]

    def __post_init__(self) -> None:
        """Refuse an empty segment.

        Raises:
            DomainValidationError: If the segment holds no messages.
        """
        if not self.messages:
            msg = "A segment is a run of messages and cannot be empty"
            raise DomainValidationError(msg, user_message="A conversation cannot be empty.")

    @property
    def started_at(self) -> datetime:
        """When the first message in this run was sent."""
        return self.messages[0].sent_at

    @property
    def ended_at(self) -> datetime:
        """When the last message in this run was sent."""
        return self.messages[-1].sent_at

    @property
    def message_count(self) -> int:
        """How many messages this run holds."""
        return len(self.messages)


def ordering_key(message: Message) -> tuple[datetime, int, int]:
    """Return the total order segmentation reads messages in.

    Every component is immutable once stored, which is what makes the order --
    and therefore every boundary derived from it -- reproducible.

    ``sent_at`` first, because that is what a conversation *is* about.
    ``telegram_message_id`` second, because Telegram's identifiers are monotonic
    within a chat and are therefore the true order of anything it issued; zero
    for a message that has none, so those sort before Telegram's at the same
    instant rather than being interleaved unpredictably. The local identifier
    last, so the order is total even for two keyboard-typed messages sharing a
    second.
    """
    return (
        message.sent_at,
        int(message.telegram_message_id) if message.telegram_message_id is not None else 0,
        int(message.id),
    )


def in_order(messages: Iterable[Message]) -> tuple[Message, ...]:
    """Return messages in segmentation order.

    Sorted here rather than trusted from the caller. A repository returns rows
    in whatever order its index gives, and a rule whose answer depended on that
    would be reproducible only by accident.
    """
    return tuple(sorted(messages, key=ordering_key))


def segment(messages: Sequence[Message], rules: SegmentationRules) -> tuple[Segment, ...]:
    """Divide a run of messages into conversations.

    Args:
        messages: The chat's messages. Order is not assumed; they are sorted.
        rules: What decides a boundary.

    Returns:
        The segments, in order, each non-empty and contiguous. Together they
        contain every message given, exactly once -- which is what "every
        message belongs to exactly one Conversation" means, and it holds by
        construction rather than by check.
    """
    ordered = in_order(messages)
    if not ordered:
        return ()

    segments: list[Segment] = []
    current: list[Message] = [ordered[0]]

    for message in ordered[1:]:
        if rules.separates(current[-1].sent_at, message.sent_at) or len(current) >= (
            rules.max_messages
        ):
            segments.append(Segment(messages=tuple(current)))
            current = [message]
            continue
        current.append(message)

    segments.append(Segment(messages=tuple(current)))
    return tuple(segments)


__all__ = [
    "DEFAULT_GAP_MINUTES",
    "DEFAULT_MAX_MESSAGES",
    "Segment",
    "SegmentationRules",
    "in_order",
    "ordering_key",
    "segment",
]
