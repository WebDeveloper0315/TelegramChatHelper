"""Composition root.

This is the only module permitted to construct infrastructure adapters. Every
other module receives its collaborators through its constructor, which is what
allows the domain and the use cases to be exercised against fakes.

Wiring is written by hand rather than delegated to a framework. The object graph
is fixed and single-process, so a container library would add a declaration
language and a layer of indirection without removing any real work. If runtime
service registration becomes necessary for plugins, that trade-off is revisited
in Milestone 12.

At Milestone 0 the container supplies configuration and logging. Adapters are
added by the milestone that introduces them.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Self

from tgassist.infrastructure.config import (
    AppConfig,
    LoadedConfig,
    Profile,
    is_owner_only,
    load_config,
)
from tgassist.infrastructure.logging import configure_logging, get_logger


class Container:
    """Holds the application's infrastructure and hands it to callers.

    Construct with :meth:`create`, which performs the startup sequence in the
    order the specification requires: configuration is resolved and validated
    before anything else, and logging is configured before any component that
    might need to report a problem.
    """

    def __init__(self, loaded: LoadedConfig) -> None:
        """Store an already-resolved configuration.

        Prefer :meth:`create`. This constructor exists so that tests can inject
        a configuration without touching the filesystem.
        """
        self._loaded = loaded
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

    # -- Lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Release held resources.

        Nothing holds a resource at Milestone 0. The method exists so that the
        lifecycle contract is established before the first adapter that needs
        it, rather than being retrofitted across every call site later.
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
