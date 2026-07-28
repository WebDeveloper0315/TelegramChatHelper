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

from tgassist.domain.errors import SecretStoreUnavailableError
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus
from tgassist.domain.ports.id_generator import IdGenerator
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

    __slots__ = ("_clock", "_closed", "_events", "_ids", "_loaded", "_secrets")

    def __init__(
        self,
        loaded: LoadedConfig,
        *,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        events: EventBus | None = None,
        secrets: SecretStore | None = None,
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
        """
        self._loaded = loaded
        self._clock = clock if clock is not None else SystemClock()
        self._ids = ids if ids is not None else UuidV7IdGenerator(self._clock)
        self._events = events if events is not None else InProcessEventBus()
        self._secrets = secrets if secrets is not None else build_default_secret_store()
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

    def close(self) -> None:
        """Release held resources.

        Nothing holds an external resource at this milestone. The method exists
        so that the lifecycle contract is established before the first adapter
        that needs it, rather than being retrofitted across every call site.
        """
        self._closed = True

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
