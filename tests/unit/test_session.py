"""Session aggregate, mapper and migration tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.model.session import (
    CONNECTED_STATES,
    MAX_KEY_REF_LENGTH,
    AuthorizationState,
    ConnectionState,
    Session,
)
from tgassist.infrastructure.config import DatabaseSection
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SessionMapper,
    SqliteDatabase,
)
from tgassist.infrastructure.persistence.mapper import column_names
from tgassist.infrastructure.persistence.schema import TELEGRAM_SESSIONS_TABLE, telegram_sessions

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
ACCOUNT = AccountId(7)
PATH = Path("sessions") / "7"
KEY_REF = "telegram-session-key-7"


def prepared(**overrides: Any) -> Session:
    """A freshly prepared session, with optional field overrides."""
    session = Session.prepare(
        account_id=ACCOUNT, session_path=PATH, encryption_key_ref=KEY_REF, now=NOW
    )
    return replace(session, **overrides) if overrides else session


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestSessionPreparation:
    def test_starts_unauthorized_and_offline(self) -> None:
        # The state authentication starts from: somewhere to put the store and
        # a key to encrypt it with. Everything else is what logging in creates.
        session = prepared()

        assert session.authorization_state is AuthorizationState.UNAUTHORIZED
        assert session.connection_state is ConnectionState.OFFLINE

    def test_records_where_the_store_lives_and_which_key_opens_it(self) -> None:
        session = prepared()

        assert session.session_path == PATH
        assert session.encryption_key_ref == KEY_REF

    def test_has_no_history_yet(self) -> None:
        session = prepared()

        assert session.client_version is None
        assert session.connected_at is None
        assert session.last_activity_at is None

    def test_is_created_and_updated_at_the_same_instant(self) -> None:
        session = prepared()

        assert session.created_at == NOW
        assert session.updated_at == NOW

    def test_the_account_is_the_identity(self) -> None:
        # ADR-038's reasoning: one session per account, so a surrogate key would
        # be a second name for one row.
        assert prepared().account_id == ACCOUNT

    def test_is_immutable(self) -> None:
        with pytest.raises(Exception, match=r"cannot assign|frozen"):
            prepared().authorization_state = AuthorizationState.READY  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestSessionValidation:
    @pytest.mark.parametrize("account_id", [0, -1])
    def test_the_account_identifier_must_be_positive(self, account_id: int) -> None:
        with pytest.raises(DomainValidationError):
            prepared(account_id=AccountId(account_id))

    def test_a_session_needs_somewhere_to_store_its_data(self) -> None:
        # Only whitespace is testable here: Path("") normalises to Path("."),
        # so an empty path is not something this type can hold.
        with pytest.raises(DomainValidationError, match="path to its local store") as excinfo:
            prepared(session_path=Path(" "))

        assert "storage location" in excinfo.value.user_message

    @pytest.mark.parametrize("reference", ["", "   "])
    def test_a_session_needs_a_key_reference(self, reference: str) -> None:
        with pytest.raises(DomainValidationError, match="name of its encryption key"):
            prepared(encryption_key_ref=reference)

    def test_a_key_reference_has_a_maximum_length(self) -> None:
        # Matches the column, so a value the entity accepts always fits.
        with pytest.raises(DomainValidationError, match="at most"):
            prepared(encryption_key_ref="k" * (MAX_KEY_REF_LENGTH + 1))

    def test_a_key_reference_of_exactly_the_maximum_is_accepted(self) -> None:
        assert prepared(encryption_key_ref="k" * MAX_KEY_REF_LENGTH).encryption_key_ref

    @pytest.mark.parametrize(
        "field", ["created_at", "updated_at", "connected_at", "last_activity_at"]
    )
    def test_naive_timestamps_are_refused(self, field: str) -> None:
        # A naive datetime has no defined instant, so two of them cannot be
        # compared and neither can be stored honestly.
        naive = datetime(2026, 6, 1, 12, 0)  # noqa: DTZ001 - the point of the test
        overrides: dict[str, Any] = {field: naive}
        if field == "connected_at":
            overrides["connection_state"] = ConnectionState.READY

        with pytest.raises(DomainValidationError, match="timezone-aware"):
            prepared(**overrides)

    def test_non_utc_timestamps_are_refused(self) -> None:
        moscow = datetime(2026, 6, 1, 15, 0, tzinfo=timezone(timedelta(hours=3)))

        with pytest.raises(DomainValidationError, match="must be UTC"):
            prepared(last_activity_at=moscow)

    def test_a_session_cannot_be_updated_before_it_was_created(self) -> None:
        with pytest.raises(DomainValidationError, match="before it was created"):
            prepared(updated_at=NOW - timedelta(seconds=1))

    @pytest.mark.parametrize(
        "state",
        [ConnectionState.OFFLINE, ConnectionState.CONNECTING, ConnectionState.WAITING_FOR_NETWORK],
    )
    def test_an_unconnected_session_cannot_record_a_connection_time(
        self, state: ConnectionState
    ) -> None:
        # A leftover stamp would answer "how long connected" with a duration
        # that never happened.
        with pytest.raises(DomainValidationError, match="not connected"):
            prepared(connection_state=state, connected_at=NOW)

    @pytest.mark.parametrize("state", sorted(CONNECTED_STATES))
    def test_a_connected_session_may_record_a_connection_time(self, state: ConnectionState) -> None:
        assert prepared(connection_state=state, connected_at=NOW).connected_at == NOW


# ---------------------------------------------------------------------------
# Derived state
# ---------------------------------------------------------------------------


class TestDerivedState:
    def test_authorization_and_connection_are_independent(self) -> None:
        # The whole point of ADR-049: a single enum could not represent this.
        session = prepared(
            authorization_state=AuthorizationState.READY,
            connection_state=ConnectionState.CONNECTING,
        )

        assert session.is_authorized
        assert not session.is_connected

    def test_connected_from_updating_onwards(self) -> None:
        # TDLib's socket is up at 'updating'; that state means connected and
        # catching up.
        assert prepared(connection_state=ConnectionState.UPDATING).is_connected

    @pytest.mark.parametrize(
        "state",
        [ConnectionState.OFFLINE, ConnectionState.CONNECTING, ConnectionState.WAITING_FOR_NETWORK],
    )
    def test_not_connected_otherwise(self, state: ConnectionState) -> None:
        assert not prepared(connection_state=state).is_connected

    def test_sending_needs_both_axes_ready(self) -> None:
        assert prepared(
            authorization_state=AuthorizationState.READY,
            connection_state=ConnectionState.READY,
        ).can_send

    def test_sending_is_refused_while_still_catching_up(self) -> None:
        # Stricter than is_connected: a client replaying a backlog may not yet
        # know the conversation has moved on, and suggesting a reply into a
        # stale view of a chat is the mistake this application exists to avoid.
        session = prepared(
            authorization_state=AuthorizationState.READY,
            connection_state=ConnectionState.UPDATING,
            connected_at=NOW,
        )

        assert session.is_connected
        assert not session.can_send

    def test_sending_is_refused_without_credentials(self) -> None:
        assert not prepared(connection_state=ConnectionState.READY, connected_at=NOW).can_send

    @pytest.mark.parametrize(
        "state",
        [
            AuthorizationState.WAITING_PHONE,
            AuthorizationState.WAITING_CODE,
            AuthorizationState.WAITING_PASSWORD,
        ],
    )
    def test_waiting_states_need_a_person(self, state: AuthorizationState) -> None:
        assert prepared(authorization_state=state).needs_credentials

    @pytest.mark.parametrize(
        "state",
        [
            AuthorizationState.UNAUTHORIZED,
            AuthorizationState.READY,
            AuthorizationState.LOGGED_OUT,
        ],
    )
    def test_other_states_do_not(self, state: AuthorizationState) -> None:
        assert not prepared(authorization_state=state).needs_credentials


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


class TestAuthorizationTransitions:
    def test_records_the_new_state_and_the_time(self) -> None:
        changed = prepared().with_authorization(AuthorizationState.WAITING_CODE, LATER)

        assert changed.authorization_state is AuthorizationState.WAITING_CODE
        assert changed.updated_at == LATER

    def test_leaves_the_connection_alone(self) -> None:
        session = prepared(connection_state=ConnectionState.READY, connected_at=NOW)

        changed = session.with_authorization(AuthorizationState.READY, LATER)

        assert changed.connection_state is ConnectionState.READY
        assert changed.connected_at == NOW

    def test_an_unchanged_state_returns_the_same_object(self) -> None:
        # A repeated report from TDLib must not move updated_at and make nothing
        # look like something.
        session = prepared()

        assert session.with_authorization(AuthorizationState.UNAUTHORIZED, LATER) is session


class TestConnectionTransitions:
    def test_becoming_connected_stamps_the_time(self) -> None:
        changed = prepared().with_connection(ConnectionState.READY, LATER)

        assert changed.connected_at == LATER

    def test_updating_counts_as_becoming_connected(self) -> None:
        assert prepared().with_connection(ConnectionState.UPDATING, LATER).connected_at == LATER

    def test_catching_up_then_ready_keeps_the_original_stamp(self) -> None:
        # The connection did not restart; it finished catching up.
        session = prepared().with_connection(ConnectionState.UPDATING, LATER)

        changed = session.with_connection(ConnectionState.READY, LATER + timedelta(minutes=1))

        assert changed.connected_at == LATER

    @pytest.mark.parametrize(
        "state",
        [ConnectionState.OFFLINE, ConnectionState.CONNECTING, ConnectionState.WAITING_FOR_NETWORK],
    )
    def test_losing_the_connection_clears_the_stamp(self, state: ConnectionState) -> None:
        session = prepared().with_connection(ConnectionState.READY, LATER)

        assert session.with_connection(state, LATER).connected_at is None

    def test_leaves_authorization_alone(self) -> None:
        # The reconnect case that the old single-enum model could not survive.
        session = prepared(authorization_state=AuthorizationState.READY)

        changed = session.with_connection(ConnectionState.WAITING_FOR_NETWORK, LATER)

        assert changed.is_authorized

    def test_an_unchanged_state_returns_the_same_object(self) -> None:
        session = prepared()

        assert session.with_connection(ConnectionState.OFFLINE, LATER) is session


class TestOtherTransitions:
    def test_activity_moves_both_timestamps(self) -> None:
        changed = prepared().with_activity(LATER)

        assert changed.last_activity_at == LATER
        assert changed.updated_at == LATER

    def test_the_client_version_is_recorded(self) -> None:
        # A store written by a newer TDLib may not be readable by an older one.
        changed = prepared().with_client_version("1.8.66", LATER)

        assert changed.client_version == "1.8.66"
        assert changed.updated_at == LATER

    def test_an_unchanged_version_returns_the_same_object(self) -> None:
        session = prepared().with_client_version("1.8.66", LATER)

        assert session.with_client_version("1.8.66", LATER + timedelta(days=1)) is session

    def test_logging_out_moves_both_axes(self) -> None:
        session = (
            prepared()
            .with_authorization(AuthorizationState.READY, LATER)
            .with_connection(ConnectionState.READY, LATER)
        )

        out = session.logged_out(LATER + timedelta(hours=1))

        assert out.authorization_state is AuthorizationState.LOGGED_OUT
        assert out.connection_state is ConnectionState.OFFLINE
        assert out.connected_at is None
        assert not out.can_send

    def test_logging_out_keeps_the_key_reference(self) -> None:
        # The caller destroys the key and the store; the entity cannot delete a
        # directory, and the record of which key it was remains useful.
        out = prepared().logged_out(LATER)

        assert out.encryption_key_ref == KEY_REF
        assert out.session_path == PATH


# ---------------------------------------------------------------------------
# The key is a name, never a value
# ---------------------------------------------------------------------------


class TestKeyMaterialNeverAppears:
    def test_the_entity_has_no_field_for_a_key(self) -> None:
        # Structural, not a convention: there is nowhere to put one.
        fields = set(Session.__dataclass_fields__)

        assert "encryption_key" not in fields
        assert fields & {"key", "secret", "password"} == set()

    def test_the_table_has_no_column_for_a_key(self) -> None:
        columns = {column.name for column in telegram_sessions.columns}

        assert "encryption_key" not in columns
        assert columns & {"key", "secret", "password"} == set()


# ---------------------------------------------------------------------------
# Mapper
# ---------------------------------------------------------------------------


class _FakeRow:
    """A row-like object for testing the mapper without a database."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        return self._values[name]


