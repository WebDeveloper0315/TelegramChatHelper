"""Unit of work port: the transaction boundary.

One use case is one transaction. That rule is what makes "either all of this
happened or none of it did" a property of the design rather than a hope — a
message ingest that persisted the message but failed before advancing the sync
cursor would silently re-ingest forever.

Repositories never commit. They enlist in whatever transaction the unit of work
opened, which is why a use case can compose several of them and still get
atomicity.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from tgassist.domain.events import DomainEvent


@runtime_checkable
class UnitOfWork(Protocol):
    """Defines a transaction boundary.

    Contract, guaranteed by every implementation and verified by the shared
    contract test suite:

    1. **Entering begins a transaction.** Leaving without :meth:`commit` rolls
       back. There is no implicit commit: forgetting to commit loses work
       loudly rather than persisting a half-finished operation quietly.
    2. **Commit is explicit and final.** Committing twice raises, because the
       second call means the caller has lost track of the boundary.
    3. **Rollback is safe at any point**, including after a failed commit and
       including when nothing was written.
    4. **Events are released only after a successful commit.**
       :meth:`collect_events` returns nothing until the transaction has
       committed, so a handler can never observe a fact that was rolled back.
       This is structural rather than a convention a caller must remember.
    5. **Nesting is refused.** Re-entering an active unit of work raises. Two
       overlapping boundaries mean neither is a boundary; a use case needing
       partial rollback uses :meth:`savepoint` instead, which is explicit about
       what it does.
    6. **Cleanup is automatic.** Leaving the context releases the connection
       whether the transaction committed, rolled back or raised.
    """

    async def __aenter__(self) -> Self:
        """Begin a transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back unless :meth:`commit` succeeded, then release resources."""
        ...

    async def commit(self) -> None:
        """Make every change in this transaction durable."""
        ...

    async def rollback(self) -> None:
        """Discard every change in this transaction."""
        ...

    def savepoint(self) -> AbstractAsyncContextManager[None]:
        """Open a nested savepoint for partial rollback within the transaction.

        Used where one failure should not discard the whole operation -- a bulk
        import that skips a malformed row, for example. Leaving the context with
        an exception rolls back to the savepoint and re-raises; leaving cleanly
        releases it. The enclosing transaction is unaffected either way.
        """
        ...

    def add_event(self, event: DomainEvent) -> None:
        """Record an event to be published after a successful commit."""
        ...

    def collect_events(self) -> Sequence[DomainEvent]:
        """Return and clear the recorded events.

        Returns an empty sequence unless the transaction committed. Calling this
        twice returns the events once, so a caller cannot publish them twice.
        """
        ...

    @property
    def is_active(self) -> bool:
        """Report whether a transaction is currently open."""
        ...

    @property
    def is_committed(self) -> bool:
        """Report whether this unit of work committed successfully."""
        ...


@runtime_checkable
class UnitOfWorkFactory(Protocol):
    """Creates units of work.

    Use cases receive a factory rather than a unit of work, because a use case
    decides when its transaction starts and a long-lived injected transaction
    would outlive the operation it was meant to bound.
    """

    def __call__(self) -> UnitOfWork:
        """Return a new, unstarted unit of work."""
        ...


__all__ = ["AsyncIterator", "UnitOfWork", "UnitOfWorkFactory"]
