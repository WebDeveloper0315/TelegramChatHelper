"""Configuration loading, layering, validation and path resolution.

Public surface of the configuration subsystem. See ``docs/CONFIGURATION.md``.
"""

from tgassist.infrastructure.config.loader import (
    LoadedConfig,
    load_config,
    resolve_config_dir,
    resolve_profile,
)
from tgassist.infrastructure.config.paths import AppPaths, default_data_dir, is_owner_only
from tgassist.infrastructure.config.settings import (
    AppConfig,
    AppSection,
    DatabaseSection,
    LoggingSection,
    LogLevel,
    Profile,
    SecuritySection,
)

__all__ = [
    "AppConfig",
    "AppPaths",
    "AppSection",
    "DatabaseSection",
    "LoadedConfig",
    "LogLevel",
    "LoggingSection",
    "Profile",
    "SecuritySection",
    "default_data_dir",
    "is_owner_only",
    "load_config",
    "resolve_config_dir",
    "resolve_profile",
]
