"""The session repository, run against both implementations.

Same shape as the user profile contract suite, and for the same reason: one row
per account, so there is nothing to page. What is asserted here is ownership,
foreign-key integrity, cascade deletion and scope isolation — plus the one
obligation specific to this table, that it never holds key material.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.fakes.session_repository import (
    InMemorySessionRepository,
    InMemorySessionStore,
)
from tgassist.domain.errors import (
    ConstraintViolationError,
    DomainValidationError,
    RecordNotFoundError,
)
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.session import AuthorizationState, ConnectionState, Session
from tgassist.domain.ports.session_repository import SessionRepository
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqlAccountRepository,
    SqlAlchemyUnitOfWork,
    SqliteDatabase,
    SqlSessionRepository,
)

EPOCH = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ACCOUNT_A = AccountId(1)
ACCOUNT_B = AccountId(2)
ABSENT_ACCOUNT = AccountId(999)


def make_account(account_id: AccountId, *, is_active: bool = False) -> Account:
    """Build an account to own a session."""
    return Account.create(
        account_id=account_id,
        telegram_user_id=TelegramUserId(1000 + int(account_id)),
        display_name=f"account-{int(account_id)}",
        now=EPOCH,
        is_active=is_active,
    )


def make_session(account_id: AccountId) -> Session:
    """Build a freshly prepared session for an account."""
    return Session.prepare(
        account_id=account_id,
        session_path=Path("sessions") / str(int(account_id)),
        encryption_key_ref=f"telegram-session-key-{int(account_id)}",
        now=EPOCH,
    )


@dataclass
class SessionSubject:
    """One repository implementation, plus the means to set up its world."""

    for_account: object
    delete_account: object
    label: str


@pytest.fixture
async def sql_subject(tmp_path: Path) -> AsyncIterator[SessionSubject]:
    """The SQL repository against a migrated database with two accounts."""
    database = SqliteDatabase(DatabaseSection(path=tmp_path / "sessions.db"))
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
        yield SessionSubject(
            for_account=lambda account_id: SqlSessionRepository(uow, account_id),
            delete_account=delete_account,
            label="sql",
        )
    finally:
        await uow.rollback()
        await database.close()


@pytest.fixture
async def memory_subject() -> SessionSubject:
    """The in-memory repository against a shared store with two accounts."""
    store = InMemorySessionStore(known_accounts={int(ACCOUNT_A), int(ACCOUNT_B)})

    async def delete_account(account_id: AccountId) -> None:
        store.delete_account(account_id)

    return SessionSubject(
        for_account=lambda account_id: InMemorySessionRepository(store, account_id),
        delete_account=delete_account,
        label="memory",
    )


@pytest.fixture(params=["sql", "memory"])
def subject(request: pytest.FixtureRequest) -> SessionSubject:
    """Both implementations."""
    name = "sql_subject" if request.param == "sql" else "memory_subject"
    resolved: SessionSubject = request.getfixturevalue(name)
    return resolved


def repo(subject: SessionSubject, account_id: AccountId) -> SessionRepository:
    """Build a repository scoped to an account."""
    built: SessionRepository = subject.for_account(account_id)  # type: ignore[operator]
    return built


class TestSessionRepositoryContract:
    """Obligations both implementations must satisfy."""

    def test_satisfies_the_port(self, subject: SessionSubject) -> None:
        assert isinstance(repo(subject, ACCOUNT_A), SessionRepository)

    def test_exposes_its_scope(self, subject: SessionSubject) -> None:
        assert repo(subject, ACCOUNT_A).account_id == ACCOUNT_A

    async def test_absent_session_returns_none(self, subject: SessionSubject) -> None:
        # An ordinary state: a session is written when login is first prepared,
        # not when the account is created.
        assert await repo(subject, ACCOUNT_A).get() is None

    async def test_stored_session_can_be_read_back(self, subject: SessionSubject) -> None:
        sessions = repo(subject, ACCOUNT_A)
        session = make_session(ACCOUNT_A)

        await sessions.add(session)

        assert await sessions.get() == session

    async def test_reads_are_snapshots_not_live_views(self, subject: SessionSubject) -> None:
        sessions = repo(subject, ACCOUNT_A)
        await sessions.add(make_session(ACCOUNT_A))

        first = await sessions.get()
        second = await sessions.get()

        assert first == second
        assert first is not second

    async def test_a_second_session_is_refused(self, subject: SessionSubject) -> None:
        # Exactly one session per account, enforced by the primary key.
        sessions = repo(subject, ACCOUNT_A)
        await sessions.add(make_session(ACCOUNT_A))

        with pytest.raises(ConstraintViolationError):
            await sessions.add(make_session(ACCOUNT_A))

    async def test_update_records_both_axes(self, subject: SessionSubject) -> None:
        sessions = repo(subject, ACCOUNT_A)
        await sessions.add(make_session(ACCOUNT_A))
        later = EPOCH + timedelta(minutes=5)
        changed = (
            make_session(ACCOUNT_A)
            .with_authorization(AuthorizationState.READY, later)
            .with_connection(ConnectionState.READY, later)
        )

        await sessions.update(changed)
        found = await sessions.get()

        assert found is not None
        assert found.authorization_state is AuthorizationState.READY
        assert found.connection_state is ConnectionState.READY
        assert found.can_send

    async def test_authorized_but_reconnecting_survives_a_round_trip(
        self, subject: SessionSubject
    ) -> None:
        # The state a single enum could not express, and the reason for ADR-049.
        sessions = repo(subject, ACCOUNT_A)
        later = EPOCH + timedelta(minutes=5)
        await sessions.add(
            make_session(ACCOUNT_A)
            .with_authorization(AuthorizationState.READY, later)
            .with_connection(ConnectionState.CONNECTING, later)
        )

        found = await sessions.get()

        assert found is not None
        assert found.is_authorized
        assert not found.is_connected
        assert not found.can_send

    async def test_optional_fields_round_trip(self, subject: SessionSubject) -> None:
        sessions = repo(subject, ACCOUNT_A)
        later = EPOCH + timedelta(minutes=5)
        session = (
            make_session(ACCOUNT_A)
            .with_connection(ConnectionState.READY, later)
            .with_client_version("1.8.66", later)
            .with_activity(later)
        )
        await sessions.add(session)

        found = await sessions.get()

        assert found is not None
        assert found.client_version == "1.8.66"
        assert found.connected_at == later
        assert found.last_activity_at == later

    async def test_nullable_fields_stay_null(self, subject: SessionSubject) -> None:
        sessions = repo(subject, ACCOUNT_A)
        await sessions.add(make_session(ACCOUNT_A))

        found = await sessions.get()

        assert found is not None
        assert found.client_version is None
        assert found.connected_at is None
        assert found.last_activity_at is None

    async def test_update_preserves_the_creation_time(self, subject: SessionSubject) -> None:
        sessions = repo(subject, ACCOUNT_A)
        await sessions.add(make_session(ACCOUNT_A))
        later = EPOCH + timedelta(days=1)
        changed = make_session(ACCOUNT_A).with_authorization(AuthorizationState.WAITING_CODE, later)

        await sessions.update(changed)
        found = await sessions.get()

        assert found is not None
        assert found.created_at == EPOCH
        assert found.updated_at == later

    async def test_update_without_a_session_raises(self, subject: SessionSubject) -> None:
        with pytest.raises(RecordNotFoundError):
            await repo(subject, ACCOUNT_A).update(make_session(ACCOUNT_A))

    async def test_logging_out_is_a_transition_not_a_deletion(
        self, subject: SessionSubject
    ) -> None:
        # The record survives, because "this account was signed out" is a fact
        # a deleted row cannot express.
        sessions = repo(subject, ACCOUNT_A)
        later = EPOCH + timedelta(hours=1)
        await sessions.add(
            make_session(ACCOUNT_A)
            .with_authorization(AuthorizationState.READY, later)
            .with_connection(ConnectionState.READY, later)
        )

        await sessions.update((await sessions.get()).logged_out(later))  # type: ignore[union-attr]
        found = await sessions.get()

        assert found is not None
        assert found.authorization_state is AuthorizationState.LOGGED_OUT
        assert found.connection_state is ConnectionState.OFFLINE
        assert found.connected_at is None

    def test_the_repository_cannot_delete(self) -> None:
        # Structural: a session goes with its account, by cascade. Nothing else
        # removes one, so there is no method that could.
        assert not hasattr(SqlSessionRepository, "delete")
        assert not hasattr(InMemorySessionRepository, "delete")


class TestAccountOwnership:
    """A session belongs to exactly one account, and cannot be misfiled."""

    async def test_a_foreign_session_cannot_be_added(self, subject: SessionSubject) -> None:
        # Writing it would point one account's record at another account's
        # encrypted store.
        with pytest.raises(DomainValidationError, match="scoped to account") as excinfo:
            await repo(subject, ACCOUNT_A).add(make_session(ACCOUNT_B))

        assert "different account" in excinfo.value.user_message

    async def test_a_foreign_session_cannot_be_updated(self, subject: SessionSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_session(ACCOUNT_A))

        with pytest.raises(DomainValidationError, match="scoped to account") as excinfo:
            await repo(subject, ACCOUNT_A).update(make_session(ACCOUNT_B))

        assert "different account" in excinfo.value.user_message

    async def test_a_session_for_a_missing_account_is_refused(
        self, subject: SessionSubject
    ) -> None:
        # Foreign key integrity: a session cannot exist without its owner.
        with pytest.raises(ConstraintViolationError):
            await repo(subject, ABSENT_ACCOUNT).add(make_session(ABSENT_ACCOUNT))


class TestScopeIsolation:
    """Two scoped repositories over the same storage never see each other's data."""

    async def test_one_account_does_not_see_another(self, subject: SessionSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_session(ACCOUNT_A))

        assert await repo(subject, ACCOUNT_B).get() is None

    async def test_each_account_reads_its_own(self, subject: SessionSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_session(ACCOUNT_A))
        await repo(subject, ACCOUNT_B).add(make_session(ACCOUNT_B))

        a = await repo(subject, ACCOUNT_A).get()
        b = await repo(subject, ACCOUNT_B).get()

        assert a is not None
        assert b is not None
        assert a.encryption_key_ref != b.encryption_key_ref
        assert a.session_path != b.session_path

    async def test_updating_one_leaves_the_other_untouched(self, subject: SessionSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_session(ACCOUNT_A))
        await repo(subject, ACCOUNT_B).add(make_session(ACCOUNT_B))
        later = EPOCH + timedelta(minutes=1)

        await repo(subject, ACCOUNT_A).update(
            make_session(ACCOUNT_A).with_authorization(AuthorizationState.READY, later)
        )

        untouched = await repo(subject, ACCOUNT_B).get()
        assert untouched is not None
        assert untouched.authorization_state is AuthorizationState.UNAUTHORIZED

    async def test_the_repository_takes_no_account_argument(self) -> None:
        # The structural guarantee: no method accepts a scope, so there is no
        # value for a caller to get wrong (ADR-039).
        for name in ("get", "add", "update"):
            signature = inspect.signature(getattr(SqlSessionRepository, name))
            assert "account_id" not in signature.parameters


