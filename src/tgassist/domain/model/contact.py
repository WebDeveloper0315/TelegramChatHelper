"""The Contact aggregate.

A Contact is a person the operator communicates with, known to exactly one
Account. It is the first aggregate describing somebody *other than* the user,
and the anchor every later aggregate hangs from: memories are about a contact,
goals are pursued with a contact, a private chat belongs to a contact.

Identity
--------

The primary key is a locally generated ``ContactId``, not the Telegram user
identifier. Three reasons, set out in full in ADR-041:

* The same person can be known to two Accounts, so ``telegram_user_id`` is not
  unique in this table -- only ``(account_id, telegram_user_id)`` is. A natural
  key would therefore have to be that pair, and every child table would carry
  both columns in its foreign key.
* A foreign system would own our identifiers. Telegram's identifier space is
  theirs to change; ours is not.
* Not every contact will necessarily come from Telegram. Nothing depends on that
  today, so it is the weakest of the three, but it costs nothing to keep open.

``(account_id, telegram_user_id)`` remains unique, enforced by an index rather
than by the key.

Lifecycle
---------

``active ⇄ archived``, and either state may become ``deleted``, from which
:meth:`Contact.restored` returns it to active::

    active ⇄ archived
      ↓  ↘     ↓
        deleted  → (restored) → active

Archived and deleted are **mutually exclusive**, expressed as two nullable
timestamps of which at most one is ever set. A timestamp rather than a boolean
because retention needs to know *when*: "purge contacts deleted more than ninety
days ago" cannot be asked of a boolean.

``DOMAIN_MODEL.md`` version 1.0 gave the lifecycle
``discovered → active → dormant → archived → deleted``. Two of those states are
not implemented, for reasons rather than by omission:

* ``discovered`` is indistinguishable from ``active`` until synchronisation
  exists to discover somebody. No behaviour differs between them today, and a
  state that changes nothing is a column that will be wrong.
* ``dormant`` is derived from ``last_seen_at`` and a configured window, and the
  document already says it is not stored. Neither the field nor the window
  exists yet.

Fields deferred
---------------

Documented attributes not implemented here, each with the reason and each one
additive migration away:

* ``first_name`` / ``last_name`` -- ``display_name`` is derived from them, so
  storing all three stores the same fact twice. They matter for salutation
  generation (Milestone 8), which is where the rule for choosing between them
  will live.
* ``phone_number_hash`` -- storing a salted hash requires choosing the salt
  strategy, and per-contact random salt and a global secret salt have different
  security properties. That choice belongs with the code that first receives a
  phone number.
* ``language`` -- nothing reads it. ``UserProfile.primary_language`` is what
  reply generation consults today; a per-contact language becomes meaningful
  when the reply generator can act on it.
* ``country`` / ``timezone`` -- Telegram supplies neither, and deriving them
  from a phone number is a guess. Reply timing (Milestone 9) is the consumer,
  and it should decide how the value is obtained.
* ``is_blocked`` -- distinct from archived: archived means "out of my way",
  blocked means "never process this person". Nothing processes anybody yet, so
  the distinction has no observable effect. Worth adding the moment suggestion
  generation exists.
* ``notes`` -- overlaps the Memory aggregate (Milestone 5), which exists to hold
  what the operator knows about a contact, with provenance and revision history.
  A free-text field would become the place people put things Memory should hold.
* ``first_seen_at`` / ``last_seen_at`` -- written by synchronisation from message
  timestamps. Until messages exist, ``first_seen_at`` would duplicate
  ``created_at`` and ``last_seen_at`` would never change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    ContactId,
    TelegramUserId,
    require_positive_identifier,
)

MAX_DISPLAY_NAME_LENGTH: Final = 128

MIN_USERNAME_LENGTH: Final = 5
MAX_USERNAME_LENGTH: Final = 32

_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,30}[A-Za-z0-9]$")
"""Telegram's username rule: 5-32 characters, letter first, no trailing underscore.

