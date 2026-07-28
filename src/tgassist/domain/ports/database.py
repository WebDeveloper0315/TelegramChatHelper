"""Database port: connection lifecycle and health.

The domain declares what it needs to know about storage -- that it can be
opened, that it is healthy, that it can be closed -- without knowing that it is
SQLite, or a file, or reached over a thread boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PragmaState:
    """The connection settings actually in force.

    Read back from the database rather than assumed. A pragma that silently
    failed to apply -- ``journal_mode=WAL`` on a filesystem that does not
    support shared memory, for instance -- would otherwise look identical to one
    that worked, right up until concurrent access started corrupting data.
    """

    journal_mode: str
    foreign_keys: bool
    busy_timeout_ms: int
    synchronous: str

    @property
    def is_wal(self) -> bool:
        """Report whether write-ahead logging is active."""
        return self.journal_mode.lower() == "wal"


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The result of a database health check."""

    reachable: bool
    integrity_ok: bool
    foreign_keys_ok: bool
    pragmas: PragmaState | None = None
    schema_revision: str | None = None
    page_count: int = 0
    page_size_bytes: int = 0
    freelist_pages: int = 0
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        """Report whether the database is fit to use."""
        return self.reachable and self.integrity_ok and self.foreign_keys_ok and not self.problems

    @property
    def size_bytes(self) -> int:
        """Return the database file size implied by the page count."""
        return self.page_count * self.page_size_bytes


@runtime_checkable
class Database(Protocol):
    """Owns the connection lifecycle.

    Contract:

    1. :meth:`connect` is idempotent; connecting an already-connected database
       is not an error.
    2. Connection settings are **verified after connecting**, not assumed.
    3. :meth:`health` never raises. It reports problems, because it is the call
       a caller makes precisely to find out whether something is wrong.
    4. :meth:`close` is idempotent and releases every resource, including any
       worker thread.
    """

    async def connect(self) -> None:
        """Open the database and apply connection settings."""
        ...

    async def close(self) -> None:
        """Release the connection and any owned threads. Idempotent."""
        ...

    @property
    def is_connected(self) -> bool:
        """Report whether the database is currently open."""
        ...

    async def health(self) -> HealthReport:
        """Check the database and report what was found. Never raises."""
        ...
