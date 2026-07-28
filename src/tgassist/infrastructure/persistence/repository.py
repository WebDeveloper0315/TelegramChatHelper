"""Generic repository infrastructure.

Only the machinery every repository needs: transaction-aware execution, keyset
pagination and error normalisation. Business repositories arrive with Milestone
1 and the entities they store.

The base deliberately does **not** provide a generic ``find(**criteria)`` or a
query builder. A repository whose interface is "any query you like" is a
database connection with extra steps, and the point of the pattern is that a
repository exposes the handful of intention-revealing queries the application
actually makes, each of which can then be indexed and tested.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import Executable, Row, Select
from sqlalchemy.engine import CursorResult

from tgassist.domain.errors import ConstraintViolationError, RecordNotFoundError
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.infrastructure.persistence.pagination import KeysetPaginator
from tgassist.infrastructure.persistence.unit_of_work import (
    SqlAlchemyUnitOfWork,
    translate_database_error,
)


class Repository[T]:
    """Base for repositories that read and write within a unit of work.

    A repository never opens its own connection or transaction. It enlists in
    the one the unit of work holds, which is what allows a use case to compose
    several repositories and still commit or roll back as a whole.
    """

    __slots__ = ("_uow",)

    def __init__(self, uow: SqlAlchemyUnitOfWork) -> None:
        """Bind the repository to the transaction it will run in."""
        self._uow = uow

    # -- Execution --------------------------------------------------------

    async def execute(
        self, statement: Executable, *, operation: str = "query"
    ) -> CursorResult[Any]:
        """Run a statement on the database thread inside the transaction.

        Raises:
            PersistenceError: Driver and SQLAlchemy failures are normalised into
                the domain taxonomy, so callers never see a library exception.
        """
        connection = self._uow.connection
        try:
            return await self._uow.database.executor.run(connection.execute, statement)
        except Exception as exc:
            raise translate_database_error(exc, operation=operation) from exc

    async def fetch_all(
        self, statement: Executable, *, operation: str = "query"
    ) -> Sequence[Row[Any]]:
        """Run a statement and return every row."""
        result = await self.execute(statement, operation=operation)
        return result.fetchall()

    async def fetch_one(
        self, statement: Executable, *, operation: str = "query"
    ) -> Row[Any] | None:
        """Run a statement and return the first row, or ``None``."""
        result = await self.execute(statement, operation=operation)
        return result.fetchone()

    async def fetch_scalar(self, statement: Executable, *, operation: str = "query") -> Any:
        """Run a statement and return the first column of the first row."""
        result = await self.execute(statement, operation=operation)
        return result.scalar_one_or_none()

    async def require_one(
        self,
        statement: Executable,
        *,
        entity: str,
        operation: str = "query",
    ) -> Row[Any]:
        """Run a statement and return the first row, raising if there is none.

        Used only where absence genuinely is an error. Ordinary lookups return
        ``None``, because "not found" is usually an expected state and raising
        for it forces every caller into a try/except.

        Raises:
            RecordNotFoundError: If the statement returned no rows.
        """
        row = await self.fetch_one(statement, operation=operation)
        if row is None:
            msg = f"No {entity} matched the query"
            raise RecordNotFoundError(
                msg,
                user_message=f"The requested {entity} was not found.",
                context={"entity": entity, "operation": operation},
            )
        return row

    # -- Pagination -------------------------------------------------------

    async def fetch_page[R](
        self,
        statement: Select[Any],
        *,
        paginator: KeysetPaginator,
        request: PageRequest,
        mapper: Callable[[Row[Any]], R],
        operation: str = "page",
    ) -> Page[R]:
        """Fetch one keyset page.

        The paginator owns the ordering, the position predicate and the
        lookahead row, so a repository supplies only its base query and the row
        mapper. Centralising it is what keeps the tiebreaker requirement -- the
        thing that stops rows being silently skipped -- from being each
        repository's problem to remember.
        """
        rows = await self.fetch_all(paginator.apply(statement, request), operation=operation)
        return paginator.build_page(rows, request, mapper)

    # -- Write helpers ----------------------------------------------------

    async def execute_write(
        self,
        statement: Executable,
        *,
        operation: str,
        conflict_message: str | None = None,
    ) -> CursorResult[Any]:
        """Run a write, reporting constraint violations with useful context.

        A bare "UNIQUE constraint failed" tells a user nothing. ``conflict_message``
        lets the repository say which invariant was violated, in terms of the
        domain rather than the schema.
        """
        try:
            return await self.execute(statement, operation=operation)
        except ConstraintViolationError as exc:
            if conflict_message is None:
                raise
            raise ConstraintViolationError(
                exc.message,
                user_message=conflict_message,
                context=exc.context,
                cause=exc.cause,
            ) from exc

    def add_event(self, event: Any) -> None:
        """Record a domain event for publication after the transaction commits."""
        self._uow.add_event(event)

    @property
    def uow(self) -> SqlAlchemyUnitOfWork:
        """Return the unit of work this repository is enlisted in."""
        return self._uow
