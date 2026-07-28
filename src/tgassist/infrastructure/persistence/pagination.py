"""Keyset pagination.

Translates a domain :class:`PageRequest` into an ordered, bounded SQL query and
turns the result back into a :class:`Page`.

Why keyset rather than offset
-----------------------------

``OFFSET 50000`` makes the database read and discard fifty thousand rows to
return the next twenty. Paging back through a long chat history therefore gets
slower the further back you scroll -- the opposite of what a message list needs.
A cursor encodes where the previous page ended, so every page costs the same.

Offset pagination is also *incorrect* on changing data: if a row is inserted
while a user pages, every subsequent page shifts by one and a row is silently
skipped. Keyset pagination is stable under concurrent inserts because it
positions by value rather than by count.

Why a tiebreaker is mandatory
-----------------------------

This is the subtle part, and the reason this module exists rather than each
repository writing its own ``WHERE``.

Paginating by a non-unique column is broken. Order ten messages by ``sent_at``
where three share a timestamp, take a page ending inside that group, and
continue with ``WHERE sent_at < :last`` -- the other two rows with the same
timestamp are skipped. Use ``<=`` instead and they repeat forever.

The fix is to sort by ``(sort_column, unique_column)`` and compare the pair. This
module therefore **requires** a unique tiebreaker column and will not build a
query without one, because the failure it prevents is silent: rows go missing
from a list and nothing reports an error.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, Row, Select, and_, or_

from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest, SortDirection, SortOrder
from tgassist.infrastructure.persistence.cursor import Cursor

SORT_KEY = "s"
TIEBREAK_KEY = "t"

_COERCIONS: dict[type, Callable[[Any], Any]] = {
    datetime: lambda value: datetime.fromisoformat(str(value)),
    date: lambda value: date.fromisoformat(str(value)),
    bool: bool,
    int: int,
    float: float,
    str: str,
}
"""How to restore each column type from its JSON representation."""


class KeysetPaginator:
    """Applies keyset pagination to a select statement.

    Args:
        sort_column: The column the caller orders by. May contain duplicates.
        tiebreak_column: A **unique** column, almost always the primary key. It
            makes the composite sort key unique, which is what makes the page
            boundary unambiguous.
        sort_field: The domain field name this paginator serves, used to reject
            a ``PageRequest`` asking for a field this query cannot order by.
    """

    __slots__ = ("_sort_column", "_sort_field", "_tiebreak_column")

    def __init__(
        self,
        *,
        sort_column: ColumnElement[Any],
        tiebreak_column: ColumnElement[Any],
        sort_field: str,
    ) -> None:
        """Create a paginator for one sort key."""
        self._sort_column = sort_column
        self._tiebreak_column = tiebreak_column
        self._sort_field = sort_field

    def resolve_direction(self, request: PageRequest) -> SortDirection:
        """Return the direction to apply, rejecting an unsupported sort field.

        Raises:
            ValueError: If the request asks to sort by a field this query does
                not support. Silently ignoring it would return correctly-shaped
                results in the wrong order, which is worse than an error.
        """
        if request.sort is None:
            return SortDirection.DESCENDING
        if request.sort.field != self._sort_field:
            msg = (
                f"This query can only be ordered by {self._sort_field!r}, "
                f"not {request.sort.field!r}"
            )
            raise ValueError(msg)
        return request.sort.direction

    def apply(self, statement: Select[Any], request: PageRequest) -> Select[Any]:
        """Return the statement ordered, positioned and limited.

        One row more than requested is fetched, so that :meth:`build_page` can
        tell whether a further page exists. The alternative -- a second
        ``COUNT(*)`` -- doubles the work to answer a question the extra row has
        already answered.
        """
        direction = self.resolve_direction(request)
        statement = statement.order_by(*self._order_by(direction))

        position = Cursor.decode(request.cursor)
        if position is not None and SORT_KEY in position and TIEBREAK_KEY in position:
            statement = statement.where(
                self._after(
                    self._coerce(self._sort_column, position[SORT_KEY]),
                    self._coerce(self._tiebreak_column, position[TIEBREAK_KEY]),
                    direction,
                )
            )

        return statement.limit(request.effective_limit() + 1)

    def build_page[T](
        self,
        rows: Sequence[Row[Any]],
        request: PageRequest,
        mapper: Callable[[Row[Any]], T],
    ) -> Page[T]:
        """Turn the fetched rows into a page, trimming the lookahead row."""
        limit = request.effective_limit()
        has_more = len(rows) > limit
        page_rows = rows[:limit]

        next_cursor: str | None = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = Cursor.encode(
                {
                    SORT_KEY: last._mapping[self._sort_column],
                    TIEBREAK_KEY: last._mapping[self._tiebreak_column],
                }
            )

        return Page(items=[mapper(row) for row in page_rows], next_cursor=next_cursor)

    # -- Internals --------------------------------------------------------

    @staticmethod
    def _coerce(column: ColumnElement[Any], value: Any) -> Any:
        """Restore a cursor value to the column's own Python type.

        A cursor is JSON, and JSON has no datetime. Encoding turns one into a
        string, and binding that string against a ``DateTime`` column compares
        text with a timestamp -- which does not error, it just quietly returns
        the wrong rows. That is the silent row-skipping this module exists to
        prevent, so the value is converted back before it reaches the query.
        """
        if value is None:
            return None
        try:
            target = column.type.python_type
        except (NotImplementedError, AttributeError):
            return value
        if isinstance(value, target):
            return value
        coerce = _COERCIONS.get(target)
        return coerce(value) if coerce is not None else value

    def _order_by(self, direction: SortDirection) -> tuple[ColumnElement[Any], ...]:
        if direction.is_descending:
            return (self._sort_column.desc(), self._tiebreak_column.desc())
        return (self._sort_column.asc(), self._tiebreak_column.asc())

    def _after(
        self, sort_value: Any, tiebreak_value: Any, direction: SortDirection
    ) -> ColumnElement[bool]:
        """Build the predicate selecting rows strictly past the cursor position.

        Expressed as an explicit disjunction rather than a row-value comparison
        (``(a, b) < (:a, :b)``). Row values are terser and both SQLite and
        PostgreSQL support them, but query planners optimise the disjunction
        more consistently against a composite index, and index use is the entire
        point of paginating this way.
        """
        if direction.is_descending:
            return or_(
                self._sort_column < sort_value,
                and_(
                    self._sort_column == sort_value,
                    self._tiebreak_column < tiebreak_value,
                ),
            )
        return or_(
            self._sort_column > sort_value,
            and_(
                self._sort_column == sort_value,
                self._tiebreak_column > tiebreak_value,
            ),
        )


def sort_orders_for(field: str) -> tuple[SortOrder, SortOrder]:
    """Return both directions for a field, for use in tests and defaults."""
    return (SortOrder.newest_first(field), SortOrder.oldest_first(field))
