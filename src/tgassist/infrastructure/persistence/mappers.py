"""Row-to-domain mapping utilities.

Mapping is explicit and hand-written (ADR-015). The ORM alternative is shorter
to write and produces objects that carry a session, a load state and a lazy
relationship graph -- persistence concerns that then travel into the domain and
undermine the independence the architecture is built on.

The price is a mapper per entity. The compensation is that mapping is ordinary
code: readable, debuggable, and provable by a round-trip property test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import Row


class RowMapper[T_co](Protocol):
    """Converts a database row into a domain object.

    Implementations are pure functions of the row. A mapper that reads the
    clock, queries the database or mutates anything is a defect, because a
    mapper that is not pure cannot be round-trip tested.
    """

    def to_domain(self, row: Row[Any]) -> T_co:
        """Build a domain object from a database row."""
        ...


class BidirectionalMapper[T](Protocol):
    """Converts between a database row and a domain object.

    The round-trip must be lossless: mapping a domain object to parameters and
    back must produce an equal object. That property is what a mapper test
    asserts, and it is what catches a forgotten column.
    """

    def to_domain(self, row: Row[Any]) -> T:
        """Build a domain object from a database row."""
        ...

    def to_params(self, entity: T) -> dict[str, Any]:
        """Build insert or update parameters from a domain object."""
        ...


# ---------------------------------------------------------------------------
# Value conversion
#
# SQLite has no native datetime, boolean or JSON type. These helpers put the
# conversions in one place so that "how is a timestamp stored" has a single
# answer, and so the PostgreSQL path (ADR-016) has one set of functions to
# change rather than a convention scattered across every mapper.
# ---------------------------------------------------------------------------


def to_stored_datetime(value: datetime | None) -> str | None:
    """Encode an instant for storage as ISO-8601 UTC text.

    Raises:
        ValueError: If the datetime is naive. A naive value has no defined
            instant, so storing one would record something that cannot be
            interpreted later -- the class of bug the Clock port exists to
            prevent, caught here at the storage boundary too.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        msg = "Refusing to store a naive datetime; all instants must be timezone-aware UTC"
        raise ValueError(msg)
    return value.astimezone(UTC).isoformat()


def from_stored_datetime(value: str | None) -> datetime | None:
    """Decode a stored instant into a timezone-aware UTC datetime."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        # Tolerated on read for databases written before this rule existed;
        # such a value is interpreted as UTC because that is what it was.
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_stored_bool(value: bool | None) -> int | None:
    """Encode a boolean as an integer, which is how SQLite stores one."""
    if value is None:
        return None
    return int(value)


def from_stored_bool(value: int | bool | None) -> bool | None:
    """Decode a stored boolean."""
    if value is None:
        return None
    return bool(value)


def to_stored_json(value: Any) -> str | None:
    """Encode a structure as JSON text.

    Sorted keys and no incidental whitespace, so that two equal structures
    produce byte-identical text. Content fingerprints and cache keys depend on
    that; an unsorted dump would invalidate caches at random.
    """
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def from_stored_json(value: str | None) -> Any:
    """Decode JSON text."""
    if value is None:
        return None
    return json.loads(value)


def row_to_dict(row: Row[Any]) -> dict[str, Any]:
    """Return a row as a plain dictionary, detached from the result set."""
    return dict(row._mapping)
