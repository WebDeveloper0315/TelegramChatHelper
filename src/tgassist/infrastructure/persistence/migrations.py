"""Alembic-backed migration runner.

Drives Alembic programmatically against the application's own connection, so
migrations run on the database thread with the application's pragmas already in
force (ADR-013, ADR-015).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import Script, ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from tgassist.domain.errors import MigrationFailedError, SchemaVersionError
from tgassist.domain.ports.migration_runner import (
    MigrationInfo,
    MigrationReport,
    MigrationRunner,
    PreUpgradeHook,
    SchemaState,
    SchemaStatus,
)
from tgassist.infrastructure.config.paths import project_root
from tgassist.infrastructure.logging import get_logger
from tgassist.infrastructure.persistence.engine import (
    SqliteDatabase,
    release_autobegun_transaction,
)

ALEMBIC_INI: Final = "alembic.ini"
MIGRATIONS_DIR: Final = "migrations"


def build_alembic_config(*, root: Path | None = None) -> AlembicConfig:
    """Build an Alembic configuration rooted at the repository."""
    base = root if root is not None else project_root()
    config = AlembicConfig(str(base / ALEMBIC_INI))
    config.set_main_option("script_location", str(base / MIGRATIONS_DIR))
    return config


class AlembicMigrationRunner(MigrationRunner):
    """Applies and reports on schema migrations."""

    __slots__ = ("_config", "_database", "_log", "_pre_upgrade_hook", "_scripts")

    def __init__(self, database: SqliteDatabase, *, root: Path | None = None) -> None:
        """Create the runner.

        Args:
            database: The database to migrate.
            root: Repository root holding ``alembic.ini`` and ``migrations``.
        """
        self._database = database
        self._config = build_alembic_config(root=root)
        self._scripts = ScriptDirectory.from_config(self._config)
        self._pre_upgrade_hook: PreUpgradeHook | None = None
        self._log = get_logger(__name__)

    # -- Inspection -------------------------------------------------------

    def head_revision(self) -> str:
        """Return the revision this application expects."""
        head = self._scripts.get_current_head()
        if head is None:  # pragma: no cover - only with an empty versions directory
            msg = "No migrations are defined"
            raise MigrationFailedError(
                msg, user_message="The application has no database schema defined."
            )
        return head

    async def current_revision(self) -> str | None:
        """Return the applied revision, or ``None`` if the schema is empty."""
        return await self._database.executor.run(self._current_revision_sync)

    def _current_revision_sync(self) -> str | None:
        connection = self._database.connection
        revision = MigrationContext.configure(connection).get_current_revision()
        release_autobegun_transaction(connection)
        return revision

    async def status(self) -> SchemaStatus:
        """Report the database's schema position without changing it."""
        return await self._database.executor.run(self._status_sync)

    def _status_sync(self) -> SchemaStatus:
        head = self.head_revision()
        current = self._current_revision_sync()

        if current is None:
            return SchemaStatus(
                state=SchemaState.EMPTY,
                current_revision=None,
                head_revision=head,
                pending=tuple(self._all_revisions()),
            )
        if current == head:
            return SchemaStatus(
                state=SchemaState.CURRENT, current_revision=current, head_revision=head
            )

        known = {script.revision for script in self._scripts.walk_revisions()}
        if current not in known:
            # A revision this application has never heard of. Almost always a
            # database from a newer release or another branch; either way,
            # guessing what it contains is not an option.
            return SchemaStatus(
                state=SchemaState.UNKNOWN, current_revision=current, head_revision=head
            )

        pending = self._revisions_between(current, head)
        if pending:
            return SchemaStatus(
                state=SchemaState.BEHIND,
                current_revision=current,
                head_revision=head,
                pending=tuple(pending),
            )
        # Known revision, not head, nothing pending between them: the database
        # is ahead of this application on the same line of history.
        return SchemaStatus(state=SchemaState.AHEAD, current_revision=current, head_revision=head)

    def _all_revisions(self) -> list[MigrationInfo]:
        return [self._describe(script) for script in reversed(list(self._scripts.walk_revisions()))]

    def _revisions_between(self, current: str, head: str) -> list[MigrationInfo]:
        try:
            scripts = list(self._scripts.iterate_revisions(head, current))
        except Exception:
            return []
        return [
            self._describe(script) for script in reversed(scripts) if script.revision != current
        ]

    @staticmethod
    def _describe(script: Script) -> MigrationInfo:
        down = script.down_revision
        return MigrationInfo(
            revision=script.revision,
            down_revision=str(down) if down else None,
            description=script.doc or "",
        )

    # -- Mutation ---------------------------------------------------------

    def set_pre_upgrade_hook(self, hook: PreUpgradeHook | None) -> None:
        """Register a callable to run before any upgrade, typically a backup."""
        self._pre_upgrade_hook = hook

    async def upgrade(self, target: str = "head", *, backup_first: bool = True) -> MigrationReport:
        """Apply pending migrations up to ``target``.

        Raises:
            SchemaVersionError: If the database is ahead of this application.
            MigrationFailedError: If a migration fails. The transaction rolls
                back, leaving the database at its previous revision.
        """
        status = await self.status()
        if status.state is SchemaState.AHEAD or status.state is SchemaState.UNKNOWN:
            msg = (
                f"The database is at revision {status.current_revision!r}, which this "
                f"application (expecting {status.head_revision!r}) does not understand"
            )
            raise SchemaVersionError(
                msg,
                user_message=(
                    "This database was created by a newer version of the application. "
                    "Please update to open it."
                ),
                context={
                    "current": status.current_revision,
                    "expected": status.head_revision,
                    "state": status.state.value,
                },
            )

        backup_taken = False
        if backup_first and self._pre_upgrade_hook is not None:
            backup_taken = await self._pre_upgrade_hook()
            if not backup_taken:
                msg = "The pre-upgrade backup did not complete; the migration was not attempted"
                raise MigrationFailedError(
                    msg,
                    user_message="The update was cancelled because a backup could not be made.",
                    context={"target": target},
                )
        elif backup_first:
            # Honest rather than reassuring: the backup subsystem arrives in
            # Milestone 11, and claiming a safety net that does not exist would
            # be worse than saying so.
            self._log.warning(
                "migration_without_backup",
                target=target,
                detail="No backup provider is registered; the migration proceeds unprotected.",
            )

        return await self._run_migration(target, upgrade=True, backup_taken=backup_taken)

    async def downgrade(self, target: str) -> MigrationReport:
        """Revert migrations down to ``target``."""
        return await self._run_migration(target, upgrade=False, backup_taken=False)

    async def _run_migration(
        self, target: str, *, upgrade: bool, backup_taken: bool
    ) -> MigrationReport:
        before = await self.current_revision()
        started = time.monotonic()
        try:
            await self._database.executor.run(self._migrate_sync, target, upgrade)
        except SchemaVersionError:
            raise
        except Exception as exc:
            direction = "upgrade" if upgrade else "downgrade"
            msg = f"Migration {direction} to {target!r} failed: {exc}"
            raise MigrationFailedError(
                msg,
                user_message=(
                    "The database update could not be applied. Your data has been left unchanged."
                ),
                context={"target": target, "from": before, "direction": direction},
                cause=exc,
            ) from exc

        after = await self.current_revision()
        report = MigrationReport(
            from_revision=before,
            to_revision=after,
            applied=(target,) if before != after else (),
            backup_taken=backup_taken,
            duration_seconds=round(time.monotonic() - started, 3),
        )
        if report.changed:
            self._log.info(
                "migration_applied",
                direction="upgrade" if upgrade else "downgrade",
                from_revision=before,
                to_revision=after,
                duration_seconds=report.duration_seconds,
                backup_taken=backup_taken,
            )
        return report

    def _migrate_sync(self, target: str, upgrade: bool) -> None:
        connection: Connection = self._database.connection
        self._config.attributes["connection"] = connection
        try:
            if upgrade:
                alembic_command.upgrade(self._config, target)
            else:
                alembic_command.downgrade(self._config, target)
        except SQLAlchemyError:
            raise
        finally:
            self._config.attributes.pop("connection", None)
            release_autobegun_transaction(connection)
