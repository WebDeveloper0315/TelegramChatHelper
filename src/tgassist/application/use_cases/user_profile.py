"""UserProfile use cases.

Two operations. Retrieval creates a default profile on first access, so
"creation" is not a step the user has to know about — adding an account should
not require deciding preferences before the application is usable.
"""

from __future__ import annotations

from dataclasses import dataclass

from tgassist.domain.errors import RecordNotFoundError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.user_profile import (
    EmojiUsage,
    MessageLength,
    TimeRange,
    TonePreference,
    UserProfile,
)
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWorkFactory
from tgassist.domain.ports.user_profile_repository import UserProfileRepository


@dataclass(frozen=True, slots=True)
class ProfileChanges:
    """The subset of preferences a caller wants to change.

    ``None`` means "leave as it is", which is distinct from any value a
    preference can take. Modelling the change set separately from the entity is
    what allows a partial update without inventing a null-means-unset convention
    in the entity itself.
    """

    primary_language: str | None = None
    tone_preference: TonePreference | None = None
    preferred_message_length: MessageLength | None = None
    emoji_usage: EmojiUsage | None = None
    quiet_hours: TimeRange | None = None

    @property
    def is_empty(self) -> bool:
        """Report whether the caller asked for nothing."""
        return all(
            value is None
            for value in (
                self.primary_language,
                self.tone_preference,
                self.preferred_message_length,
                self.emoji_usage,
                self.quiet_hours,
            )
        )


class GetUserProfile:
    """Returns an account's profile, creating a default one on first access."""

    __slots__ = ("_accounts", "_clock", "_profiles", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        profiles: ScopedRepositoryFactory[UserProfileRepository],
        accounts: RepositoryFactory[AccountRepository],
        clock: Clock,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._profiles = profiles
        self._accounts = accounts
        self._clock = clock

    async def execute(self, account_id: AccountId | None = None) -> UserProfile:
        """Return the profile, creating it with defaults if absent.

        Args:
            account_id: Account to read. ``None`` uses the active account, which
                is what a caller usually wants.

        Raises:
            RecordNotFoundError: If no account matches, or if none is active.
                A profile cannot exist without an account to own it, so this is
                reported rather than silently returning nothing.
        """
        async with self._unit_of_work() as uow:
            resolved = await self._resolve_account(uow, account_id)
            profiles = self._profiles(uow, resolved)

            existing = await profiles.get()
            if existing is not None:
                return existing

            profile = UserProfile.default_for(resolved, self._clock.now())
            await profiles.add(profile)
            await uow.commit()

        return profile

    async def _resolve_account(self, uow: object, account_id: AccountId | None) -> AccountId:
        accounts = self._accounts(uow)  # type: ignore[arg-type]
        if account_id is None:
            active = await accounts.get_active()
            if active is None:
                msg = "No account is active"
                raise RecordNotFoundError(
                    msg,
                    user_message="No account is active. Create one first.",
                )
            return active.id

        account = await accounts.get(account_id)
        if account is None:
            msg = f"No account with identifier {int(account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That account was not found.",
                context={"account_id": int(account_id)},
            )
        return account.id


class UpdateUserProfile:
    """Applies a partial change to an account's profile."""

    __slots__ = ("_clock", "_get", "_profiles", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        profiles: ScopedRepositoryFactory[UserProfileRepository],
        get_profile: GetUserProfile,
        clock: Clock,
    ) -> None:
        """Take the collaborators this use case actually needs."""
        self._unit_of_work = unit_of_work
        self._profiles = profiles
        self._get = get_profile
        self._clock = clock

    async def execute(
        self, changes: ProfileChanges, account_id: AccountId | None = None
    ) -> UserProfile:
        """Apply the requested changes and return the resulting profile.

        Changing nothing is permitted and returns the profile unchanged, because
        a caller that supplied no options has made no mistake worth an error.

        Raises:
            RecordNotFoundError: If no account matches, or none is active.
            DomainValidationError: If a value violates an invariant.
        """
        # Reading through the same use case guarantees the profile exists, so
        # `profile set` works on a fresh account without a separate init step.
        current = await self._get.execute(account_id)
        updated = self._apply(current, changes)
        if updated is current:
            return current

        async with self._unit_of_work() as uow:
            await self._profiles(uow, current.account_id).update(updated)
            await uow.commit()

        return updated

    def _apply(self, profile: UserProfile, changes: ProfileChanges) -> UserProfile:
        """Apply each requested change in turn.

        Each ``with_`` method returns the profile unchanged when the value
        matches, so a request that sets everything to its current value leaves
        ``updated_at`` alone.
        """
        now = self._clock.now()
        result = profile
        if changes.primary_language is not None:
            result = result.with_language(changes.primary_language, now)
        if changes.tone_preference is not None:
            result = result.with_tone(changes.tone_preference, now)
        if changes.preferred_message_length is not None:
            result = result.with_message_length(changes.preferred_message_length, now)
        if changes.emoji_usage is not None:
            result = result.with_emoji_usage(changes.emoji_usage, now)
        if changes.quiet_hours is not None:
            result = result.with_quiet_hours(changes.quiet_hours, now)
        return result
