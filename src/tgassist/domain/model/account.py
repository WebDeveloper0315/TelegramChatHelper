"""The Account aggregate.

An Account is the Telegram account this application acts on behalf of, and the
**ownership root** for everything else: every chat, contact, memory, goal and
audit event carries an ``account_id``. That is why it is the first aggregate
built -- its shape constrains every table that follows, so getting it wrong is
expensive in a way that getting a later aggregate wrong is not.

Deliberately narrow
-------------------

An Account holds only what belongs to *the owner of the data*. It does not hold
authentication state: whether a connection is established, whether a code has
been requested, whether two-factor is pending. Those belong to ``Session``,
which models them as an explicit state machine (``DOMAIN_MODEL.md`` section 5.3).

``DOMAIN_MODEL.md`` version 1.0 gave Account the lifecycle
``created → authenticating → active → suspended → logged_out → deleted``, which
overlaps Session's states while providing only an ``is_active`` boolean to
express them. Implementing it made the duplication concrete: two entities would
have owned "is this account authenticated", and they would eventually disagree.
Account therefore keeps the lifecycle it genuinely owns -- whether the user has
selected it -- and authentication remains Session's. See ADR-037.

Fields deferred
---------------

Two documented fields are not implemented yet, because nothing writes them until
Telegram authentication exists and both involve a decision better made with the
consumer in front of us:

* ``phone_number_hash`` -- storing a salted hash requires choosing the salt
  strategy, and per-account random salt and a global secret salt have different
  security properties. That choice belongs with the code that first receives a
  phone number, not two milestones ahead of it.
* ``last_authenticated_at`` -- written only by authentication.

Both are one additive migration away.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    TelegramUserId,
    require_positive_identifier,
)

MAX_DISPLAY_NAME_LENGTH: Final = 128
DEFAULT_TIMEZONE: Final = "UTC"


def validate_timezone(name: str) -> str:
    """Return ``name`` if it is a resolvable IANA timezone identifier.

    Identifiers rather than fixed offsets, because an offset cannot express
    daylight saving. Storing ``+01:00`` records what the offset was on the day
    it was written and is wrong for half of every subsequent year -- which
    matters here, since reply-timing advice is computed against the contact's
    local hours.

    Raises:
        ValueError: If the identifier does not resolve.
    """
    if not name:
        msg = "A timezone identifier is required"
        raise DomainValidationError(msg, user_message="A timezone is required.")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        msg = f"{name!r} is not a known IANA timezone identifier"
        raise DomainValidationError(
            msg,
            user_message=f"{name!r} is not a recognised timezone.",
            context={"timezone": name},
            cause=exc,
        ) from exc
    return name


@dataclass(frozen=True, slots=True)
class Account:
    """A Telegram account this application acts on behalf of.

    Immutable. State changes return a new instance, so an Account held by one
    component cannot change underneath it -- which matters because there is no
    identity map (ADR-035) and a mutable entity would make "the account I read"
    and "the account in the database" silently different things.

    Attributes:
        id: Local identifier. Assigned by the caller, not the database, so an
            Account can be fully constructed and validated before it is saved.
        telegram_user_id: Identifier assigned by Telegram. Unique per account.
        display_name: Human-readable label, shown wherever accounts are listed.
        timezone: IANA identifier. The default zone for interpreting local
            times when a contact's own zone is unknown.
        is_active: Whether this is the account the application is operating.
            Exactly one Account is active at a time in version 1.0, enforced by
            a partial unique index rather than by convention.
        created_at: When this Account was first recorded, UTC.
        updated_at: When it last changed, UTC.
    """

    id: AccountId
    telegram_user_id: TelegramUserId
    display_name: str
    timezone: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Validation lives here rather than only in the schema, so an invalid
        Account cannot exist in memory -- not merely cannot be saved. A check
        that runs only at the database boundary discovers the problem at the
        end of a use case, far from the code that caused it.

        Raises:
            ValueError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="Account id")
        require_positive_identifier(self.telegram_user_id, name="Telegram user id")

        if not self.display_name.strip():
            msg = "An account requires a non-empty display name"
            raise DomainValidationError(msg, user_message="A display name is required.")
        if len(self.display_name) > MAX_DISPLAY_NAME_LENGTH:
            msg = (
                f"An account display name may be at most "
                f"{MAX_DISPLAY_NAME_LENGTH} characters, got {len(self.display_name)}"
            )
            raise DomainValidationError(
                msg,
                user_message=(
                    f"A display name may be at most {MAX_DISPLAY_NAME_LENGTH} characters."
                ),
            )

        validate_timezone(self.timezone)

        _require_utc(self.created_at, name="created_at")
        _require_utc(self.updated_at, name="updated_at")
        if self.updated_at < self.created_at:
            msg = (
                f"An account cannot be updated before it was created: "
                f"{self.updated_at} < {self.created_at}"
            )
            raise DomainValidationError(msg, user_message="That account has inconsistent dates.")

    # -- Construction -----------------------------------------------------

    @classmethod
    def create(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        account_id: AccountId,
        telegram_user_id: TelegramUserId,
        display_name: str,
        now: datetime,
        timezone: str = DEFAULT_TIMEZONE,
        is_active: bool = False,
    ) -> Account:
        """Build a new Account.

        Args:
            account_id: Local identifier, from the identifier generator.
            telegram_user_id: Identifier assigned by Telegram.
            display_name: Human-readable label. Surrounding whitespace is
                trimmed, because a name that differs only by whitespace is the
                same name and would otherwise defeat comparison.
            now: Current instant, from the injected clock.
            timezone: IANA identifier.
            is_active: Whether this account is the one being operated.
        """
        return cls(
            id=account_id,
            telegram_user_id=telegram_user_id,
            display_name=display_name.strip(),
            timezone=timezone,
            is_active=is_active,
            created_at=now,
            updated_at=now,
        )

    # -- State transitions ------------------------------------------------

    def activated(self, now: datetime) -> Account:
        """Return this Account marked active.

        Returns ``self`` when already active, so that activating twice does not
        move ``updated_at`` and make a no-op look like a change.
        """
        if self.is_active:
            return self
        return replace(self, is_active=True, updated_at=now)

    def deactivated(self, now: datetime) -> Account:
        """Return this Account marked inactive. A no-op if already inactive."""
        if not self.is_active:
            return self
        return replace(self, is_active=False, updated_at=now)

    def renamed(self, display_name: str, now: datetime) -> Account:
        """Return this Account with a new display name.

        Raises:
            ValueError: If the new name is empty or too long.
        """
        cleaned = display_name.strip()
        if cleaned == self.display_name:
            return self
        return replace(self, display_name=cleaned, updated_at=now)


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That account has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That account has an invalid timestamp.")
