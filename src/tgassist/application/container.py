"""Composition root.

This is the only module permitted to construct infrastructure adapters. Every
other component receives its collaborators through its constructor, which is
what allows the domain and the use cases to be exercised against fakes.

Wiring is written by hand rather than delegated to a framework. The object graph
is fixed and single-process, so a container library would add a declaration
language and a layer of indirection without removing any real work. If runtime
service registration becomes necessary for plugins, that trade-off is revisited
in Milestone 12.

**This container is not a service locator.** Components never receive the
container and reach into it for what they need; the container constructs them
and passes their dependencies as constructor arguments. The distinction matters
because a locator hides a component's real dependencies from its signature,
which is exactly the information a reader and a test need most.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Self

from tgassist.domain.errors import SchemaVersionError, SecretStoreUnavailableError
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.database import HealthReport
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.migration_runner import SchemaState, SchemaStatus
from tgassist.domain.ports.secret_store import SecretStore
from tgassist.infrastructure.clock import SystemClock
from tgassist.infrastructure.config import (
    AppConfig,
    LoadedConfig,
    Profile,
    is_owner_only,
    load_config,
)
from tgassist.infrastructure.events import InProcessEventBus
from tgassist.infrastructure.ids import UuidV7IdGenerator
from tgassist.infrastructure.logging import configure_logging, get_logger
from tgassist.infrastructure.persistence import (
    AlembicMigrationRunner,
    SqliteDatabase,
    UnitOfWorkFactory,
)
from tgassist.infrastructure.security import build_default_secret_store


class Container:
    """Holds the application's infrastructure and hands it to callers.

    Construct with :meth:`create`, which performs the startup sequence in the
    order the specification requires: configuration is resolved and validated
    before anything else, and logging is configured before any component that
    might need to report a problem.

    Every port can be overridden through the constructor, so a test builds a
    container with fakes rather than patching module globals.
    """

    __slots__ = (
        "_clock",
        "_closed",
        "_database",
        "_events",
        "_ids",
        "_loaded",
        "_migrations",
        "_secrets",
        "_uow_factory",
    )

    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        events: EventBus | None = None,
        secrets: SecretStore | None = None,
        database: SqliteDatabase | None = None,
    ) -> None:
        """Store a resolved configuration and build or accept the core ports.

        Prefer :meth:`create` for real use. This constructor exists so that a
        test can inject doubles without touching the filesystem or the operating
        system credential store.

        Args:
            loaded: Resolved configuration with the provenance of each value.
            clock: Time source. Defaults to the system clock.
            ids: Identifier generator. Defaults to UUID version 7, driven by
                whichever clock is in use, so a fixed clock also fixes the
                identifiers.
            events: Event bus. Defaults to synchronous in-process delivery.
            secrets: Secret store. Defaults to environment over credential store.
            database: Database. Defaults to SQLite at the configured path. Tests
                inject an in-memory database this way.
        """
        self._loaded = loaded
        self._clock = clock if clock is not None else SystemClock()
        self._ids = ids if ids is not None else UuidV7IdGenerator(self._clock)
        self._events = events if events is not None else InProcessEventBus()
        self._secrets = secrets if secrets is not None else build_default_secret_store()
        self._database = (
            database
            if database is not None
            else SqliteDatabase(
                loaded.config.database.model_copy(update={"path": loaded.config.database_path})
            )
        )
        self._uow_factory = UnitOfWorkFactory(self._database)
        self._migrations = AlembicMigrationRunner(self._database)
        self._closed = False

    @classmethod
    def create(
        cls,
        *,
        profile: Profile | str | None = None,
        config_dir: Path | None = None,
        overrides: dict[str, Any] | None = None,
        configure_logging_on_start: bool = True,
    ) -> Self:
        """Resolve configuration, configure logging and build the container.

        Args:
            profile: Environment profile. Defaults to ``TGASSIST_PROFILE``.
            config_dir: Configuration directory. Defaults to ``TGASSIST_CONFIG_DIR``.
            overrides: Highest-precedence values, such as command-line flags.
            configure_logging_on_start: Configure global logging. Disabled by
                tests that must not mutate process-wide logging state.

        Raises:
            ConfigurationError: If configuration is missing, malformed or invalid.
        """
        loaded = load_config(profile=profile, config_dir=config_dir, overrides=overrides)
        container = cls(loaded)

        if configure_logging_on_start:
            configure_logging(loaded.config)
            get_logger(__name__).info(
                "application_configured",
                profile=loaded.config.profile.value,
                config_layers=[str(path) for path in loaded.layers],
                data_dir=str(loaded.config.paths.data_dir),
            )
        return container

    # -- Configuration ----------------------------------------------------

    @property
    def config(self) -> AppConfig:
        """Return the immutable application configuration."""
        return self._loaded.config

    @property
    def loaded_config(self) -> LoadedConfig:
        """Return the configuration together with the provenance of each value."""
        return self._loaded

    # -- Core ports -------------------------------------------------------

    @property
    def clock(self) -> Clock:
        """Return the application's time source."""
        return self._clock

    @property
    def ids(self) -> IdGenerator:
        """Return the application's identifier generator."""
        return self._ids

    @property
    def events(self) -> EventBus:
        """Return the application's event bus."""
        return self._events

    @property
    def secrets(self) -> SecretStore:
        """Return the application's secret store."""
        return self._secrets

    @property
    def database(self) -> SqliteDatabase:
        """Return the database."""
        return self._database

    @property
    def unit_of_work(self) -> UnitOfWorkFactory:
        """Return the unit of work factory.

        A factory rather than a unit of work: a use case decides when its
        transaction begins, and an injected open transaction would outlive the
        operation it was meant to bound.
        """
        return self._uow_factory

    @property
    def migrations(self) -> AlembicMigrationRunner:
        """Return the migration runner."""
        return self._migrations

    # -- Database startup -------------------------------------------------

    async def start_database(self, *, migrate: bool | None = None) -> SchemaStatus:
        """Open the database and bring its schema to the expected revision.

        Args:
            migrate: Apply pending migrations. Defaults to the configured
                ``database.auto_migrate``.

        Returns:
            The schema position after any migration.

        Raises:
            DatabaseUnavailableError: If the database cannot be opened.
            SchemaVersionError: If the database was written by a newer version.
                Refusing to start is the only safe response, because a migration
                that removed a column cannot restore what it discarded.
            MigrationFailedError: If a migration fails, leaving the database at
                its previous revision.
        """
        await self._database.connect()
        status = await self._migrations.status()

        if status.state in (SchemaState.AHEAD, SchemaState.UNKNOWN):
            msg = (
                f"The database is at revision {status.current_revision!r}; this "
                f"application expects {status.head_revision!r}"
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

        should_migrate = self.config.database.auto_migrate if migrate is None else migrate
        if should_migrate and status.state in (SchemaState.EMPTY, SchemaState.BEHIND):
            await self._migrations.upgrade()
            status = await self._migrations.status()

        return status

    async def database_health(self) -> HealthReport:
        """Check the database and report what was found. Never raises."""
        return await self._database.health()

    # -- Logging ----------------------------------------------------------

    def logger(self, name: str | None = None) -> Any:
        """Return a bound logger for the given name."""
        return get_logger(name)

    # -- Filesystem -------------------------------------------------------

    def ensure_directories(self) -> None:
        """Create the application's directory layout."""
        self.config.paths.ensure(restrict_permissions=self.config.security.enforce_file_permissions)

    def permission_report(self) -> dict[str, bool | None]:
        """Report whether each existing directory is restricted to its owner.

        ``None`` means "not verified on this platform", never "safe".
        Presentation reaches filesystem detail through here rather than
        importing infrastructure directly.
        """
        return {
            directory.name: is_owner_only(directory)
            for directory in self.config.paths.all_directories()
            if directory.exists()
        }

    # -- Security ---------------------------------------------------------

    async def verify_secret_store(self) -> bool:
        """Check the credential backend, enforcing the configured requirement.

        Returns:
            Whether a usable backend is present.

        Raises:
            SecretStoreUnavailableError: If ``security.require_secret_store`` is
                set and no backend is available. Failing closed is deliberate:
                continuing would mean storing a Telegram session -- the highest
                value asset in the system -- without encryption.
        """
        available = await self._secrets.is_available()
        if not available and self.config.security.require_secret_store:
            msg = "No operating system credential store is available"
            raise SecretStoreUnavailableError(
                msg,
                user_message=(
                    "The system credential store is unavailable, so credentials "
                    "cannot be protected."
                ),
                context={"profile": self.config.profile.value},
            )
        return available

    # -- Lifecycle --------------------------------------------------------

    async def aclose(self) -> None:
        """Release held resources, including the database and its worker thread."""
        if self._closed:
            return
        self._closed = True
        await self._database.close()

    def close(self) -> None:
        """Release resources that can be released without an event loop.

        The database owns a worker thread and is closed properly by
        :meth:`aclose`. This synchronous form exists for callers that never
        opened it, and still shuts the executor down so no thread is leaked.
        """
        if self._closed:
            return
        self._closed = True
        self._database.executor.close(wait=False)

    @property
    def is_closed(self) -> bool:
        """Report whether :meth:`close` has been called."""
        return self._closed

    def __enter__(self) -> Self:
        """Enter the container's lifetime."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the container's lifetime, releasing resources."""
        self.close()

    async def __aenter__(self) -> Self:
        """Enter the container's lifetime."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Leave the container's lifetime, closing the database."""
        await self.aclose()
