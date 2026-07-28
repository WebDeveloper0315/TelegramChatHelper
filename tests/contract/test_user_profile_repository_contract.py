"""The user profile repository, run against both implementations.

The Milestone 1.0 contract suite does not apply here: it assumes a collection
with pagination, and this repository holds exactly one row. What it *does* have
is the obligations that matter for every account-owned aggregate to come —
ownership, foreign-key integrity, cascade deletion and scope isolation — so
those are asserted against both implementations here.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.user_profile_repository import (
    InMemoryProfileStore,
    InMemoryUserProfileRepository,
)
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.user_profile import TimeRange, TonePreference, UserProfile
from tgassist.domain.ports.user_profile_repository import UserProfileRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAlchemyUnitOfWork,
    SqliteDatabase,
    SqlUserProfileRepository,
)

EPOCH = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
ABSENT_ACCOUNT = AccountId(999)


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own a profile."""
    return Account.create(
        account_id=account_id,
        telegram_user_id=TelegramUserId(1000 + int(account_id)),
        display_name=f"account-{int(account_id)}",
        now=EPOCH,
        is_active=is_active,
    )


def make_profile(
    account_id: AccountId, *, tone: TonePreference = TonePreference.NEUTRAL
) -> UserProfile:
    """Build a profile for an account."""
    return UserProfile.default_for(account_id, EPOCH).with_tone(tone, EPOCH)


@dataclass
class ProfileSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_account: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[ProfileSubject]:
    """The SQL repository against a migrated database with two accounts."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "profiles.db"))
    await database.connect()
    await AlembicMigrationRunner(database).upgrade()

    uow = SqlAlchemyUnitOfWork(database)
    await uow.begin()
    accounts = SqlAccountRepository(uow)
    await accounts.add(make_account(ACCOUNT_A, is_active=True))
    await accounts.add(make_account(ACCOUNT_B))

    async def delete_account(account_id: AccountId) -> None:
        await database.executor.run(
            lambda: uow.connection.execute(
                text("DELETE FROM accounts WHERE id = :id"), {"id": int(account_id)}
            )
        )

    try:
        yield ProfileSubject(
            for_account=lambda account_id: SqlUserProfileRepository(uow, account_id),
            delete_account=delete_account,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> ProfileSubject:
    """The in-memory repository against a shared store with two accounts."""
    store = InMemoryProfileStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})

    async def delete_account(account_id: AccountId) -> None:
        store.delete_account(account_id)

    return ProfileSubject(
        for_account=lambda account_id: InMemoryUserProfileRepository(store, account_id),
        delete_account=delete_account,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> ProfileSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: ProfileSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: ProfileSubject, account_id: AccountId) -> UserProfileRepository:
    """Build a repository scoped to an account."""
    built: UserProfileRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestUserProfileRepositoryContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: ProfileSubject) -> None:
        assert isinstance(repo(subject, ACCOUNT_A), UserProfileRepository)

    def test_exposes_its_scope(self, subject: ProfileSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    async def test_absent_profile_returns_none(self, subject: ProfileSubject) -> None:
        # An ordinary state: a profile is created on first access, not with the
        # account.
        assert await repo(subject, ACCOUNT_A).get() is None

    async def test_stored_profile_can_be_read_back(self, subject: ProfileSubject) -> None:
        profiles = repo(subject, ACCOUNT_A)
        profile = make_profile(ACCOUNT_A)

        await profiles.add(profile)

        assert await profiles.get() == profile

    async def test_reads_are_snapshots_not_live_views(self, subject: ProfileSubject) -> None:
        profiles = repo(subject, ACCOUNT_A)
        await profiles.add(make_profile(ACCOUNT_A))

        first = await profiles.get()
        second = await profiles.get()

        assert first == second
        assert first is not second

    async def test_a_second_profile_is_refused(self, subject: ProfileSubject) -> None:
        # Exactly one profile per account, enforced by the primary key.
        profiles = repo(subject, ACCOUNT_A)
        await profiles.add(make_profile(ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await profiles.add(make_profile(ACCOUNT_A))

    async def test_update_replaces_the_profile(self, subject: ProfileSubject) -> None:
        profiles = repo(subject, ACCOUNT_A)
        await profiles.add(make_profile(ACCOUNT_A))
        changed = make_profile(ACCOUNT_A, tone=TonePreference.FORMAL)

        await profiles.update(changed)
        found = await profiles.get()

        assert found is not None
        assert found.tone_preference is TonePreference.FORMAL

    async def test_update_preserves_the_creation_time(self, subject: ProfileSubject) -> None:
        # Otherwise a profile would appear to have been created when it was
        # last edited.
        profiles = repo(subject, ACCOUNT_A)
        await profiles.add(make_profile(ACCOUNT_A))
        later = EPOCH + timedelta(days=1)
        changed = make_profile(ACCOUNT_A).with_tone(TonePreference.CASUAL, later)

        await profiles.update(changed)
        found = await profiles.get()

        assert found is not None
        assert found.created_at == EPOCH
        assert found.updated_at == later

    async def test_update_without_a_profile_raises(self, subject: ProfileSubject) -> None:
        with pytest.raises(RecordNotFoundError):
            await repo(subject, ACCOUNT_A).update(make_profile(ACCOUNT_A))


class TestAccountOwnership:
    """A profile belongs to exactly one account, and cannot be misfiled."""

    async def test_a_foreign_profile_cannot_be_added(self, subject: ProfileSubject) -> None:
        # The scope prevents cross-account reads; this prevents a caller from
        # handing over an entity built for someone else, which would overwrite
        # the wrong row.
        with pytest.raises(DomainValidationError, match="scoped to account") as excinfo:
            await repo(subject, ACCOUNT_A).add(make_profile(ACCOUNT_B))

        assert "different account" in excinfo.value.user_message

    async def test_a_foreign_profile_cannot_be_updated(self, subject: ProfileSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_profile(ACCOUNT_A))

        with pytest.raises(DomainValidationError, match="scoped to account") as excinfo:
            await repo(subject, ACCOUNT_A).update(make_profile(ACCOUNT_B))

        assert "different account" in excinfo.value.user_message

    async def test_a_profile_for_a_missing_account_is_refused(
        self, subject: ProfileSubject
    ) -> None:
        # Foreign key integrity: a profile cannot exist without its owner.
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ABSENT_ACCOUNT).add(make_profile(ABSENT_ACCOUNT))


class TestScopeIsolation:
    """Two scoped repositories over the same storage never see each other's data."""

    async def test_one_account_does_not_see_another(self, subject: ProfileSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_profile(ACCOUNT_A))

        assert await repo(subject, ACCOUNT_B).get() is None

    async def test_each_account_reads_its_own(self, subject: ProfileSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_profile(ACCOUNT_A, tone=TonePreference.CASUAL))
        await repo(subject, ACCOUNT_B).add(make_profile(ACCOUNT_B, tone=TonePreference.FORMAL))

        a = await repo(subject, ACCOUNT_A).get()
        b = await repo(subject, ACCOUNT_B).get()

        assert a is not None
        assert b is not None
        assert a.tone_preference is TonePreference.CASUAL
        assert b.tone_preference is TonePreference.FORMAL

    async def test_updating_one_leaves_the_other_untouched(self, subject: ProfileSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_profile(ACCOUNT_A))
        await repo(subject, ACCOUNT_B).add(make_profile(ACCOUNT_B))

        await repo(subject, ACCOUNT_A).update(make_profile(ACCOUNT_A, tone=TonePreference.FORMAL))

        untouched = await repo(subject, ACCOUNT_B).get()
        assert untouched is not None
        assert untouched.tone_preference is TonePreference.NEUTRAL

    async def test_the_repository_takes_no_account_argument(self) -> None:
        # The structural guarantee: no method accepts a scope, so there is no
        # value for a caller to get wrong (ADR-039).
        for name in ("get", "add", "update"):
            signature = inspect.signature(getattr(SqlUserProfileRepository, name))
            assert "account_id" not in signature.parameters


