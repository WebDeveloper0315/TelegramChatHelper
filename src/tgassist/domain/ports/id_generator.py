"""Identifier generation port.

Identifiers come from here rather than from ``uuid4()`` scattered through the
code, for two reasons: generation can be made deterministic in tests, and the
ordering strategy is decided in one place where its consequences for database
index locality are visible.

Three kinds of identifier, because they have different consumers and different
constraints:

* :meth:`IdGenerator.new_id` -- a 64-bit integer, for entities that need an
  identity before the database assigns one.
* :meth:`IdGenerator.new_uuid` -- a canonical UUID string, for identifiers that
  cross a process boundary or appear in exported data.
* :meth:`IdGenerator.new_correlation_id` -- ties the log records of one logical
  operation together.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IdGenerator(Protocol):
    """Produces identifiers.

    Contract, guaranteed by every implementation and verified by the shared
    contract test suite:

    1. Every identifier is unique within the lifetime of the generator.
    2. Identifiers are **time-ordered**: an identifier generated later never
       sorts before one generated earlier. This is what makes them useful as
       database keys, because insertions append to the end of an index rather
       than scattering through it.
    3. :meth:`new_id` returns a positive integer that fits in a signed 64-bit
       column.
    4. :meth:`new_uuid` returns a canonical lowercase UUID string with hyphens.
    5. Generation never blocks and never raises.
    6. Implementations are safe to call from more than one thread.

    The contract does not promise unpredictability. These identifiers order by
    creation time by design, so they must never be used where guessing one
    would be a security problem; that is what the secret store is for.
    """

    def new_id(self) -> int:
        """Return a positive, time-ordered 64-bit integer identifier."""
        ...

    def new_uuid(self) -> str:
        """Return a canonical, time-ordered UUID string."""
        ...

    def new_correlation_id(self) -> str:
        """Return an identifier tying together the log records of one operation."""
        ...
