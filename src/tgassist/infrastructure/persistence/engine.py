"""SQLite engine, connection settings and health checks.

Every database decision has exactly one implementation site here, which is what
allows ADR-022's phase 2 (optional full-database encryption) to be a contained
change rather than a search through every connection in the codebase.

Uses SQLAlchemy Core, not the ORM (ADR-015): the expression language gives safe
parameter binding and dialect portability, while the ORM's identity map and
session lifetime are precisely the things that leak persistence concerns into a
domain that is supposed to be ignorant of them.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from tgassist.domain.errors import DatabaseUnavailableError
from tgassist.domain.ports.database import Database, HealthReport, PragmaState
from tgassist.infrastructure.config.settings import DatabaseSection
from tgassist.infrastructure.logging import get_logger
from tgassist.infrastructure.persistence.executor import DatabaseExecutor

MEMORY_URL: Final = "sqlite+pysqlite:///:memory:"

_ALEMBIC_VERSION_QUERY: Final = "SELECT version_num FROM alembic_version"


def release_autobegun_transaction(connection: Connection) -> None:
    """Roll back a transaction SQLAlchemy started implicitly.

    SQLAlchemy 2.0 "autobegins": any statement on a connection with no explicit
    transaction opens one. On a short-lived connection that is invisible, because
    the transaction ends with the connection. This design holds one connection
    for the process lifetime, so an autobegun transaction from a pragma read or a
    health check persists -- and the next explicit ``begin()`` is refused with
    "a transaction is already begun".

    Every read performed outside a unit of work therefore releases its implicit
    transaction. Rolling back is safe by construction: only reads happen outside
    a unit of work, so there is never anything to lose.
    """
    if connection.in_transaction():
        connection.rollback()


def build_url(path: Path | None) -> str:
    """Return a SQLAlchemy URL for a database path, or an in-memory database."""
    if path is None:
        return MEMORY_URL
    return f"sqlite+pysqlite:///{path.as_posix()}"


class SqliteDatabase(Database):
    """Owns the SQLAlchemy engine, its worker thread and its connection settings.

    Only one connection exists, held by the single worker thread. That is not a
    performance compromise so much as an invariant: a second connection would
    reintroduce the writer contention the threading model exists to eliminate.
    """

    __slots__ = (
        "_config",
        "_connection",
        "_engine",
        "_executor",
        "_log",
        "_transaction_lock",
        "_url",
    )

    def __init__(
        self,
        config: DatabaseSection,
        *,
        executor: DatabaseExecutor | None = None,
        url: str | None = None,
    ) -> None:
        """Create the database.

        Args:
            config: Connection settings.
            executor: Worker thread to run on. One is created if omitted.
            url: Override the URL derived from ``config``. Tests use an
                in-memory database this way.
        """
        self._config = config
        self._url = url if url is not None else build_url(config.path)
        self._executor = executor if executor is not None else DatabaseExecutor()
        self._engine: Engine | None = None
        self._connection: Connection | None = None
        self._transaction_lock = asyncio.Lock()
        self._log = get_logger(__name__)

    # -- Lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Open the database, apply connection settings and verify them."""
        if self._engine is not None:
            return
        await self._executor.run(self._connect_sync)

    def _connect_sync(self) -> None:
        try:
            self._engine = self._create_engine()
            self._register_pragma_listener(self._engine)
            self._connection = self._engine.connect()
        except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
            self._engine = None
            self._connection = None
            msg = f"Could not open the database at {self._url}: {exc}"
            raise DatabaseUnavailableError(
                msg,
                user_message="The database could not be opened.",
                context={"url": self._url},
                cause=exc,
            ) from exc

        state = self._read_pragmas(self._connection)
        release_autobegun_transaction(self._connection)
        if self._config.journal_mode.upper() == "WAL" and not state.is_wal and not self.is_memory:
            # A silently ignored journal mode is worse than a refused one: the
            # application would run with different concurrency guarantees than
            # it was designed for and only find out under load.
            self._log.warning(
                "journal_mode_not_applied",
                requested=self._config.journal_mode,
                actual=state.journal_mode,
                detail="The filesystem may not support shared memory.",
            )

    def _create_engine(self) -> Engine:
        if not self.is_memory:
            path = self._config.path
            if path is not None:
                path.parent.mkdir(parents=True, exist_ok=True)

        # StaticPool hands out one connection, forever, to every caller.
        #
        # That is exactly this design: one connection on one thread (ADR-013).
        # A sized pool would express "a pool that happens to hold one", which is
        # a different thing -- the second checkout would block until the first
        # is returned, and since the first is held for the process lifetime it
        # would block permanently. An in-memory database needs the same pool for
        # a further reason: it lives inside its connection, so a second
        # connection would be a second, empty database.
        #
        # check_same_thread is disabled because SQLite's own thread guard is
        # redundant here and would reject the executor's worker thread. Thread
        # affinity is instead guaranteed structurally: every statement runs
        # through DatabaseExecutor, which owns exactly one thread.
        return create_engine(
            self._url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            future=True,
        )

    def _register_pragma_listener(self, engine: Engine) -> None:
        config = self._config
        is_memory = self.is_memory

        @event.listens_for(engine, "connect")
        def _apply_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                # Foreign keys are off by default in SQLite and must be enabled
                # per connection. Without this, every declared relationship is
                # documentation rather than enforcement.
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={config.busy_timeout_ms}")
                cursor.execute(f"PRAGMA synchronous={config.synchronous}")
                cursor.execute("PRAGMA temp_store=MEMORY")
                if not is_memory:
                    cursor.execute(f"PRAGMA journal_mode={config.journal_mode}")
            finally:
                cursor.close()

    async def close(self) -> None:
        """Release the connection, the engine and the worker thread. Idempotent."""
        if self._engine is None:
            self._executor.close()
            return
        await self._executor.run(self._close_sync)
        self._executor.close()

    def _close_sync(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    # -- State ------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Report whether the database is currently open."""
        return self._engine is not None

    @property
    def is_memory(self) -> bool:
        """Report whether this is an in-memory database."""
        return ":memory:" in self._url

    @property
    def url(self) -> str:
        """Return the database URL."""
        return self._url

    @property
    def transaction_lock(self) -> asyncio.Lock:
        """Serialises units of work over the single shared connection.

        One connection can hold one transaction. Without this lock a second
        concurrent use case would find a transaction already open and fail;
        with it, the second waits. Queuing is the behaviour the single-writer
        model implies -- it is why SQLITE_BUSY does not appear here -- and this
        extends that from individual statements to whole transactions.

        Held for the lifetime of a transaction, so a long write does delay other
        writers. See ADR-034 for the read-concurrency consequences.
        """
        return self._transaction_lock

    @property
    def executor(self) -> DatabaseExecutor:
        """Return the worker thread all database work runs on."""
        return self._executor

    @property
    def engine(self) -> Engine:
        """Return the SQLAlchemy engine.

        Raises:
            DatabaseUnavailableError: If the database is not connected.
        """
        if self._engine is None:
            msg = "The database is not connected"
            raise DatabaseUnavailableError(
                msg, user_message="The database is not available.", context={"url": self._url}
            )
        return self._engine

    @property
    def connection(self) -> Connection:
        """Return the single held connection.

        Only the unit of work and the migration runner use this; repositories
        receive a connection from the unit of work that owns their transaction.

        Raises:
            DatabaseUnavailableError: If the database is not connected.
        """
        if self._connection is None:
            msg = "The database is not connected"
            raise DatabaseUnavailableError(
                msg, user_message="The database is not available.", context={"url": self._url}
            )
        return self._connection

    # -- Health -----------------------------------------------------------

    async def health(self) -> HealthReport:
        """Check the database and report what was found. Never raises."""
        if self._engine is None:
            return HealthReport(
                reachable=False,
                integrity_ok=False,
                foreign_keys_ok=False,
                problems=("The database is not connected.",),
            )
        return await self._executor.run(self._health_sync)

    def _health_sync(self) -> HealthReport:
        problems: list[str] = []
        try:
            connection = self.connection
            pragmas = self._read_pragmas(connection)

            integrity = connection.execute(text("PRAGMA quick_check")).scalar_one_or_none()
            integrity_ok = str(integrity).lower() == "ok"
            if not integrity_ok:
                problems.append(f"Integrity check reported: {integrity}")

            violations = connection.execute(text("PRAGMA foreign_key_check")).fetchall()
            foreign_keys_ok = not violations
            if violations:
                problems.append(f"{len(violations)} foreign key violation(s)")

            if not pragmas.foreign_keys:
                problems.append("Foreign key enforcement is disabled.")
            if not self.is_memory and not pragmas.is_wal:
                problems.append(f"Journal mode is {pragmas.journal_mode}, expected WAL.")

            page_count = int(connection.execute(text("PRAGMA page_count")).scalar_one())
            page_size = int(connection.execute(text("PRAGMA page_size")).scalar_one())
            freelist = int(connection.execute(text("PRAGMA freelist_count")).scalar_one())

            # Read everything first: each statement autobegins, so releasing
            # before the last read would leave a fresh transaction open.
            revision = self._read_revision(connection)
            release_autobegun_transaction(connection)

            return HealthReport(
                reachable=True,
                integrity_ok=integrity_ok,
                foreign_keys_ok=foreign_keys_ok,
                pragmas=pragmas,
                schema_revision=revision,
                page_count=page_count,
                page_size_bytes=page_size,
                freelist_pages=freelist,
                problems=tuple(problems),
            )
        except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
            # A health check exists to report trouble, so it reports this one
            # rather than becoming the trouble.
            return HealthReport(
                reachable=False,
                integrity_ok=False,
                foreign_keys_ok=False,
                problems=(f"Health check failed: {exc}",),
            )

    @staticmethod
    def _read_pragmas(connection: Connection) -> PragmaState:
        """Read the settings actually in force, rather than assuming they applied."""
        journal = str(connection.execute(text("PRAGMA journal_mode")).scalar_one())
        foreign_keys = bool(connection.execute(text("PRAGMA foreign_keys")).scalar_one())
        busy_timeout = int(connection.execute(text("PRAGMA busy_timeout")).scalar_one())
        synchronous = int(connection.execute(text("PRAGMA synchronous")).scalar_one())
        return PragmaState(
            journal_mode=journal,
            foreign_keys=foreign_keys,
            busy_timeout_ms=busy_timeout,
            synchronous={0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}.get(
                synchronous, str(synchronous)
            ),
        )

    @staticmethod
    def _read_revision(connection: Connection) -> str | None:
        try:
            return connection.execute(text(_ALEMBIC_VERSION_QUERY)).scalar_one_or_none()
        except SQLAlchemyError:
            # No alembic_version table: an un-migrated database, which is a
            # state rather than a fault. The failed statement still autobegan,
            # so the connection is reset before returning.
            release_autobegun_transaction(connection)
            return None
