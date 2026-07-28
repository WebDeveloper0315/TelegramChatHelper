"""In-memory unit of work fake.

Honours the same transaction semantics as the real one -- explicit commit,
rollback on exit, events withheld until commit, nesting refused -- so a use-case
test can exercise transaction behaviour with no database at all.

Written as an independent implementation rather than a subclass of the
SQLAlchemy one, so the shared contract suite genuinely tests it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self

from tgassist.domain.errors import TransactionFailedError
from tgassist.domain.events import DomainEvent
from tgassist.domain.ports.unit_of_work import UnitOfWork


class InMemoryUnitOfWork(UnitOfWork):
    """A transaction over a dictionary.

    Writes are staged and only merged into the backing store on commit, which is
    what makes rollback meaningful rather than decorative.
    """

    __slots__ = (
        "_active",
        "_committed",
        "_events",
        "_savepoint_depth",
        "_staged",
        "committed_data",
    )

    def __init__(self) -> None:
        """Create an unstarted unit of work."""
        self.committed_data: dict[str, Any] = {}
        self._staged: dict[str, Any] = {}
        self._events: list[DomainEvent] = []
        self._active = False
        self._committed = False
        self._savepoint_depth = 0

    async def __aenter__(self) -> Self:
        await self.begin()
        return self

    async def begin(self) -> None:
        if self._active:
            msg = "This unit of work already has an open transaction"
            raise TransactionFailedError(msg, context={"state": "already_active"})
        self._active = True
        self._committed = False
        self._staged = dict(self.committed_data)
        self._events.clear()

    async def commit(self) -> None:
        if not self._active:
            msg = "Cannot commit: no transaction is open"
            raise TransactionFailedError(msg, context={"state": "inactive"})
        self.committed_data = dict(self._staged)
        self._active = False
        self._committed = True

    async def rollback(self) -> None:
        if not self._active:
            return
        self._staged = dict(self.committed_data)
        self._events.clear()
        self._active = False

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        if self._active:
            await self.rollback()

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        if not self._active:
            msg = "Cannot savepoint: no transaction is open"
            raise TransactionFailedError(msg, context={"state": "inactive"})
        snapshot = dict(self._staged)
        self._savepoint_depth += 1
        try:
            yield
        except Exception:
            self._staged = snapshot
            raise
        finally:
            self._savepoint_depth -= 1

    def add_event(self, event: DomainEvent) -> None:
        self._events.append(event)

    def collect_events(self) -> Sequence[DomainEvent]:
        if not self._committed:
            return ()
        collected = tuple(self._events)
        self._events.clear()
        return collected

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def is_committed(self) -> bool:
        return self._committed

    # -- Test-facing storage ---------------------------------------------

    def put(self, key: str, value: Any) -> None:
        """Stage a value, visible only after commit."""
        if not self._active:
            msg = "Cannot write: no transaction is open"
            raise TransactionFailedError(msg, context={"state": "inactive"})
        self._staged[key] = value

    def get(self, key: str) -> Any:
        """Read a staged value within the transaction, or a committed one outside it."""
        source = self._staged if self._active else self.committed_data
        return source.get(key)


class InMemoryUnitOfWorkFactory:
    """Creates in-memory units of work that share one backing store.

    Sharing the store across units of work is what allows a test to assert that
    a rolled-back transaction left nothing behind for the next one.
    """

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def __call__(self) -> InMemoryUnitOfWork:
        uow = InMemoryUnitOfWork()
        uow.committed_data = self._store
        return uow

    @property
    def store(self) -> dict[str, Any]:
        """Return the shared backing store."""
        return self._store
