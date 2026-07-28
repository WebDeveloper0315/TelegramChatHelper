"""Keyset pagination for the in-memory fakes.

Every collection fake needs this, and it is exactly the code that is easy to get
subtly wrong in a way the contract suite would then pass for the wrong reason --
a comparison that should be strict, a cursor emitted on the last page, a missing
tiebreaker. Written once here, it is verified by every aggregate's contract run
rather than once per fake.

Deliberately independent of ``KeysetPaginator``: the point of the fakes is that
they were written separately, so a bug in the production paginator does not
reproduce itself in the thing meant to check it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.infrastructure.persistence.cursor import Cursor
from tgassist.infrastructure.persistence.pagination import SORT_KEY, TIEBREAK_KEY


def paginate[T](
    items: Sequence[T],
    request: PageRequest,
    *,
    sort_key: Callable[[T], tuple[datetime, int]],
    identity: Callable[[T], int],
) -> Page[T]:
    """Return one keyset page of ``items``.

    Args:
        items: Everything visible to the caller, in any order.
        request: Cursor, limit and sort direction.
        sort_key: The ordering pair -- sort column and unique tiebreaker. The
            tiebreaker is not optional: ordering by a non-unique column alone
            silently skips rows when a page boundary falls inside a group of
            equal values.
        identity: The tiebreaker value alone, for encoding the next cursor.

    Returns:
        A page, with ``next_cursor`` set only when more rows remain.
    """
    descending = request.sort is None or request.sort.direction.is_descending
    ordered = sorted(items, key=sort_key, reverse=descending)

    position = Cursor.decode(request.cursor)
    if position is not None and SORT_KEY in position and TIEBREAK_KEY in position:
        marker = (
            datetime.fromisoformat(str(position[SORT_KEY])),
            int(position[TIEBREAK_KEY]),
        )
        # Strictly past the marker, so the row the cursor names is not repeated.
        ordered = [
            item
            for item in ordered
            if (sort_key(item) < marker if descending else sort_key(item) > marker)
        ]

    limit = request.effective_limit()
    page_items = ordered[:limit]
    next_cursor = (
        Cursor.encode(
            {
                SORT_KEY: sort_key(page_items[-1])[0].isoformat(),
                TIEBREAK_KEY: identity(page_items[-1]),
            }
        )
        if len(ordered) > limit and page_items
        else None
    )
    return Page(items=page_items, next_cursor=next_cursor)


__all__ = ["paginate"]
