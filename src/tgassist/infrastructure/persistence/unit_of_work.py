"""SQLAlchemy unit of work.

Holds one transaction open on the database thread for the duration of a use
case, so that everything the use case writes commits together or not at all.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Self

from sqlalchemy import Connection
from sqlalchemy.engine import Transaction
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from tgassist.domain.errors import (
    ConstraintViolationError,
    DatabaseUnavailableError,
    TransactionFailedError,
)
from tgassist.domain.events import DomainEvent
from tgassist.domain.ports.unit_of_work import UnitOfWork
from tgassist.infrastructure.logging import get_logger
from tgassist.infrastructure.persistence.engine import (
    SqliteDatabase,
    release_autobegun_transaction,
)

TRANSACTION_WAIT_SECONDS = 30.0
"""How long a unit of work waits for the transaction lock before failing."""


def translate_database_error(exc: BaseException, *, operation: str) -> Exception:
    """Convert a driver or SQLAlchemy exception into the domain taxonomy.

    Normalising here rather than at each call site is what allows a use case to
    handle "the write violated a constraint" without importing SQLAlchemy, and
    what keeps a driver upgrade from rippling through the application.
    """
    if isinstance(exc, IntegrityError):
        return ConstraintViolationError(
            f"{operation} violated a database constraint: {exc.orig}",
            user_message="That change conflicts with existing data.",
            context={"operation": operation},
            cause=exc,
        )
    if isinstance(exc, DBAPIError) and _is_locked(exc):
        return DatabaseUnavailableError(
            f"{operation} could not acquire the database: {exc.orig}",
            user_message="The database is busy. Retrying.",
            context={"operation": operation},
            cause=exc,
        )
    if isinstance(exc, (SQLAlchemyError, sqlite3.Error)):
        return TransactionFailedError(
            f"{operation} failed: {exc}",
            user_message="The operation could not be completed.",
            context={"operation": operation},
            cause=exc,
        )
    return TransactionFailedError(
        f"{operation} failed unexpectedly: {exc}",
        user_message="The operation could not be completed.",
        context={"operation": operation},
        cause=exc,
    )


def _is_locked(exc: DBAPIError) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return "locked" in message or "busy" in message


class SqlAlchemyUnitOfWork(UnitOfWork):
    """A transaction on the shared connection, driven from the event loop.

    Repositories enlist by taking :attr:`connection`; they never open one of
    their own, which is what makes composing several of them atomic.
    """

    __slots__ = (
        "_committed",
        "_connection",
        "_database",
        "_events",
        "_holds_lock",
        "_log",
        "_started_at",
        "_transaction",
    )

    def __init__(self, database: SqliteDatabase) -> None:
        """Create an unstarted unit of work."""
        self._database = database
        self._connection: Connection | None = None
        self._transaction: Transaction | None = None
        self._events: list[DomainEvent] = []
        self._committed = False
        self._holds_lock = False
        self._started_at = 0.0
        self._log = get_logger(__name__)

    # -- Lifecycle --------------------------------------------------------

    async def __aenter__(self) -> Self:
        """Begin a transaction."""
        await self.begin()
        return self

    async def begin(self) -> None:
        """Begin a transaction.

        Raises:
            TransactionFailedError: If a transaction is already open. Two
                overlapping boundaries mean neither is a boundary, so nesting is
                refused rather than silently flattened.
        """
        if self._transaction is not None:
            msg = "This unit of work already has an open transaction"
            raise TransactionFailedError(
                msg,
                user_message="An internal error occurred while saving.",
                context={"state": "already_active"},
            )
        self._committed = False
        self._events.clear()
        self._started_at = time.monotonic()

        # One connection holds one transaction, so units of work take turns.
        # A bounded wait turns the pathological case -- two transactions
        # overlapping in the same task -- into a diagnosable error rather than
        # a silent hang.
        try:
            await asyncio.wait_for(
                self._database.transaction_lock.acquire(), timeout=TRANSACTION_WAIT_SECONDS
            )
        except TimeoutError as exc:
            msg = (
                f"Timed out after {TRANSACTION_WAIT_SECONDS}s waiting for the database "
                "transaction lock; another unit of work is still open"
            )
            raise TransactionFailedError(
                msg,
                user_message="The database is busy. Please try again.",
                context={"state": "lock_timeout"},
                cause=exc,
            ) from exc
        self._holds_lock = True

        connection = self._database.connection
        try:
            self._connection = self._database.connection
            # Release any transaction autobegun by a read outside a unit of
            # work -- a health check or a schema query. Only reads happen there,
            # so there is nothing to lose, and leaving one open would make this
            # begin() fail.
            await self._run(lambda: release_autobegun_transaction(connection))
            self._transaction = await self._run(self._connection.begin)
        except Exception as exc:
            self._connection = None
            self._transaction = None
            self._release_lock()
            raise translate_database_error(exc, operation="begin") from exc

    async def commit(self) -> None:
        """Make every change in this transaction durable.

        Raises:
            TransactionFailedError: If no transaction is open, or the commit
                fails. A failed commit rolls back before raising.
        """
        transaction = self._require_active("commit")
        try:
            await self._run(transaction.commit)
        except Exception as exc:
            await self._safe_rollback()
            raise translate_database_error(exc, operation="commit") from exc
        self._committed = True
        self._transaction = None
        self._release_lock()
        self._log.debug(
            "transaction_committed",
            duration_ms=round((time.monotonic() - self._started_at) * 1000, 2),
            events=len(self._events),
        )

    async def rollback(self) -> None:
        """Discard every change in this transaction. Safe at any point."""
        if self._transaction is None:
            return
        transaction, self._transaction = self._transaction, None
        self._events.clear()
        try:
            await self._run(transaction.rollback)
        except Exception as exc:
            raise translate_database_error(exc, operation="rollback") from exc
        finally:
            self._release_lock()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless committed, then release resources."""
        del exc_type, exc, traceback
        try:
            if self._transaction is not None:
                await self.rollback()
        finally:
            self._connection = None
            self._release_lock()

    # -- Savepoints -------------------------------------------------------

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        """Open a nested savepoint for partial rollback within the transaction.

        This is the one legitimate form of nesting: a bulk operation that should
        skip a bad record rather than discard the whole batch. It is deliberately
        distinct from nesting a second unit of work, which would obscure where
        the real transaction boundary is.
        """
        connection = self._require_connection("savepoint")
        nested = await self._run(connection.begin_nested)
        try:
            yield
        except Exception:
            await self._run(nested.rollback)
            raise
        else:
            await self._run(nested.commit)

    # -- Events -----------------------------------------------------------

    def add_event(self, event: DomainEvent) -> None:
        """Record an event to be published after a successful commit."""
        self._events.append(event)

    def collect_events(self) -> Sequence[DomainEvent]:
        """Return and clear the recorded events, or nothing if not committed.

        Withholding events until commit is what makes "never announce a fact
        that was rolled back" structural. A caller cannot publish prematurely,
        because there is nothing to publish.
        """
        if not self._committed:
            return ()
        collected = tuple(self._events)
        self._events.clear()
        return collected

    # -- State ------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Report whether a transaction is currently open."""
        return self._transaction is not None

    @property
    def is_committed(self) -> bool:
        """Report whether this unit of work committed successfully."""
        return self._committed

    @property
    def connection(self) -> Connection:
        """Return the connection this transaction runs on.

        Repositories use this to enlist. Reaching for a different connection
        would place the work outside the transaction.
        """
        return self._require_connection("connection")

    @property
    def database(self) -> SqliteDatabase:
        """Return the owning database, for repositories needing the executor."""
        return self._database

    # -- Internals --------------------------------------------------------

    async def _run[R](self, func: Callable[[], R]) -> R:
        """Run a blocking database call on the database thread."""
        return await self._database.executor.run(func)

    def _require_active(self, operation: str) -> Transaction:
        if self._transaction is None:
            msg = f"Cannot {operation}: no transaction is open"
            raise TransactionFailedError(
                msg,
                user_message="An internal error occurred while saving.",
                context={"operation": operation, "state": "inactive"},
            )
        return self._transaction

    def _require_connection(self, operation: str) -> Connection:
        if self._connection is None:
            msg = f"Cannot {operation}: no transaction is open"
            raise TransactionFailedError(
                msg,
                user_message="An internal error occurred while saving.",
                context={"operation": operation, "state": "inactive"},
            )
        return self._connection

    def _release_lock(self) -> None:
        """Release the transaction lock exactly once."""
        if self._holds_lock:
            self._holds_lock = False
            self._database.transaction_lock.release()

    async def _safe_rollback(self) -> None:
        transaction, self._transaction = self._transaction, None
        if transaction is None:
            return
        try:
            await self._run(transaction.rollback)
        except Exception:
            # Already failing; a rollback failure must not mask the commit
            # failure that caused it.
            self._log.exception("rollback_after_failed_commit_failed")
        finally:
            self._events.clear()
            self._release_lock()


class UnitOfWorkFactory:
    """Creates units of work bound to one database.

    Use cases receive this rather than a unit of work: a use case decides when
    its transaction begins, and an injected open transaction would outlive the
    operation it was meant to bound.
    """

    __slots__ = ("_database",)

    def __init__(self, database: SqliteDatabase) -> None:
        """Bind the factory to a database."""
        self._database = database

    def __call__(self) -> SqlAlchemyUnitOfWork:
        """Return a new, unstarted unit of work."""
        return SqlAlchemyUnitOfWork(self._database)
