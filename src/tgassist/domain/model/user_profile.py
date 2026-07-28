"""The UserProfile aggregate.

Describes the operator: how they write and when they would rather not be
prompted. Exactly one per Account, owned by it, and deleted with it.

Its purpose in the architecture is as much structural as behavioural. It is the
first aggregate that belongs to an Account rather than being one, so it is where
foreign keys, cascade deletion and account-scoped querying are established.

Identity is the Account
-----------------------

``DOMAIN_MODEL.md`` version 1.0 gave UserProfile a surrogate ``id`` alongside
``account_id``. Implementing it showed the surrogate to be redundant: exactly one
profile exists per Account, so the account identifier already identifies the
profile uniquely. Using it as the primary key makes the one-per-account
invariant *the primary key* rather than a separate unique index that has to be
remembered and could be dropped. See ADR-038.

Fields deferred, and why
------------------------

Several documented attributes are not implemented. Two are duplicates and three
have no determinable shape yet:

* ``display_name`` and ``timezone`` -- Account already has both. A second name
  raises "which one is displayed"; a second timezone raises "which one
  interprets quiet hours". For a single-operator application these are the same
  person and the same zone, so Account owns them (ADR-038).
* ``available_hours`` -- storing it alongside ``quiet_hours`` permits the two to
  contradict each other (quiet 22:00-08:00 *and* available all day). Whether it
  is genuinely distinct from "not quiet" is a question the Behavior Engine can
  answer and this milestone cannot, so it waits for Milestone 9.
* ``auto_approve_memory_categories`` -- the category vocabulary belongs to
  Memory. Storing category names before the categories exist means storing
  strings that cannot be validated against anything.
* ``confidence_thresholds`` -- meaningless without the calibrator that reads
  them, and the calibrator arrives with Milestone 8.
* ``additional_languages`` -- consumed only by multilingual prompt selection.

Each is one additive migration away, and each has somewhere better to be
decided.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Self

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import AccountId, require_positive_identifier

MINUTES_PER_DAY: Final = 24 * 60
DEFAULT_LANGUAGE: Final = "en"
DEFAULT_QUIET_START: Final = 22 * 60
DEFAULT_QUIET_END: Final = 8 * 60

_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


class TonePreference(StrEnum):
    """The register the assistant should suggest replies in."""

    CASUAL = "casual"
    NEUTRAL = "neutral"
    FORMAL = "formal"
    MIRROR_CONTACT = "mirror_contact"
    """Adopt the contact's own style rather than a fixed register."""


class EmojiUsage(StrEnum):
    """How freely suggestions should use emoji."""

    NONE = "none"
    SPARING = "sparing"
    FREQUENT = "frequent"


class MessageLength(StrEnum):
    """How long suggested replies should tend to be."""

    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"


