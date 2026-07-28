"""The Chat aggregate.

A Chat is a container for communication: the edge that connects an Account to a
Contact. Account and Contact are the graph's nodes; without Chat there is no
structure joining them, and every system planned on top of this one attaches
here or reaches a Contact through here.

* Synchronisation is per-chat. ``telegram_chat_id`` is what a sync run resolves,
  and ``sync_enabled`` is what decides whether it runs at all.
* Message ingestion writes ``chat_id``. Messages have no other home.
* Memory and goals anchor on a Contact, which for a private chat is reachable
  from the chat that reaches them.
* ``ai_processing_mode`` is the privacy gate every future AI feature reads
  (ADR-024). It exists here because "stop using AI on our chats" is a per-chat
  right in ``PRIVACY.md`` section 7.

None of those systems is implemented. This aggregate is the structure they
attach to, and it is useful on its own: a user can record who they talk to and
decide what may be done with each conversation.

Deliberately not implemented
----------------------------

* ``Conversation`` -- a segment of a chat, bounded by message timestamps. There
  are no messages, so there is nothing to segment and its ``started_at`` would
  have no source. It arrives with Milestone 3.
* ``SyncCursor`` -- per-chat synchronisation bookmarks. That is sync state, and
  it belongs with the code that advances it.

Fields deferred
---------------

Documented attributes not implemented here, each one additive migration away:

* ``last_message_at`` -- written by ingestion. Until messages exist it would be
  null on every row, and an index on it would order nothing. Listings sort by
  ``created_at`` until then.
* ``is_muted`` -- Telegram notification state, mirrored by synchronisation.
  Nothing notifies.
* ``is_archived`` -- a third way to hide something, after archiving a contact
  and disabling a chat's synchronisation. What a user controls about a chat
  today is whether it is synced and what AI may do with it; both are here.
* ``retention_days`` -- nullable, meaning "inherit the global policy". There is
  no global policy to inherit until Milestone 10, and a column whose null has a
  meaning nobody has defined is a column every reader will define differently.
* ``deleted_at`` -- a chat exists because a conversation exists in Telegram. The
  user does not delete chats; they stop syncing them, or purge the contact
  (``PRIVACY.md`` section 7), which removes the chat by cascade.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ChatId,
    ContactId,
    TelegramChatId,
    require_positive_identifier,
)

MAX_TITLE_LENGTH: Final = 256


class ChatType(StrEnum):
    """What kind of Telegram container this is.

    All five are modelled although Milestone 1 supports only private chats
    (``PROJECT_SPEC.md`` section 12). Enabling group support later is then an
    application change rather than a migration -- and, more immediately, the
    ``private`` invariants below are only meaningful if a non-private chat is
    representable.
    """

    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"
    SAVED = "saved"


class AiProcessingMode(StrEnum):
    """What this application may do with a chat's content.

    The privacy gate (ADR-024). Ordered by increasing exposure, and defaulting
    to ``LOCAL_ONLY``: content stays on the device unless the user decides
    otherwise, per chat, which is what makes "don't send our messages to a cloud
    service" a setting rather than a promise.
    """

    DISABLED = "disabled"
    LOCAL_ONLY = "local_only"
    CLOUD_ALLOWED = "cloud_allowed"


@dataclass(frozen=True, slots=True)
class Chat:
    """A communication container belonging to one Account.

    Immutable, like every entity here: state changes return a new instance.

    Attributes:
        id: Local identifier, assigned by the caller rather than the database.
            Locally generated for the same reasons a Contact's is (ADR-041).
        account_id: The Account this chat belongs to.
        telegram_chat_id: Identifier assigned by Telegram. Unique within an
            account. **May be negative** -- Telegram numbers groups and channels
            below zero -- so the only structural check is that it is not zero.
        chat_type: What kind of container this is.
        contact_id: The person on the other side, for a private chat. ``None``
            for every other type, and never ``None`` for a private one.
        title: The container's name, for a group, channel or saved-messages
            chat. ``None`` for a private chat, whose name is the contact's
            display name -- stored once, on the Contact.
        sync_enabled: Whether synchronisation should ingest this chat.
        ai_processing_mode: What may be done with this chat's content.
        created_at: When this Chat was first recorded, UTC.
        updated_at: When it last changed, UTC.
    """

    id: ChatId
    account_id: AccountId
    telegram_chat_id: TelegramChatId
    chat_type: ChatType
    contact_id: ContactId | None
    title: str | None
    sync_enabled: bool
    ai_processing_mode: AiProcessingMode
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="Chat id")
        require_positive_identifier(self.account_id, name="Account id")

        if self.telegram_chat_id == 0:
            msg = "A Telegram chat identifier cannot be zero"
            raise DomainValidationError(msg, user_message="That is not a valid chat identifier.")

        if self.contact_id is not None:
            require_positive_identifier(self.contact_id, name="Contact id")

        # The two halves of the same rule. A private chat is with exactly one
        # person and has no name of its own; every other kind has a name and no
        # single person. Stating both directions is what stops a private chat
        # existing with nobody in it.
        is_private = self.chat_type is ChatType.PRIVATE
        if is_private and self.contact_id is None:
            msg = "A private chat requires the contact it is with"
            raise DomainValidationError(
                msg, user_message="A private chat must name the contact it is with."
            )
        if not is_private and self.contact_id is not None:
            msg = f"A {self.chat_type.value} chat cannot name a single contact"
            raise DomainValidationError(
                msg,
                user_message="Only a private chat can name a single contact.",
                context={"chat_type": self.chat_type.value},
            )
        if is_private and self.title is not None:
            msg = "A private chat has no title of its own; the contact's name is its name"
            raise DomainValidationError(msg, user_message="A private chat cannot have a title.")
        if not is_private:
            if self.title is None or not self.title.strip():
                msg = f"A {self.chat_type.value} chat requires a title"
                raise DomainValidationError(msg, user_message="That kind of chat needs a title.")
            if len(self.title) > MAX_TITLE_LENGTH:
                msg = (
                    f"A chat title may be at most {MAX_TITLE_LENGTH} characters, "
                    f"got {len(self.title)}"
                )
                raise DomainValidationError(
                    msg, user_message=f"A title may be at most {MAX_TITLE_LENGTH} characters."
                )

        _require_utc(self.created_at, name="created_at")
        _require_utc(self.updated_at, name="updated_at")
        if self.updated_at < self.created_at:
            msg = (
                f"A chat cannot be updated before it was created: "
                f"{self.updated_at} < {self.created_at}"
            )
            raise DomainValidationError(msg, user_message="That chat has inconsistent dates.")

    # -- Construction -----------------------------------------------------

    @classmethod
    def private_with(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        chat_id: ChatId,
        account_id: AccountId,
        telegram_chat_id: TelegramChatId,
        contact_id: ContactId,
        now: datetime,
        sync_enabled: bool = True,
        ai_processing_mode: AiProcessingMode = AiProcessingMode.LOCAL_ONLY,
    ) -> Chat:
        """Build a private chat with one contact.

        A separate constructor per kind, rather than one taking every field,
        because the two kinds have genuinely different required arguments: a
        private chat needs a contact and refuses a title, and the reverse holds
        for the rest. Encoding that in the signatures means the impossible
        combinations cannot be written, not merely that they are rejected.

        Defaults follow ADR-024: content stays on the device unless the user
        decides otherwise. Synchronisation defaults to on because storing
        history locally is the application's purpose and nothing leaves the
        device by doing it.
        """
        return cls(
            id=chat_id,
            account_id=account_id,
            telegram_chat_id=telegram_chat_id,
            chat_type=ChatType.PRIVATE,
            contact_id=contact_id,
            title=None,
            sync_enabled=sync_enabled,
            ai_processing_mode=ai_processing_mode,
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def group_titled(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        chat_id: ChatId,
        account_id: AccountId,
        telegram_chat_id: TelegramChatId,
        chat_type: ChatType,
        title: str,
        now: datetime,
        sync_enabled: bool = True,
        ai_processing_mode: AiProcessingMode = AiProcessingMode.LOCAL_ONLY,
    ) -> Chat:
        """Build a chat that has a title rather than a single counterpart.

        Raises:
            DomainValidationError: If ``chat_type`` is ``PRIVATE``, which has a
                constructor of its own, or if the title is empty.
        """
        if chat_type is ChatType.PRIVATE:
            msg = "A private chat is built with private_with, which requires a contact"
            raise DomainValidationError(
                msg, user_message="A private chat must name the contact it is with."
            )
        return cls(
            id=chat_id,
            account_id=account_id,
            telegram_chat_id=telegram_chat_id,
            chat_type=chat_type,
            contact_id=None,
            title=title.strip(),
            sync_enabled=sync_enabled,
            ai_processing_mode=ai_processing_mode,
            created_at=now,
            updated_at=now,
        )

    # -- Derived state ----------------------------------------------------

    @property
    def is_private(self) -> bool:
        """Whether this chat is with exactly one person."""
        return self.chat_type is ChatType.PRIVATE

    @property
    def allows_ai(self) -> bool:
        """Whether any AI processing is permitted for this chat."""
        return self.ai_processing_mode is not AiProcessingMode.DISABLED

    @property
    def allows_cloud_ai(self) -> bool:
        """Whether content from this chat may leave the device.

        A separate question from :attr:`allows_ai`, and the one that matters for
        the promise in ``PRIVACY.md`` section 7. Asked as a property so no
        caller has to remember which enum members are permissive.
        """
        return self.ai_processing_mode is AiProcessingMode.CLOUD_ALLOWED

    # -- State transitions ------------------------------------------------

    def with_sync_enabled(self, enabled: bool, now: datetime) -> Chat:
        """Return this Chat with synchronisation on or off.

        Returns ``self`` when unchanged, so a redundant call does not move
        ``updated_at`` and make a no-op look like a change.
        """
        if enabled == self.sync_enabled:
            return self
        return replace(self, sync_enabled=enabled, updated_at=now)

    def with_ai_processing_mode(self, mode: AiProcessingMode, now: datetime) -> Chat:
        """Return this Chat with a new AI processing mode. A no-op if unchanged."""
        if mode is self.ai_processing_mode:
            return self
        return replace(self, ai_processing_mode=mode, updated_at=now)

    def retitled(self, title: str, now: datetime) -> Chat:
        """Return this Chat with a new title.

        Raises:
            DomainValidationError: If this is a private chat, whose name belongs
                to its contact, or if the title is empty or too long.
        """
        if self.is_private:
            msg = "A private chat has no title of its own; rename the contact instead"
            raise DomainValidationError(
                msg,
                user_message="A private chat takes its name from the contact. Rename them instead.",
            )
        cleaned = title.strip()
        if cleaned == self.title:
            return self
        return replace(self, title=cleaned, updated_at=now)


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That chat has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That chat has an invalid timestamp.")


__all__ = [
    "MAX_TITLE_LENGTH",
    "AiProcessingMode",
    "Chat",
    "ChatType",
]
