"""The Message aggregate.

A single message: the immutable factual record everything else is derived from.
Memories cite one, summaries condense many, suggestions reply to the last, and
timing advice is computed from the intervals between them.

Immutability
------------

A Message has **no ``updated_at`` and no update path**. That is not an omission;
it is what "immutable factual record" means, and it is expressed structurally --
``MessageRepository`` has no ``update`` method, so there is no route by which a
stored message can change. ``ingested_at`` serves as the creation time.

``DOMAIN_MODEL.md`` section 5.6 names four eventual exceptions -- ``edited_at``,
``text`` on edit, ``is_deleted_remotely`` and ``deleted_at``. All four are
written by synchronisation, and how an edit is represented (a mutation, or a new
row superseding the old) is a decision that belongs with the code performing it.
Adding an update path now would settle that question by accident.

Identity
--------

The primary key is a locally generated ``MessageId``. ``telegram_message_id`` is
**optional**, because ingestion accepts messages from any source and only one of
those sources issues identifiers (ADR-045). Where it is present,
``(account_id, chat_id, telegram_message_id)`` is unique -- the idempotency
guarantee that makes re-synchronisation safe -- enforced by a *partial* index so
that many source-less messages remain permitted.

Content
-------

``text`` is conversation content: the most sensitive data this application
holds. It is redacted from logs by the sensitivity policy, and it is stored
locally only -- ``Chat.ai_processing_mode`` decides whether anything may be done
with it beyond storing it (ADR-024).

Fields deferred
---------------

Each is one additive migration away, and none has a reader or a writer yet:

* ``conversation_id`` -- Conversation does not exist (ADR-044).
* ``sender_telegram_user_id`` -- identifies *which* participant sent a message in
  a group. In a private chat ``sender_kind`` already answers it unambiguously,
  and groups are not synchronised.
* ``reply_to_message_id`` -- threading. A self-referential foreign key with its
  own deletion semantics, and nothing reads it until context assembly in
  Milestone 8.
* ``forwarded_from``, ``edited_at``, ``is_deleted_remotely`` -- written by
  synchronisation.
* ``deleted_at`` -- nothing deletes a message. Retention is Milestone 10, purge
  is Milestone 11, and remote-deletion mirroring is Milestone 3. Adding it now
  would put a filter nothing writes into every history query, and would settle
  the soft-versus-hard question a milestone before the code that has to answer
  it.
* ``is_outgoing`` -- **dropped**, not deferred. It is exactly
  ``sender_kind == operator``, and two owners of one fact eventually disagree.
  The document justifies storing it "because it is queried constantly"; that is
  an argument for an index on a derived column, which can be added when a
  measured query needs one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    MessageId,
    TelegramMessageId,
    require_positive_identifier,
)

MAX_TEXT_LENGTH: Final = 65_536
"""Telegram's own limit is far lower; this guards against a runaway import.

