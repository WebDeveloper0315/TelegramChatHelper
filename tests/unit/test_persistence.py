"""Engine, migration, repository and startup tests.

Behaviour shared across unit of work implementations lives in
``tests/contract/test_unit_of_work_contract.py``. This file covers the SQLite
engine, the Alembic runner and the generic repository helpers.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select, text

from tgassist.application.container import Container
from tgassist.domain.errors import (
    ConstraintViolationError,
    DatabaseUnavailableError,
    MigrationFailedError,
    RecordNotFoundError,
    SchemaVersionError,
)
from tgassist.domain.model.page import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, clamp_page_size
from tgassist.domain.ports.migration_runner import SchemaState
from tgassist.infrastructure.config import AppConfig, DatabaseSection, LoadedConfig
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    Cursor,
    DatabaseExecutor,
    Repository,
    SqlAlchemyUnitOfWork,
    SqliteDatabase,
)
from tgassist.infrastructure.persistence.mappers import (
    from_stored_bool,
    from_stored_datetime,
    from_stored_json,
    to_stored_bool,
    to_stored_datetime,
    to_stored_json,
)
from tgassist.infrastructure.persistence.schema import SCHEMA_METADATA_TABLE

_metadata = MetaData()
widgets = Table(
    "widgets",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(32), nullable=False, unique=True),
)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SqliteDatabase]:
    """A connected SQLite database on disk."""
    db = SqliteDatabase(DatabaseSection(path=tmp_path / "test.db"))
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def seeded(database: SqliteDatabase) -> SqliteDatabase:
    """A database with the sample table created."""
    await database.executor.run(_metadata.create_all, database.connection)
    return database


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TestEngineConfiguration:
    async def test_connect_is_idempotent(self, database: SqliteDatabase) -> None:
        await database.connect()
        await database.connect()

        assert database.is_connected

    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        db = SqliteDatabase(DatabaseSection(path=tmp_path / "x.db"))
        await db.connect()
        await db.close()
        await db.close()

        assert not db.is_connected

    async def test_close_without_connect_is_safe(self, tmp_path: Path) -> None:
        await SqliteDatabase(DatabaseSection(path=tmp_path / "x.db")).close()

    async def test_wal_is_enabled(self, database: SqliteDatabase) -> None:
        health = await database.health()

        assert health.pragmas is not None
        assert health.pragmas.is_wal

    async def test_foreign_keys_are_enforced(self, database: SqliteDatabase) -> None:
        # Off by default in SQLite, and must be enabled per connection. Without
        # this every declared relationship is documentation, not enforcement.
        health = await database.health()

        assert health.pragmas is not None
        assert health.pragmas.foreign_keys is True

    async def test_busy_timeout_is_applied(self, tmp_path: Path) -> None:
        db = SqliteDatabase(DatabaseSection(path=tmp_path / "x.db", busy_timeout_ms=1234))
        await db.connect()
        try:
            health = await db.health()
        finally:
            await db.close()

        assert health.pragmas is not None
        assert health.pragmas.busy_timeout_ms == 1234

    async def test_synchronous_is_applied(self, tmp_path: Path) -> None:
        db = SqliteDatabase(DatabaseSection(path=tmp_path / "x.db", synchronous="FULL"))
        await db.connect()
        try:
            health = await db.health()
        finally:
            await db.close()

        assert health.pragmas is not None
        assert health.pragmas.synchronous == "FULL"

    async def test_pragmas_are_read_back_not_assumed(self, database: SqliteDatabase) -> None:
        # A pragma that silently failed to apply looks identical to one that
        # worked, until concurrency exposes it. Reading back is the difference.
        health = await database.health()

        assert health.pragmas is not None
        assert health.pragmas.journal_mode

    async def test_parent_directory_is_created(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c.db"
        db = SqliteDatabase(DatabaseSection(path=nested))
        await db.connect()
        try:
            assert nested.parent.is_dir()
        finally:
            await db.close()

    async def test_accessing_the_engine_before_connecting_raises(self, tmp_path: Path) -> None:
        db = SqliteDatabase(DatabaseSection(path=tmp_path / "x.db"))

        with pytest.raises(DatabaseUnavailableError):
            _ = db.engine

    async def test_unopenable_path_raises_a_domain_error(self, tmp_path: Path) -> None:
        # A directory where a file should be: the driver error must arrive as a
        # domain error, not as a sqlite3 exception.
        directory = tmp_path / "not-a-file"
        directory.mkdir()
        db = SqliteDatabase(DatabaseSection(path=directory))

        with pytest.raises(DatabaseUnavailableError):
            await db.connect()


class TestHealthCheck:
    async def test_reports_healthy(self, database: SqliteDatabase) -> None:
        health = await database.health()

        assert health.healthy
        assert health.reachable
        assert health.integrity_ok
        assert health.foreign_keys_ok

    async def test_reports_size(self, seeded: SqliteDatabase) -> None:
        health = await seeded.health()

        assert health.page_size_bytes > 0
        assert health.size_bytes == health.page_count * health.page_size_bytes

    async def test_never_raises_when_disconnected(self, tmp_path: Path) -> None:
        # A health check exists to report trouble, not to become it.
        db = SqliteDatabase(DatabaseSection(path=tmp_path / "x.db"))

        health = await db.health()

        assert not health.healthy
        assert not health.reachable
        assert health.problems

    async def test_leaves_no_open_transaction(self, database: SqliteDatabase) -> None:
        # SQLAlchemy autobegins on any statement. On a connection held for the
        # process lifetime that would block the next explicit begin().
        await database.health()

        in_transaction = await database.executor.run(database.connection.in_transaction)

        assert in_transaction is False


class TestExecutor:
    async def test_runs_on_a_single_thread(self) -> None:
        executor = DatabaseExecutor()
        try:
            threads = {await executor.run(_current_thread_id) for _ in range(50)}
        finally:
            executor.close()

        assert len(threads) == 1

    async def test_refuses_work_after_close(self) -> None:
        # Queuing onto a closed executor would hang rather than fail.
        executor = DatabaseExecutor()
        executor.close()

        with pytest.raises(RuntimeError, match="closed"):
            await executor.run(lambda: None)

    def test_close_is_idempotent(self) -> None:
        executor = DatabaseExecutor()
        executor.close()
        executor.close()

        assert executor.is_closed


def _current_thread_id() -> int:
    return threading.get_ident()


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


class TestMigrations:
    async def test_empty_database_reports_empty(self, database: SqliteDatabase) -> None:
        status = await AlembicMigrationRunner(database).status()

        assert status.state is SchemaState.EMPTY
        assert status.current_revision is None
        assert status.pending

    async def test_upgrade_applies_the_baseline(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)

        report = await runner.upgrade()

        assert report.changed
        assert report.from_revision is None
        assert report.to_revision == runner.head_revision()

    async def test_upgrade_creates_the_schema(self, database: SqliteDatabase) -> None:
        await AlembicMigrationRunner(database).upgrade()

        names = await database.executor.run(
            lambda: (
                database.connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
                .scalars()
                .all()
            )
        )

        assert SCHEMA_METADATA_TABLE in names

    async def test_status_is_current_after_upgrade(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()

        status = await runner.status()

        assert status.state is SchemaState.CURRENT
        assert status.pending == ()

    async def test_upgrade_is_idempotent(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()

        report = await runner.upgrade()

        assert not report.changed

    async def test_round_trips_up_down_up(self, database: SqliteDatabase) -> None:
        # The property every migration must have. A migration that cannot be
        # reverted cannot be backed out of a bad release.
        runner = AlembicMigrationRunner(database)
        head = runner.head_revision()

        await runner.upgrade()
        await runner.downgrade("base")
        assert await runner.current_revision() is None

        await runner.upgrade()
        assert await runner.current_revision() == head

    async def test_downgrade_drops_the_schema(self, database: SqliteDatabase) -> None:
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()

        await runner.downgrade("base")
        names = await database.executor.run(
            lambda: (
                database.connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
                .scalars()
                .all()
            )
        )

        assert SCHEMA_METADATA_TABLE not in names

    async def test_a_newer_database_is_refused(self, database: SqliteDatabase) -> None:
        # Downgrading user data is never attempted: a migration that dropped a
        # column cannot restore what it discarded.
        runner = AlembicMigrationRunner(database)
        await runner.upgrade()
        await database.executor.run(
            lambda: database.connection.execute(
                text("UPDATE alembic_version SET version_num='9999'")
            )
        )
        await database.executor.run(database.connection.commit)

        status = await runner.status()
        assert status.state is SchemaState.UNKNOWN
        assert not status.can_start

        with pytest.raises(SchemaVersionError):
            await runner.upgrade()

    async def test_pre_upgrade_hook_runs_first(self, database: SqliteDatabase) -> None:
        calls: list[str] = []

        async def hook() -> bool:
            calls.append("backup")
            return True

        runner = AlembicMigrationRunner(database)
        runner.set_pre_upgrade_hook(hook)

        report = await runner.upgrade()

        assert calls == ["backup"]
        assert report.backup_taken

    async def test_a_failed_backup_prevents_the_migration(self, database: SqliteDatabase) -> None:
        async def hook() -> bool:
            return False

        runner = AlembicMigrationRunner(database)
        runner.set_pre_upgrade_hook(hook)

        with pytest.raises(MigrationFailedError):
            await runner.upgrade()

        assert await runner.current_revision() is None

    async def test_reports_no_backup_when_none_is_configured(
        self, database: SqliteDatabase
    ) -> None:
        # Honest rather than reassuring: the backup subsystem is Milestone 11.
        report = await AlembicMigrationRunner(database).upgrade()

        assert report.backup_taken is False


# ---------------------------------------------------------------------------
# Repository infrastructure
# ---------------------------------------------------------------------------


class WidgetRepository(Repository[str]):
    """A minimal repository, used to exercise the generic base."""

    async def add(self, widget_id: int, name: str) -> None:
        await self.execute_write(
            insert(widgets).values(id=widget_id, name=name),
            operation="add_widget",
            conflict_message="A widget with that name already exists.",
        )

    async def names(self) -> list[str]:
        rows = await self.fetch_all(select(widgets.c.name).order_by(widgets.c.id))
        return [row.name for row in rows]

    async def page(self, cursor: str | None, limit: int) -> Page[str]:
        after = Cursor.decode(cursor)
        last_id = int(after["id"]) if after else 0

        def statement(row_limit: int) -> object:
            return (
                select(widgets.c.id, widgets.c.name)
                .where(widgets.c.id > last_id)
                .order_by(widgets.c.id)
                .limit(row_limit)
            )

        return await self.fetch_page(
            statement,  # type: ignore[arg-type]
            mapper=lambda row: str(row.name),
            cursor_builder=lambda row: {"id": row.id},
            limit=limit,
        )

    async def require(self, name: str) -> str:
        row = await self.require_one(
            select(widgets.c.name).where(widgets.c.name == name), entity="widget"
        )
        return str(row.name)


class TestRepositoryInfrastructure:
    async def test_writes_and_reads_within_a_transaction(self, seeded: SqliteDatabase) -> None:
        async with SqlAlchemyUnitOfWork(seeded) as uow:
            repo = WidgetRepository(uow)
            await repo.add(1, "alpha")
            assert await repo.names() == ["alpha"]
            await uow.commit()

    async def test_constraint_violations_become_domain_errors(self, seeded: SqliteDatabase) -> None:
        async with SqlAlchemyUnitOfWork(seeded) as uow:
            repo = WidgetRepository(uow)
            await repo.add(1, "alpha")

            with pytest.raises(ConstraintViolationError) as excinfo:
                await repo.add(2, "alpha")

        assert "already exists" in excinfo.value.user_message

    async def test_require_one_raises_when_absent(self, seeded: SqliteDatabase) -> None:
        async with SqlAlchemyUnitOfWork(seeded) as uow:
            with pytest.raises(RecordNotFoundError):
                await WidgetRepository(uow).require("missing")

    async def test_pagination_walks_every_row_exactly_once(self, seeded: SqliteDatabase) -> None:
        async with SqlAlchemyUnitOfWork(seeded) as uow:
            repo = WidgetRepository(uow)
            for index in range(25):
                await repo.add(index + 1, f"widget-{index:02d}")
            await uow.commit()

        seen: list[str] = []
        cursor: str | None = None
        async with SqlAlchemyUnitOfWork(seeded) as uow:
            repo = WidgetRepository(uow)
            while True:
                page = await repo.page(cursor, limit=7)
                seen.extend(page.items)
                if not page.has_more:
                    break
                cursor = page.next_cursor

        assert len(seen) == 25
        assert len(set(seen)) == 25
        assert seen == sorted(seen)

    async def test_last_page_has_no_cursor(self, seeded: SqliteDatabase) -> None:
        async with SqlAlchemyUnitOfWork(seeded) as uow:
            repo = WidgetRepository(uow)
            await repo.add(1, "only")
            await uow.commit()

        async with SqlAlchemyUnitOfWork(seeded) as uow:
            page = await WidgetRepository(uow).page(None, limit=10)

        assert not page.has_more
        assert page.next_cursor is None


class TestCursor:
    def test_round_trips(self) -> None:
        token = Cursor.encode({"id": 42, "at": "2026-01-01"})

        assert Cursor.decode(token) == {"id": 42, "at": "2026-01-01"}

    def test_absent_token_decodes_to_none(self) -> None:
        assert Cursor.decode(None) is None
        assert Cursor.decode("") is None

    def test_malformed_token_decodes_to_none(self) -> None:
        # A stale bookmark should start from the beginning, not raise.
        assert Cursor.decode("not-a-cursor!!") is None

    def test_is_url_safe(self) -> None:
        token = Cursor.encode({"value": "a/b+c=d"})

        assert "/" not in token
        assert "+" not in token


class TestPage:
    def test_empty_page(self) -> None:
        page: Page[str] = Page.empty()

        assert not page
        assert len(page) == 0
        assert not page.has_more

    def test_iterates_items(self) -> None:
        page = Page(items=["a", "b"])

        assert list(page) == ["a", "b"]

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [(None, DEFAULT_PAGE_SIZE), (0, 1), (-5, 1), (10, 10), (10_000, MAX_PAGE_SIZE)],
    )
    def test_page_size_is_clamped(self, requested: int | None, expected: int) -> None:
        assert clamp_page_size(requested) == expected


class TestMappers:
    def test_datetime_round_trips_in_utc(self) -> None:
        value = datetime(2026, 3, 1, 9, 30, tzinfo=timezone(timedelta(hours=9)))

        restored = from_stored_datetime(to_stored_datetime(value))

        assert restored == value
        assert restored is not None
        assert restored.utcoffset() == timedelta(0)

    def test_naive_datetimes_are_refused(self) -> None:
        # The same rule the Clock port enforces, applied at the storage boundary.
        with pytest.raises(ValueError, match="naive"):
            to_stored_datetime(datetime(2026, 1, 1))  # noqa: DTZ001

    def test_none_passes_through(self) -> None:
        assert to_stored_datetime(None) is None
        assert from_stored_datetime(None) is None
        assert to_stored_bool(None) is None
        assert to_stored_json(None) is None

    def test_bool_round_trips(self) -> None:
        assert from_stored_bool(to_stored_bool(True)) is True
        assert from_stored_bool(to_stored_bool(False)) is False

    def test_json_round_trips(self) -> None:
        value = {"b": 1, "a": [1, 2, {"c": None}]}

        assert from_stored_json(to_stored_json(value)) == value

    def test_json_encoding_is_stable(self) -> None:
        # Content fingerprints and cache keys depend on byte-identical output
        # for equal structures; an unsorted dump would invalidate caches at random.
        assert to_stored_json({"b": 1, "a": 2}) == to_stored_json({"a": 2, "b": 1})

    def test_datetime_is_stored_as_utc_text(self) -> None:
        stored = to_stored_datetime(datetime(2026, 1, 1, 12, 0, tzinfo=UTC))

        assert stored is not None
        assert stored.startswith("2026-01-01T12:00:00")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrentAccess:
    async def test_concurrent_reads_are_serialised_without_error(
        self, seeded: SqliteDatabase
    ) -> None:
        async with SqlAlchemyUnitOfWork(seeded) as uow:
            repo = WidgetRepository(uow)
            for index in range(10):
                await repo.add(index + 1, f"w{index}")
            await uow.commit()

        async def read() -> list[str]:
            async with SqlAlchemyUnitOfWork(seeded) as uow:
                return await WidgetRepository(uow).names()

        results = await asyncio.gather(*(read() for _ in range(20)))

        assert all(len(names) == 10 for names in results)

    async def test_concurrent_writes_do_not_produce_database_locked(
        self, seeded: SqliteDatabase
    ) -> None:
        # The reason for a single worker thread: with several, these would
        # contend and surface as intermittent SQLITE_BUSY under load.
        async def write(index: int) -> None:
            async with SqlAlchemyUnitOfWork(seeded) as uow:
                await WidgetRepository(uow).add(index, f"concurrent-{index}")
                await uow.commit()

        await asyncio.gather(*(write(i) for i in range(1, 21)))

        async with SqlAlchemyUnitOfWork(seeded) as uow:
            names = await WidgetRepository(uow).names()

        assert len(names) == 20

    async def test_every_statement_runs_on_the_database_thread(
        self, seeded: SqliteDatabase
    ) -> None:
        async with SqlAlchemyUnitOfWork(seeded) as uow:
            await WidgetRepository(uow).names()

        assert not seeded.executor.is_database_thread()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "profile": "testing",
            "app": {"data_dir": str(tmp_path)},
            "logging": {"console_enabled": False, "file_enabled": False},
        }
    )


class TestContainerStartup:
    async def test_start_database_migrates_to_head(self, tmp_path: Path) -> None:
        async with Container(LoadedConfig(config=_config(tmp_path))) as container:
            status = await container.start_database()

            assert status.state is SchemaState.CURRENT
            assert status.current_revision == container.migrations.head_revision()

    async def test_start_database_is_idempotent(self, tmp_path: Path) -> None:
        async with Container(LoadedConfig(config=_config(tmp_path))) as container:
            await container.start_database()
            status = await container.start_database()

            assert status.state is SchemaState.CURRENT

    async def test_auto_migrate_can_be_disabled(self, tmp_path: Path) -> None:
        async with Container(LoadedConfig(config=_config(tmp_path))) as container:
            status = await container.start_database(migrate=False)

            assert status.state is SchemaState.EMPTY

    async def test_health_after_startup(self, tmp_path: Path) -> None:
        async with Container(LoadedConfig(config=_config(tmp_path))) as container:
            await container.start_database()

            health = await container.database_health()

            assert health.healthy
            assert health.schema_revision == container.migrations.head_revision()

    async def test_database_file_is_created_at_the_configured_path(self, tmp_path: Path) -> None:
        async with Container(LoadedConfig(config=_config(tmp_path))) as container:
            await container.start_database()

        assert (tmp_path / "tgassist.db").exists()

    async def test_startup_refuses_a_newer_database(self, tmp_path: Path) -> None:
        async with Container(LoadedConfig(config=_config(tmp_path))) as container:
            await container.start_database()
            db = container.database
            await db.executor.run(
                lambda: db.connection.execute(text("UPDATE alembic_version SET version_num='9999'"))
            )
            await db.executor.run(db.connection.commit)

            with pytest.raises(SchemaVersionError):
                await container.start_database()

    async def test_container_exposes_persistence_ports(self, tmp_path: Path) -> None:
        async with Container(LoadedConfig(config=_config(tmp_path))) as container:
            assert container.database is container.database
            assert container.unit_of_work() is not container.unit_of_work()
            assert container.migrations is container.migrations

    async def test_closing_releases_the_worker_thread(self, tmp_path: Path) -> None:
        container = Container(LoadedConfig(config=_config(tmp_path)))
        await container.start_database()

        await container.aclose()

        assert container.database.executor.is_closed
