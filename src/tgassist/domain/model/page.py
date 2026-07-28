"""Keyset pagination.

Every collection query in this application is paginated, and every one uses a
**cursor** rather than a numeric offset.

The reason is the shape of the data. ``OFFSET 50000`` makes the database walk
and discard fifty thousand rows to return the next twenty, so paging through a
long chat history gets slower the further back you scroll — exactly the wrong
performance curve for a message list. A cursor encodes *where the last page
ended*, so every page costs the same regardless of depth.

Cursors are opaque to callers. The encoding is an implementation detail of the
repository that issued them, and passing a cursor from one query to another is
meaningless rather than merely wrong.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results and the cursor that continues it.

    Attributes:
        items: The rows in this page, in query order.
        next_cursor: Opaque token fetching the following page, or ``None`` when
            this is the last page.
        has_more: Whether a following page exists. Distinct from
            ``next_cursor is not None`` only in that it reads clearly at call
            sites; the two are always consistent.
    """

    items: Sequence[T]
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        """Report whether a following page exists."""
        return self.next_cursor is not None

    def __iter__(self) -> Iterator[T]:
        """Iterate the items in this page."""
        return iter(self.items)

    def __len__(self) -> int:
        """Return the number of items in this page."""
        return len(self.items)

    def __bool__(self) -> bool:
        """Report whether this page contains any items."""
        return bool(self.items)

    @classmethod
    def empty(cls) -> Page[T]:
        """Return a page with no items and no continuation."""
        return cls(items=())


def clamp_page_size(requested: int | None) -> int:
    """Constrain a requested page size to a sane range.

    A caller asking for a million rows is either mistaken or attempting to
    exhaust memory; either way the request is capped rather than honoured.

    Args:
        requested: The caller's page size, or ``None`` for the default.

    Returns:
        A page size between 1 and :data:`MAX_PAGE_SIZE`.
    """
    if requested is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(requested, MAX_PAGE_SIZE))
