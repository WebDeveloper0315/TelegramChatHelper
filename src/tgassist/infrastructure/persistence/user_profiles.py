"""UserProfile mapper and repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update

from tgassist.domain.errors import DomainValidationError, RecordNotFoundError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.user_profile import (
    EmojiUsage,
    MessageLength,
    TimeRange,
    TonePreference,
    UserProfile,
)
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.domain.ports.user_profile_repository import UserProfileRepository
from tgassist.infrastructure.persistence.mapper import EntityMapper
from tgassist.infrastructure.persistence.mappers import from_stored_datetime
from tgassist.infrastructure.persistence.repository import Repository
from tgassist.infrastructure.persistence.schema import user_profiles
from tgassist.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork


class UserProfileMapper(EntityMapper[UserProfile]):
    """Converts between :class:`UserProfile` and its row."""

    def to_domain(self, row: Any) -> UserProfile:
        """Build a profile from a row."""
        created_at = from_stored_datetime(_as_iso(row.created_at))
        updated_at = from_stored_datetime(_as_iso(row.updated_at))
        if created_at is None or updated_at is None:  # pragma: no cover - schema forbids
            msg = "A user profile row is missing its timestamps"
            raise DomainValidationError(msg, user_message="That profile is incomplete.")

        return UserProfile(
            account_id=AccountId(row.account_id),
            primary_language=row.primary_language,
            tone_preference=TonePreference(row.tone_preference),
            preferred_message_length=MessageLength(row.preferred_message_length),
            emoji_usage=EmojiUsage(row.emoji_usage),
            quiet_hours=TimeRange(
                start_minute=row.quiet_hours_start_minute,
                end_minute=row.quiet_hours_end_minute,
            ),
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_params(self, entity: UserProfile) -> dict[str, Any]:
        """Build column values from a profile.

        Enumerations are stored as their string values rather than as ordinals.
        An ordinal is compact but silently changes meaning if a member is ever
        inserted in the middle of the enum, and it makes the stored data
        unreadable to anyone opening the file.
        """
        return {
            "account_id": int(entity.account_id),
            "primary_language": entity.primary_language,
            "tone_preference": entity.tone_preference.value,
            "preferred_message_length": entity.preferred_message_length.value,
            "emoji_usage": entity.emoji_usage.value,
            "quiet_hours_start_minute": entity.quiet_hours.start_minute,
            "quiet_hours_end_minute": entity.quiet_hours.end_minute,
            "created_at": entity.created_at,
            "updated_at": entity.updated_at,
        }


def _as_iso(value: Any) -> str | None:
    """Render a stored timestamp as ISO text, whichever form the driver returned."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class SqlUserProfileRepository(Repository[UserProfile], UserProfileRepository):
    """Stores the profile for one account.

    Scoped at construction. No method takes an account identifier, so there is
    no scope for a caller to get wrong, and every query this class issues is
    filtered by the account it was built for.
    """

    __slots__ = ("_account_id", "_mapper")

    def __init__(self, uow: SqlAlchemyUnitOfWork, account_id: AccountId) -> None:
        """Bind to a transaction and an account."""
        super().__init__(uow)
        self._account_id = account_id
        self._mapper = UserProfileMapper()

    @property
    def account_id(self) -> AccountId:
        """Return the account this repository is scoped to."""
        return self._account_id

    async def get(self) -> UserProfile | None:
        """Return this account's profile, or ``None`` if it has none yet."""
        row = await self.fetch_one(
            select(user_profiles).where(user_profiles.c.account_id == int(self._account_id)),
            operation="get_user_profile",
        )
        return self._mapper.to_domain(row) if row is not None else None

    async def add(self, profile: UserProfile) -> None:
        """Persist a new profile."""
        self._require_own(profile, operation="add")
        await self.execute_write(
            insert(user_profiles).values(self._mapper.to_params(profile)),
            operation="add_user_profile",
            conflict_message="That account already has a profile.",
        )

    async def update(self, profile: UserProfile) -> None:
        """Replace this account's profile.

        Raises:
            RecordNotFoundError: If the account has no profile.
        """
        self._require_own(profile, operation="update")
        params = self._mapper.to_params(profile)
        # created_at belongs to the original row; an update must not rewrite it,
        # or a profile would appear to have been created when it was last edited.
        params.pop("created_at", None)
        params.pop("account_id", None)

        result = await self.execute_write(
            update(user_profiles)
            .where(user_profiles.c.account_id == int(self._account_id))
            .values(**params),
            operation="update_user_profile",
        )
        if result.rowcount == 0:
            msg = f"Account {int(self._account_id)} has no profile to update"
            raise RecordNotFoundError(
                msg,
                user_message="That account has no profile yet.",
                context={"account_id": int(self._account_id)},
            )

    def _require_own(self, profile: UserProfile, *, operation: str) -> None:
        """Refuse a profile belonging to a different account.

        The scope makes cross-account *reads* impossible, but a caller could
        still hand this repository an entity built for another account. Writing
        it would silently overwrite the wrong row, so it is refused here rather
        than trusted.
        """
        if profile.account_id != self._account_id:
            msg = (
                f"Cannot {operation} a profile for account {int(profile.account_id)} "
                f"through a repository scoped to account {int(self._account_id)}"
            )
            raise DomainValidationError(
                msg,
                user_message="That profile belongs to a different account.",
                context={
                    "scope": int(self._account_id),
                    "profile_account": int(profile.account_id),
                },
            )


def user_profile_repository(uow: UnitOfWork, account_id: AccountId) -> SqlUserProfileRepository:
    """Build a profile repository scoped to one account.

    Matches ``ScopedRepositoryFactory``, so a use case can declare it as a
    dependency and supply the account once, inside its transaction.

    Raises:
        TypeError: If given a unit of work this repository cannot enlist in.
    """
    if not isinstance(uow, SqlAlchemyUnitOfWork):
        msg = f"SqlUserProfileRepository requires a SqlAlchemyUnitOfWork, got {type(uow).__name__}"
        raise TypeError(msg)
    return SqlUserProfileRepository(uow, account_id)


__all__ = [
    "SqlUserProfileRepository",
    "UserProfileMapper",
    "user_profile_repository",
]