class TestSessionMapper:
    def test_round_trip_preserves_every_field(self) -> None:
        mapper = SessionMapper()
        original = (
            prepared()
            .with_authorization(AuthorizationState.READY, LATER)
            .with_connection(ConnectionState.UPDATING, LATER)
            .with_client_version("1.8.66", LATER)
            .with_activity(LATER)
        )

        restored = mapper.to_domain(_FakeRow(mapper.to_params(original)))

        assert restored == original

    def test_round_trip_preserves_absent_optional_fields(self) -> None:
        mapper = SessionMapper()
        original = prepared()

        assert mapper.to_domain(_FakeRow(mapper.to_params(original))) == original

    def test_covers_every_column(self) -> None:
        written = column_names(SessionMapper().to_params(prepared()))
        declared = {column.name for column in telegram_sessions.columns}

        assert declared == written

    def test_stores_enumerations_as_their_values(self) -> None:
        params = SessionMapper().to_params(prepared())

        assert params["authorization_state"] == "unauthorized"
        assert params["connection_state"] == "offline"

    def test_stores_the_path_as_text(self) -> None:
        assert SessionMapper().to_params(prepared())["session_path"] == str(PATH)

    def test_is_pure(self) -> None:
        mapper = SessionMapper()
        session = prepared()

        assert mapper.to_params(session) == mapper.to_params(session)

    def test_reads_text_timestamps(self) -> None:
        params = SessionMapper().to_params(prepared())
        params["created_at"] = NOW.isoformat()
        params["updated_at"] = NOW.isoformat()

        assert SessionMapper().to_domain(_FakeRow(params)).created_at == NOW


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SqliteDatabase]:
    """A connected database with no schema applied."""
    db = SqliteDatabase(DatabaseSection(path=tmp_path / "sessions.db"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def _tables(database: SqliteDatabase) -> list[str]:
    return await database.executor.run(
        lambda: list(
            database.connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).scalars()
        )
    )


async def _insert_account(database: SqliteDatabase, account_id: int) -> None:
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO accounts (id, telegram_user_id, display_name, timezone, "
                "is_active, created_at, updated_at) "
                "VALUES (:id, :id, 'x', 'UTC', 0, '2026-01-01', '2026-01-01')"
            ),
            {"id": account_id},
        )
    )