A message longer than this is not a message, and storing it would put an
unbounded value in the largest table in the system.
"""


class SenderKind(StrEnum):
    """Who sent a message.

    Three values rather than a boolean, because ``system`` -- Telegram's own
    service notices, "X joined the group" -- is neither the operator nor the
    contact, and treating it as either would put words in somebody's mouth.
    """

    OPERATOR = "operator"
    CONTACT = "contact"
    SYSTEM = "system"


class MessageType(StrEnum):
    """What kind of message this is.

    The full documented set, although only ``TEXT`` is producible today. It is
    the discriminator that makes ``text`` meaningfully optional -- a photo has no
    text, or has a caption -- and synchronisation will produce every one of these
    from its first run. Recording ``other`` for everything and recovering the
    distinction later would need a data migration; recording it correctly costs
    one check constraint.
    """

    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    VIDEO = "video"
    DOCUMENT = "document"
    STICKER = "sticker"
    LOCATION = "location"
    POLL = "poll"
    SERVICE = "service"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Message:
    """One message in one chat.

    Frozen, like every entity here, and unlike the others it has no transitions
    at all: there is nothing a Message becomes.

    Attributes:
        id: Local identifier, assigned by the caller rather than the database.
        account_id: The Account that owns this message.
        chat_id: The Chat it belongs to. Together with ``account_id`` this is a
            composite foreign key, so a message cannot be filed in another
            account's chat (ADR-043).
        telegram_message_id: Identifier assigned by Telegram, or ``None`` for a
            message from a source that issues none. Unique within its chat when
            present.
        sender_kind: Who sent it.
        message_type: What kind of message it is.
        text: The content, or ``None`` for a message that has none. Required for
            a text message; optional for every other kind, which may carry a
            caption or nothing.
        sent_at: When it was sent, as reported by its source, UTC.
        ingested_at: When this application stored it, UTC. Distinct from
            ``sent_at`` and both are required: a backfill ingests a message from
            years ago today, and conflating the two would make every timing
            analysis wrong and every sync diagnostic useless.
    """

    id: MessageId
    account_id: AccountId
    chat_id: ChatId
    telegram_message_id: TelegramMessageId | None
    sender_kind: SenderKind
    message_type: MessageType
    text: str | None
    sent_at: datetime
    ingested_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="Message id")
        require_positive_identifier(self.account_id, name="Account id")
        require_positive_identifier(self.chat_id, name="Chat id")

        if self.telegram_message_id is not None:
            require_positive_identifier(self.telegram_message_id, name="Telegram message id")

        if self.message_type is MessageType.TEXT and not (self.text or "").strip():
            msg = "A text message requires text"
            raise DomainValidationError(msg, user_message="A text message cannot be empty.")
        if self.text is not None and len(self.text) > MAX_TEXT_LENGTH:
            msg = f"Message text may be at most {MAX_TEXT_LENGTH} characters"
            raise DomainValidationError(msg, user_message="That message is too long to store.")

        _require_utc(self.sent_at, name="sent_at")
        _require_utc(self.ingested_at, name="ingested_at")
        # No rule that ingested_at >= sent_at. A message can arrive with a clock
        # skew, or with a sender's timestamp slightly ahead of ours, and
        # rejecting it would lose a real message over a fraction of a second.

    # -- Construction -----------------------------------------------------

    @classmethod
    def record(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        message_id: MessageId,
        account_id: AccountId,
        chat_id: ChatId,
        sender_kind: SenderKind,
        sent_at: datetime,
        ingested_at: datetime,
        text: str | None = None,
        message_type: MessageType = MessageType.TEXT,
        telegram_message_id: TelegramMessageId | None = None,
    ) -> Message:
        """Build a Message.

        One constructor, unlike Chat's two: every source produces the same
        shape, differing only in whether it supplies an external identifier.
        That is the point of the pipeline being source-agnostic.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        return cls(
            id=message_id,
            account_id=account_id,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            sender_kind=sender_kind,
            message_type=message_type,
            text=text,
            sent_at=sent_at,
            ingested_at=ingested_at,
        )

    # -- Derived state ----------------------------------------------------

    @property
    def is_outgoing(self) -> bool:
        """Whether the operator sent this message.

        Derived rather than stored. It is exactly ``sender_kind == operator``,
        and a stored copy could disagree with the field it is derived from.
        """
        return self.sender_kind is SenderKind.OPERATOR

    @property
    def has_external_identity(self) -> bool:
        """Whether this message can be recognised again on re-ingestion.

        A message without one is not a defect: it came from a source that issues
        no identifiers, so there is nothing to deduplicate against and every
        ingestion of it is a new message (ADR-045).
        """
        return self.telegram_message_id is not None

    @property
    def is_analysable(self) -> bool:
        """Whether this message has text an AI feature could work with.

        Asked as a property so no future caller has to decide for itself which
        message types carry usable content.
        """
        return bool((self.text or "").strip())


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That message has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That message has an invalid timestamp.")


__all__ = [
    "MAX_TEXT_LENGTH",
    "Message",
    "MessageType",
    "SenderKind",
]
