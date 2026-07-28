"""The mapping framework.

Mapping is hand-written and explicit (ADR-015). This module defines what a
mapper must guarantee; the per-aggregate mappers arrive with their aggregates.

The contract
------------

A mapper converts between a **domain model** -- a frozen dataclass with
validated invariants and no persistence concerns -- and a **persistence model**,
a flat dictionary of column values. It must satisfy four properties, and the
first is the one that catches real bugs:

1. **Round-trip fidelity.** ``to_domain(row_from(to_params(entity))) == entity``.
   This is what catches a column added to the schema and forgotten in the
   mapper, which otherwise surfaces as a field that silently reverts to its
   default after a save.
2. **Purity.** A mapper reads no clock, issues no query and mutates nothing. Its
   output depends only on its input, which is what makes property-based
   round-trip testing possible at all.
3. **Total conversion.** Every column the mapper claims is converted. Partial
   mapping is how a value gets written in one representation and read in
   another.
4. **Identity preservation.** The identifier survives the round trip unchanged.

Why the round trip is not exactly symmetric
-------------------------------------------

Some fields are database-assigned: an autoincrement primary key, a trigger-set
timestamp. ``to_params`` omits those on insert, so the round trip is stated over
an entity that already has its identity. :func:`assert_round_trip` therefore
compares the domain object after a full cycle, not the parameter dictionaries.

Schema evolution
----------------

A mapper reads what the *current* schema provides. When a migration adds a
column, the mapper gains a field and the migration supplies a default for
existing rows -- so old rows read correctly without the mapper needing to know
which migration wrote them. Mappers therefore contain no version branching, and
if one ever needs it, that is the signal that the migration should have
backfilled instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from sqlalchemy import Row


class PersistenceRow(Protocol):
    """The read side of a row: anything with a column mapping."""

    @property
    def _mapping(self) -> Any:  # pragma: no cover - structural only
        ...


class EntityMapper[T](ABC):
    """Converts between one domain entity and its persisted representation.

    Subclasses implement two methods and inherit the contract. Keeping the pair
    in one class -- rather than as two free functions -- is what makes the
    round-trip property expressible and testable.
    """

    @abstractmethod
    def to_domain(self, row: Row[Any]) -> T:
        """Build a domain entity from a database row.

        Must be pure: no clock, no query, no mutation.
        """

    @abstractmethod
    def to_params(self, entity: T) -> dict[str, Any]:
        """Build column values from a domain entity.

        Omits database-assigned columns on insert. Every other column the
        mapper claims must be present, because a missing key silently writes a
        default rather than the value the caller intended.
        """

    def to_params_many(self, entities: list[T]) -> list[dict[str, Any]]:
        """Build column values for a batch, for bulk insert.

        Batching matters for history backfill, where per-row round trips would
        dominate the cost.
        """
        return [self.to_params(entity) for entity in entities]

    def to_domain_many(self, rows: list[Row[Any]]) -> list[T]:
        """Build domain entities from a batch of rows."""
        return [self.to_domain(row) for row in rows]


def column_names(params: dict[str, Any]) -> frozenset[str]:
    """Return the columns a parameter dictionary writes.

    Used by mapper tests to assert that a mapper covers every column its table
    declares, which is the check that catches a forgotten field at the moment
    the schema changes rather than at the moment a user notices data loss.
    """
    return frozenset(params)