def validate_language(tag: str) -> str:
    """Return a normalised language tag, or raise if it is malformed.

    Structural validation only: the shape of a BCP-47 tag, not membership of the
    IANA subtag registry. Checking the registry would mean shipping and updating
    it, and a well-formed tag that is not registered is a far smaller problem
    than a malformed one -- it simply matches nothing.

    Raises:
        DomainValidationError: If the tag is empty or malformed.
    """
    cleaned = tag.strip()
    if not cleaned:
        msg = "A primary language is required"
        raise DomainValidationError(msg, user_message="A language is required.")
    if not _LANGUAGE_TAG.match(cleaned):
        msg = f"{tag!r} is not a well-formed language tag"
        raise DomainValidationError(
            msg,
            user_message=f"{tag!r} is not a valid language code. Try 'en' or 'en-GB'.",
            context={"language": tag},
        )
    parts = cleaned.split("-")
    return "-".join([parts[0].lower(), *(p.upper() for p in parts[1:])])


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A daily recurring range of local time, in minutes past midnight.

    Stored as minutes rather than as clock times because a range that wraps past
    midnight -- which quiet hours almost always do -- is awkward to compare as
    two times and trivial to compare as two integers.

    Attributes:
        start_minute: Inclusive start, 0 to 1439.
        end_minute: Exclusive end, 0 to 1439. A value below ``start_minute``
            means the range wraps past midnight.
    """

    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        """Validate the range.

        Raises:
            DomainValidationError: If either bound is outside a day, or if the
                two are equal. Equal bounds are ambiguous -- they could mean an
                empty range or a whole day -- and the documented invariant
                forbids covering the entire day, so the ambiguity is rejected
                rather than resolved arbitrarily.
        """
        for name, value in (("start", self.start_minute), ("end", self.end_minute)):
            if not 0 <= value < MINUTES_PER_DAY:
                msg = (
                    f"A time range {name} must be between 0 and {MINUTES_PER_DAY - 1}, got {value}"
                )
                raise DomainValidationError(msg, user_message="That time is not valid.")
        if self.start_minute == self.end_minute:
            msg = (
                "A time range cannot start and end at the same minute: it would be "
                "ambiguous between an empty range and the entire day"
            )
            raise DomainValidationError(
                msg, user_message="A time range must have a start different from its end."
            )

    @classmethod
    def from_clock(cls, start: str, end: str) -> Self:
        """Build a range from two ``HH:MM`` strings."""
        return cls(start_minute=_parse_clock(start), end_minute=_parse_clock(end))

    @property
    def wraps_midnight(self) -> bool:
        """Report whether this range crosses midnight."""
        return self.end_minute < self.start_minute

    @property
    def duration_minutes(self) -> int:
        """Return the length of the range in minutes."""
        if self.wraps_midnight:
            return MINUTES_PER_DAY - self.start_minute + self.end_minute
        return self.end_minute - self.start_minute

    def contains(self, minute_of_day: int) -> bool:
        """Report whether a minute past midnight falls inside the range."""
        if self.wraps_midnight:
            return minute_of_day >= self.start_minute or minute_of_day < self.end_minute
        return self.start_minute <= minute_of_day < self.end_minute

    def contains_local_time(self, moment: datetime) -> bool:
        """Report whether a local time falls inside the range.

        The caller converts to the operator's zone first. Passing an instant in
        the wrong zone produces a confidently wrong answer, which is why this
        takes a local time rather than a UTC instant.
        """
        return self.contains(moment.hour * 60 + moment.minute)

    def __str__(self) -> str:
        """Render the range as ``HH:MM-HH:MM``."""
        return f"{_format_clock(self.start_minute)}-{_format_clock(self.end_minute)}"


def _parse_clock(value: str) -> int:
    """Parse ``HH:MM`` into minutes past midnight."""
    cleaned = value.strip()
    if not re.match(r"^\d{1,2}:\d{2}$", cleaned):
        msg = f"{value!r} is not a time in HH:MM form"
        raise DomainValidationError(
            msg, user_message=f"{value!r} is not a valid time. Use HH:MM, for example 22:00."
        )
    hours, minutes = (int(part) for part in cleaned.split(":"))
    if hours >= 24 or minutes >= 60:  # noqa: PLR2004 - the clock, not a magic number
        msg = f"{value!r} is not a valid time of day"
        raise DomainValidationError(msg, user_message=f"{value!r} is not a valid time.")
    return hours * 60 + minutes


def _format_clock(minute_of_day: int) -> str:
    """Render minutes past midnight as ``HH:MM``."""
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


@dataclass(frozen=True, slots=True)
class UserProfile:
    """The operator's preferences for one Account.

    Immutable. Changes return a new instance, and each ``with_`` method returns
    ``self`` when the value is unchanged, so a redundant edit does not move
    ``updated_at``.

    Attributes:
        account_id: The owning Account. Also the identity: exactly one profile
            exists per Account, so a separate surrogate key would add nothing
            and weaken the invariant (ADR-038).
        primary_language: BCP-47 tag the operator writes in.
        tone_preference: Register for suggested replies.
        preferred_message_length: How long suggestions should tend to be.
        emoji_usage: How freely suggestions may use emoji.
        quiet_hours: When the operator would rather not be prompted to send.
            Interpreted in the Account's timezone.
        created_at: When the profile was created, UTC.
        updated_at: When it last changed, UTC.
    """

    account_id: AccountId
    primary_language: str
    tone_preference: TonePreference
    preferred_message_length: MessageLength
    emoji_usage: EmojiUsage
    quiet_hours: TimeRange
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity owns.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.account_id, name="Account id")
        validate_language(self.primary_language)

        if self.quiet_hours.duration_minutes >= MINUTES_PER_DAY:  # pragma: no cover
            # Unreachable while TimeRange rejects equal bounds; kept because the
            # invariant belongs to the profile and should not depend on a
            # neighbouring class continuing to enforce it.
            msg = "Quiet hours cannot cover the entire day"
            raise DomainValidationError(msg, user_message="Quiet hours cannot cover the whole day.")

        _require_utc(self.created_at, name="created_at")
        _require_utc(self.updated_at, name="updated_at")
        if self.updated_at < self.created_at:
            msg = (
                f"A profile cannot be updated before it was created: "
                f"{self.updated_at} < {self.created_at}"
            )
            raise DomainValidationError(msg, user_message="That profile has inconsistent dates.")

    @classmethod
    def default_for(cls, account_id: AccountId, now: datetime) -> UserProfile:
        """Build a profile with sensible defaults.

        Every field has a defensible default, which is what allows a profile to
        be created on first access rather than demanding a setup step before the
        application is usable.
        """
        return cls(
            account_id=account_id,
            primary_language=DEFAULT_LANGUAGE,
            tone_preference=TonePreference.NEUTRAL,
            preferred_message_length=MessageLength.MEDIUM,
            emoji_usage=EmojiUsage.SPARING,
            quiet_hours=TimeRange(DEFAULT_QUIET_START, DEFAULT_QUIET_END),
            created_at=now,
            updated_at=now,
        )

    # -- Changes ----------------------------------------------------------

    def with_language(self, language: str, now: datetime) -> UserProfile:
        """Return this profile with a different primary language."""
        normalised = validate_language(language)
        if normalised == self.primary_language:
            return self
        return replace(self, primary_language=normalised, updated_at=now)

    def with_tone(self, tone: TonePreference, now: datetime) -> UserProfile:
        """Return this profile with a different tone preference."""
        if tone is self.tone_preference:
            return self
        return replace(self, tone_preference=tone, updated_at=now)

    def with_message_length(self, length: MessageLength, now: datetime) -> UserProfile:
        """Return this profile with a different preferred message length."""
        if length is self.preferred_message_length:
            return self
        return replace(self, preferred_message_length=length, updated_at=now)

    def with_emoji_usage(self, usage: EmojiUsage, now: datetime) -> UserProfile:
        """Return this profile with a different emoji preference."""
        if usage is self.emoji_usage:
            return self
        return replace(self, emoji_usage=usage, updated_at=now)

    def with_quiet_hours(self, quiet_hours: TimeRange, now: datetime) -> UserProfile:
        """Return this profile with different quiet hours."""
        if quiet_hours == self.quiet_hours:
            return self
        return replace(self, quiet_hours=quiet_hours, updated_at=now)


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That profile has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That profile has an invalid timestamp.")
