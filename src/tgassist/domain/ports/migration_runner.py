"""Migration runner port: schema version management.

The schema is versioned and every change is a reversible, reviewed migration.
Hand-editing a user's database is prohibited, because the only record of what
happened would be in whoever typed it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class SchemaState(StrEnum):
    """How the database schema relates to what the application expects."""

    EMPTY = "empty"
    """No schema has been applied. A fresh installation."""

    CURRENT = "current"
    """The database matches the application. Proceed."""

    BEHIND = "behind"
    """Migrations are pending. Offer to apply them, after a backup."""

    AHEAD = "ahead"
    """The database was written by a newer application.

    Refuse to start. Downgrading user data is never attempted, because a
    migration that removes a column cannot restore what it discarded.
    """

    UNKNOWN = "unknown"
    """The recorded revision is not one this application knows.

    Usually a database from a different branch or a failed partial upgrade.
    Treated as unsafe.
    """


@dataclass(frozen=True, slots=True)
class MigrationInfo:
    """One migration in the sequence."""

    revision: str
    down_revision: str | None
    description: str


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """The outcome of an upgrade or downgrade."""

    from_revision: str | None
    to_revision: str | None
    applied: Sequence[str] = field(default_factory=tuple)
    backup_taken: bool = False
    duration_seconds: float = 0.0

    @property
    def changed(self) -> bool:
        """Report whether the schema actually moved."""
        return self.from_revision != self.to_revision


@dataclass(frozen=True, slots=True)
class SchemaStatus:
    """A complete picture of the database's schema position."""

    state: SchemaState
    current_revision: str | None
    head_revision: str
    pending: Sequence[MigrationInfo] = field(default_factory=tuple)

    @property
    def can_start(self) -> bool:
        """Report whether the application may run against this database."""
        return self.state in (SchemaState.CURRENT, SchemaState.EMPTY, SchemaState.BEHIND)


PreUpgradeHook = Callable[[], Awaitable[bool]]
"""Runs before a migration; returns whether a backup was taken.

The backup subsystem arrives in Milestone 11. Until then no hook is registered
and the report records that no backup was taken, which is honest -- rather than
claiming a safety net that does not exist.
"""


@runtime_checkable
class MigrationRunner(Protocol):
    """Applies and reports on schema migrations.

    Contract:

    1. :meth:`status` never modifies the database.
    2. :meth:`upgrade` runs the pre-upgrade hook first, if one is registered,
       and refuses to proceed if the hook fails.
    3. A failed migration leaves the database at its previous revision.
    4. :meth:`downgrade` is supported so that a bad release can be backed out,
       and so that migrations are tested in both directions.
    5. Every operation is reported, not merely performed: a caller needs to know
       what changed to write it to the audit log.
    """

    async def status(self) -> SchemaStatus:
        """Report the database's schema position without changing it."""
        ...

    async def current_revision(self) -> str | None:
        """Return the applied revision, or ``None`` if the schema is empty."""
        ...

    def head_revision(self) -> str:
        """Return the revision this application expects."""
        ...

    async def upgrade(self, target: str = "head", *, backup_first: bool = True) -> MigrationReport:
        """Apply pending migrations up to ``target``."""
        ...

    async def downgrade(self, target: str) -> MigrationReport:
        """Revert migrations down to ``target``."""
        ...

    def set_pre_upgrade_hook(self, hook: PreUpgradeHook | None) -> None:
        """Register a callable to run before any upgrade, typically a backup."""
        ...
