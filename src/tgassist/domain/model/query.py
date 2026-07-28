"""Query description value objects.

These let a use case say *what* it wants -- "the newest twenty messages in this
chat, continuing from here" -- without saying how to get it. They are domain
objects because the intent is a business concern; only the translation into SQL
belongs to infrastructure.

Deliberately small. There is no filter tree, no predicate combinator and no
query language here. Repositories expose named, intention-revealing methods
(``list_recent_by_chat``, not ``find(**criteria)``), so the only parameters that
genuinely recur across them are ordering and position. A richer query object
would be a database connection wearing a hat, and it would move query
construction out of the one place where an index can be designed for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


class SortDirection(StrEnum):
    """Which way a sort runs."""

    ASCENDING = "asc"
    DESCENDING = "desc"

    @property
    def is_descending(self) -> bool:
        """Report whether this direction is descending."""
        return self is SortDirection.DESCENDING

    def inverted(self) -> SortDirection:
        """Return the opposite direction."""
        return SortDirection.ASCENDING if self.is_descending else SortDirection.DESCENDING


@dataclass(frozen=True, slots=True)
class SortOrder:
    """A field to sort by and the direction to sort it.

    ``field`` is a **domain** field name, not a column. The repository maps it
    to a column, which is what stops a rename in the schema from reaching the
    application layer, and what stops a caller from ordering by a column that
    has no index.
    """

    field: str
    direction: SortDirection = SortDirection.DESCENDING

    def __post_init__(self) -> None:
        """Reject an empty field name."""
        if not self.field:
            msg = "A sort order requires a field name"
            raise ValueError(msg)

    @classmethod
    def newest_first(cls, field: str = "created_at") -> Self:
        """Return a descending order, the common case for activity lists."""
        return cls(field=field, direction=SortDirection.DESCENDING)

    @classmethod
    def oldest_first(cls, field: str = "created_at") -> Self:
        """Return an ascending order."""
        return cls(field=field, direction=SortDirection.ASCENDING)


@dataclass(frozen=True, slots=True)
class PageRequest:
    """Where to start a page, how many rows, and in what order.

    Attributes:
        cursor: Opaque token from a previous page, or ``None`` to start at the
            beginning. Its encoding belongs to the repository that issued it,
            and passing one to a different query is meaningless.
        limit: Requested row count, clamped by :meth:`effective_limit`.
        sort: Ordering. ``None`` lets the repository apply its natural order,
            which is usually the one its index supports.
    """

    cursor: str | None = None
    limit: int = DEFAULT_PAGE_SIZE
    sort: SortOrder | None = None

    def effective_limit(self) -> int:
        """Return the limit constrained to a sane range.

        A caller asking for a million rows is either mistaken or trying to
        exhaust memory. Either way the request is capped rather than honoured.
        """
        return max(1, min(self.limit, MAX_PAGE_SIZE))

    def first_page(self) -> Self:
        """Return the same request positioned at the beginning."""
        return type(self)(cursor=None, limit=self.limit, sort=self.sort)

    def continuing_from(self, cursor: str | None) -> Self:
        """Return the same request positioned at ``cursor``."""
        return type(self)(cursor=cursor, limit=self.limit, sort=self.sort)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A closed interval between two instants.

    Used by anything that reports over a period: relationship metrics, retention
    evaluation, activity summaries.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """Reject naive or inverted windows."""
        if self.start.tzinfo is None or self.end.tzinfo is None:
            msg = "A time window requires timezone-aware instants"
            raise ValueError(msg)
        if self.start > self.end:
            msg = f"A time window cannot end before it starts: {self.start} > {self.end}"
            raise ValueError(msg)

    @property
    def duration_seconds(self) -> float:
        """Return the length of the window in seconds."""
        return (self.end - self.start).total_seconds()

    def contains(self, instant: datetime) -> bool:
        """Report whether an instant falls within the window, inclusive."""
        if instant.tzinfo is None:
            msg = "Cannot test a naive datetime against a time window"
            raise ValueError(msg)
        return self.start <= instant <= self.end