async def _insert_session(database: SqliteDatabase, account_id: int, **overrides: object) -> None:
    values: dict[str, object] = {
        "account_id": account_id,
        "authorization_state": "unauthorized",
        "connection_state": "offline",
        "session_path": f"sessions/{account_id}",
        "encryption_key_ref": f"telegram-session-key-{account_id}",
        "client_version": None,
        "connected_at": None,
        "last_activity_at": None,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
    }
    values.update(overrides)
    await database.executor.run(
        lambda: database.connection.execute(
            text(
                "INSERT INTO telegram_sessions (account_id, authorization_state, "
                "connection_state, session_path, encryption_key_ref, client_version, "
                "connected_at, last_activity_at, created_at, updated_at) "
                "VALUES (:account_id, :authorization_state, :connection_state, "
                ":session_path, :encryption_key_ref, :client_version, :connected_at, "
                ":last_activity_at, :created_at, :updated_at)"
            ),
            values,
        )
    )


async def _session_count(database: SqliteDatabase) -> int:
    return await database.executor.run(
        lambda: database.connection.execute(
            text("SELECT COUNT(*) FROM telegram_sessions")
        ).scalar_one()
    )


class TestTelegramSessionsMigration:
    async def test_creates_the_table(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        assert TELEGRAM_SESSIONS_TABLE in await _tables(database)

    async def test_upgrading_reaches_the_sessions_revision(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        applied = await AlembicMigrationRunner(database).current_revision()

        assert applied is not None
        assert applied >= "0007"

    async def test_round_trips_up_down_up(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)

        await runner.upgrade()
        await runner.downgrade("0006")
        assert TELEGRAM_SESSIONS_TABLE not in await _tables(database)

        await runner.upgrade()
        assert TELEGRAM_SESSIONS_TABLE in await _tables(database)

    async def test_downgrade_leaves_accounts_intact(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()

        await runner.downgrade("0006")

        assert "accounts" in await _tables(database)


class TestForeignKeyIntegrity:
    async def test_a_session_requires_an_existing_account(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        with pytest.raises(Exception, match="FOREIGN KEY constraint"):
            await _insert_session(database, account_id=999)


class TestCascadeDeletion:
    async def test_deleting_an_account_deletes_its_session(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)
        await _insert_session(database, 1)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM accounts WHERE id = 1"))
        )

        assert await _session_count(database) == 0

    async def test_deleting_one_account_leaves_others(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        for account_id in (1, 2):
            await _insert_account(database, account_id)
            await _insert_session(database, account_id)

        await database.executor.run(
            lambda: database.connection.execute(text("DELETE FROM accounts WHERE id = 1"))
        )

        assert await _session_count(database) == 1


class TestCheckConstraints:
    async def test_one_session_per_account(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)
        await _insert_session(database, 1)

        with pytest.raises(Exception, match=r"UNIQUE constraint|PRIMARY KEY"):
            await _insert_session(database, 1)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"authorization_state": "reconnecting"},
            {"connection_state": "ready_ish"},
            {"session_path": "   "},
            {"encryption_key_ref": ""},
            {"connection_state": "offline", "connected_at": "2026-01-01"},
            {"connection_state": "connecting", "connected_at": "2026-01-01"},
            {"created_at": "2026-06-01", "updated_at": "2026-01-01"},
        ],
    )
    async def test_invalid_rows_are_refused(
        self, database: SqliteDatabase, overrides: dict[str, object]
    ) -> None:
        # The schema restates the entity's invariants, so a row written by any
        # route -- a repair script, a future migration -- cannot violate them.
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)

        with pytest.raises(Exception, match="CHECK constraint"):
            await _insert_session(database, 1, **overrides)

    @pytest.mark.parametrize(
        "overrides",
        [
            {
                "authorization_state": "ready",
                "connection_state": "ready",
                "connected_at": "2026-01-01",
            },
            {"authorization_state": "waiting_password"},
            {"authorization_state": "logged_out"},
            {"connection_state": "waiting_for_network"},
            {"connection_state": "updating", "connected_at": "2026-01-01"},
        ],
    )
    async def test_valid_rows_are_accepted(
        self, database: SqliteDatabase, overrides: dict[str, object]
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        await _insert_account(database, 1)

        await _insert_session(database, 1, **overrides)

    async def test_every_authorization_state_the_entity_can_hold_is_storable(
        self, database: SqliteDatabase
    ) -> None:
        # The two enumerations and the two CHECK constraints drift apart
        # silently otherwise: a state the entity accepts but the table refuses
        # would fail only at the moment a real user reached it.
        await AlembicMigrationRunner(database).upgrade()
        for index, state in enumerate(AuthorizationState, start=1):
            await _insert_account(database, index)
            await _insert_session(database, index, authorization_state=state.value)

        assert await _session_count(database) == len(AuthorizationState)

    async def test_every_connection_state_the_entity_can_hold_is_storable(
        self, database: SqliteDatabase
    ) -> None:
        await AlembicMigrationRunner(database).upgrade()
        for index, state in enumerate(ConnectionState, start=1):
            await _insert_account(database, index)
            await _insert_session(database, index, connection_state=state.value)

        assert await _session_count(database) == len(ConnectionState)