Validated structurally, not against Telegram. A username that cannot exist is
worth refusing at the point of entry; whether a well-formed one is currently
taken is a question only Telegram can answer, and its answer changes.
"""


def validate_username(value: str) -> str:
    """Return a normalised Telegram username.

    A leading ``@`` is stripped, because users type it and it is display
    punctuation rather than part of the name. Surrounding whitespace goes for
    the same reason it does on a display name: a value differing only by
    whitespace is the same value, and keeping both would defeat comparison.

    Raises:
        DomainValidationError: If the username cannot be a Telegram username.
    """
    cleaned = value.strip().removeprefix("@").strip()
    if not cleaned:
        msg = "A username cannot be blank"
        raise DomainValidationError(
            msg,
            user_message="A username cannot be blank. Omit it instead.",
        )
    if not _USERNAME.match(cleaned):
        msg = f"{cleaned!r} is not a well-formed Telegram username"
        raise DomainValidationError(
            msg,
            user_message=(
                f"{cleaned!r} is not a valid Telegram username. Use "
                f"{MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} letters, digits or "
                f"underscores, starting with a letter."
            ),
            context={"username": cleaned},
        )
    return cleaned


@dataclass(frozen=True, slots=True)
class Contact:
    """A person known to exactly one Account.

    Immutable, like every entity here: state changes return a new instance, so a
    Contact held by one component cannot change underneath it. There is no
    identity map (ADR-035), which makes a mutable entity the difference between
    "the contact I read" and "the contact in the database".

    Attributes:
        id: Local identifier, assigned by the caller rather than the database,
            so a Contact can be fully constructed and validated before it is
            saved.
        account_id: The Account that knows this person. A Contact belongs to
            exactly one; the same person known to two Accounts is two Contacts,
            because what is remembered about them differs per account.
        telegram_user_id: Identifier assigned by Telegram. Unique within an
            account, not across the table.
        username: Telegram ``@handle`` without the ``@``, or ``None``. Genuinely
            optional -- many Telegram users have never set one -- which is why
            this is the aggregate's only nullable non-lifecycle column.
        display_name: Human-readable label. Required: a contact nobody can
            recognise is not usable in a list.
        archived_at: When the operator archived this contact, or ``None``.
        deleted_at: When the operator deleted this contact, or ``None``.
            Deletion is soft: history is preserved until the purge in
            ``PRIVACY.md`` section 7 removes it.
        created_at: When this Contact was first recorded, UTC.
        updated_at: When it last changed, UTC.
    """

    id: ContactId
    account_id: AccountId
    telegram_user_id: TelegramUserId
    username: str | None
    display_name: str
    archived_at: datetime | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        In the entity rather than only in the schema, so an invalid Contact
        cannot exist in memory -- not merely cannot be saved. A check that runs
        only at the database boundary reports the problem at the end of a use
        case, far from the code that caused it.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="Contact id")
        require_positive_identifier(self.account_id, name="Account id")
        require_positive_identifier(self.telegram_user_id, name="Telegram user id")

        if not self.display_name.strip():
            msg = "A contact requires a non-empty display name"
            raise DomainValidationError(msg, user_message="A display name is required.")
        if len(self.display_name) > MAX_DISPLAY_NAME_LENGTH:
            msg = (
                f"A contact display name may be at most "
                f"{MAX_DISPLAY_NAME_LENGTH} characters, got {len(self.display_name)}"
            )
            raise DomainValidationError(
                msg,
                user_message=(
                    f"A display name may be at most {MAX_DISPLAY_NAME_LENGTH} characters."
                ),
            )

        if self.username is not None:
            validate_username(self.username)

        for value, name in (
            (self.created_at, "created_at"),
            (self.updated_at, "updated_at"),
            (self.archived_at, "archived_at"),
            (self.deleted_at, "deleted_at"),
        ):
            if value is not None:
                _require_utc(value, name=name)

        if self.updated_at < self.created_at:
            msg = (
                f"A contact cannot be updated before it was created: "
                f"{self.updated_at} < {self.created_at}"
            )
            raise DomainValidationError(msg, user_message="That contact has inconsistent dates.")

        if self.archived_at is not None and self.deleted_at is not None:
            msg = "A contact cannot be archived and deleted at the same time"
            raise DomainValidationError(
                msg,
                user_message="A contact cannot be both archived and deleted.",
                context={"contact_id": int(self.id)},
            )

    # -- Construction -----------------------------------------------------

    @classmethod
    def create(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        contact_id: ContactId,
        account_id: AccountId,
        telegram_user_id: TelegramUserId,
        display_name: str,
        now: datetime,
        username: str | None = None,
    ) -> Contact:
        """Build a new Contact, active from the start.

        Args:
            contact_id: Local identifier, from the identifier generator.
            account_id: The Account that knows this person.
            telegram_user_id: Identifier assigned by Telegram.
            display_name: Human-readable label. Surrounding whitespace is
                trimmed.
            now: Current instant, from the injected clock.
            username: Telegram handle, with or without a leading ``@``.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        return cls(
            id=contact_id,
            account_id=account_id,
            telegram_user_id=telegram_user_id,
            username=validate_username(username) if username is not None else None,
            display_name=display_name.strip(),
            archived_at=None,
            deleted_at=None,
            created_at=now,
            updated_at=now,
        )

    # -- Derived state ----------------------------------------------------

    @property
    def is_archived(self) -> bool:
        """Whether the operator has archived this contact."""
        return self.archived_at is not None

    @property
    def is_deleted(self) -> bool:
        """Whether the operator has deleted this contact."""
        return self.deleted_at is not None

    @property
    def is_active(self) -> bool:
        """Whether this contact is neither archived nor deleted."""
        return self.archived_at is None and self.deleted_at is None

    @property
    def status(self) -> str:
        """A one-word description of the lifecycle state, for display."""
        if self.deleted_at is not None:
            return "deleted"
        if self.archived_at is not None:
            return "archived"
        return "active"

    # -- State transitions ------------------------------------------------

    def archived(self, now: datetime) -> Contact:
        """Return this Contact archived.

        Returns ``self`` when already archived, so archiving twice does not move
        ``updated_at`` and make a no-op look like a change.

        Raises:
            DomainValidationError: If the contact is deleted. Archiving a
                deleted contact is not a state this model has; restore it first,
                which makes the intent explicit rather than inferred.
        """
        if self.deleted_at is not None:
            msg = f"Contact {int(self.id)} is deleted and cannot be archived"
            raise DomainValidationError(
                msg,
                user_message="That contact is deleted. Restore it before archiving it.",
                context={"contact_id": int(self.id)},
            )
        if self.archived_at is not None:
            return self
        return replace(self, archived_at=now, updated_at=now)

    def deleted(self, now: datetime) -> Contact:
        """Return this Contact soft-deleted.

        Deletion supersedes archiving, so ``archived_at`` is cleared: the two
        states are mutually exclusive, and leaving both set would mean a
        restored contact silently returned to the archive.

        Returns ``self`` when already deleted.
        """
        if self.deleted_at is not None:
            return self
        return replace(self, archived_at=None, deleted_at=now, updated_at=now)

    def restored(self, now: datetime) -> Contact:
        """Return this Contact active, from either archived or deleted.

        One method rather than two, because both states answer the same
        question -- "make this contact ordinary again" -- and a caller who has
        to know which state a contact is in before it can restore it has been
        handed the model's problem.

        Returns ``self`` when already active.
        """
        if self.is_active:
            return self
        return replace(self, archived_at=None, deleted_at=None, updated_at=now)

    def renamed(self, display_name: str, now: datetime) -> Contact:
        """Return this Contact with a new display name.

        Raises:
            DomainValidationError: If the new name is empty or too long.
        """
        cleaned = display_name.strip()
        if cleaned == self.display_name:
            return self
        return replace(self, display_name=cleaned, updated_at=now)

    def with_username(self, username: str | None, now: datetime) -> Contact:
        """Return this Contact with a new Telegram handle, or none.

        Raises:
            DomainValidationError: If the username is not well-formed.
        """
        cleaned = validate_username(username) if username is not None else None
        if cleaned == self.username:
            return self
        return replace(self, username=cleaned, updated_at=now)


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That contact has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That contact has an invalid timestamp.")


__all__ = [
    "MAX_DISPLAY_NAME_LENGTH",
    "MAX_USERNAME_LENGTH",
    "MIN_USERNAME_LENGTH",
    "Contact",
    "validate_username",
]