class TestCascadeDeletion:
    """Deleting an account removes its profile."""

    async def test_deleting_an_account_removes_its_profile(self, subject: ProfileSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_profile(ACCOUNT_A))

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get() is None

    async def test_deleting_an_account_leaves_others_alone(self, subject: ProfileSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_profile(ACCOUNT_A))
        await repo(subject, ACCOUNT_B).add(make_profile(ACCOUNT_B))

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get() is None
        assert await repo(subject, ACCOUNT_B).get() is not None


class TestImplementationsAgree:
    """Both implementations behave identically."""

    async def test_defaults_round_trip_identically(
        self, sql_subject: ProfileSubject, memory_subject: ProfileSubject
    ) -> None:
        profile = UserProfile.default_for(ACCOUNT_A, EPOCH)

        await repo(sql_subject, ACCOUNT_A).add(profile)
        await repo(memory_subject, ACCOUNT_A).add(profile)

        assert await repo(sql_subject, ACCOUNT_A).get() == (
            await repo(memory_subject, ACCOUNT_A).get()
        )

    async def test_wrapping_quiet_hours_round_trip_identically(
        self, sql_subject: ProfileSubject, memory_subject: ProfileSubject
    ) -> None:
        profile = UserProfile.default_for(ACCOUNT_A, EPOCH).with_quiet_hours(
            TimeRange.from_clock("23:30", "06:45"), EPOCH
        )

        await repo(sql_subject, ACCOUNT_A).add(profile)
        await repo(memory_subject, ACCOUNT_A).add(profile)

        from_sql = await repo(sql_subject, ACCOUNT_A).get()
        from_memory = await repo(memory_subject, ACCOUNT_A).get()

        assert from_sql == from_memory
        assert from_sql is not None
        assert from_sql.quiet_hours.wraps_midnight