class TestCascadeDeletion:
    """Deleting an account removes its session."""

    async def test_deleting_an_account_removes_its_session(self, subject: SessionSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_session(ACCOUNT_A))

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get() is None

    async def test_deleting_an_account_leaves_others_alone(self, subject: SessionSubject) -> None:
        await repo(subject, ACCOUNT_A).add(make_session(ACCOUNT_A))
        await repo(subject, ACCOUNT_B).add(make_session(ACCOUNT_B))

        await subject.delete_account(ACCOUNT_A)  # type: ignore[operator]

        assert await repo(subject, ACCOUNT_A).get() is None
        assert await repo(subject, ACCOUNT_B).get() is not None


class TestImplementationsAgree:
    """Both implementations behave identically."""

    async def test_prepared_sessions_round_trip_identically(
        self, sql_subject: SessionSubject, memory_subject: SessionSubject
    ) -> None:
        session = make_session(ACCOUNT_A)

        await repo(sql_subject, ACCOUNT_A).add(session)
        await repo(memory_subject, ACCOUNT_A).add(session)

        assert await repo(sql_subject, ACCOUNT_A).get() == (
            await repo(memory_subject, ACCOUNT_A).get()
        )

    async def test_fully_populated_sessions_round_trip_identically(
        self, sql_subject: SessionSubject, memory_subject: SessionSubject
    ) -> None:
        later = EPOCH + timedelta(minutes=30)
        session = (
            make_session(ACCOUNT_A)
            .with_authorization(AuthorizationState.READY, later)
            .with_connection(ConnectionState.UPDATING, later)
            .with_client_version("1.8.66", later)
            .with_activity(later)
        )

        await repo(sql_subject, ACCOUNT_A).add(session)
        await repo(memory_subject, ACCOUNT_A).add(session)

        from_sql = await repo(sql_subject, ACCOUNT_A).get()
        from_memory = await repo(memory_subject, ACCOUNT_A).get()

        assert from_sql == from_memory
        assert from_sql is not None
        assert from_sql.session_path == session.session_path
