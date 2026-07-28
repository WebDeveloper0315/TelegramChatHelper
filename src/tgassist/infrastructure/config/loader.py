"""Configuration loading, layering and validation.

Resolution order, lowest precedence first:

1. Built-in defaults declared on the models
2. ``config/default.yaml`` -- the committed, documented baseline
3. ``config/profiles/<profile>.yaml`` -- environment profile
4. ``config/local.yaml`` -- gitignored user overrides
5. Environment variables prefixed ``TGASSIST_``
6. Explicit overrides, such as command-line flags

Built-in defaults are complete, so the application starts with no configuration
files present. Unknown keys are an error rather than a silent ignore: a typo
should stop startup, not be discovered later as a setting that never took
effect.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_settings.sources import EnvSettingsSource

from tgassist.domain.errors import (
    ConfigurationError,
    InvalidConfigurationValueError,
    MissingRequiredSettingError,
    UnknownConfigurationKeyError,
)
from tgassist.infrastructure.config.paths import default_config_dir
from tgassist.infrastructure.config.settings import (
    ENV_NESTED_DELIMITER,
    ENV_PREFIX,
    AppConfig,
    Profile,
)

PROFILE_ENV_VAR = f"{ENV_PREFIX}PROFILE"
CONFIG_DIR_ENV_VAR = f"{ENV_PREFIX}CONFIG_DIR"

DEFAULT_FILE = "default.yaml"
LOCAL_FILE = "local.yaml"
PROFILES_DIR = "profiles"


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """A resolved configuration together with the provenance of each value.

    Attributes:
        config: The validated, immutable configuration.
        origins: Dotted key to the source that supplied the winning value.
        layers: Configuration files that were read, in application order.
        missing_layers: Files that were looked for and not found. Absent files
            are normal, not an error, because defaults are complete.
    """

    config: AppConfig
    origins: dict[str, str] = field(default_factory=dict)
    layers: tuple[Path, ...] = ()
    missing_layers: tuple[Path, ...] = ()


def resolve_profile(explicit: Profile | str | None = None) -> Profile:
    """Determine the active profile from an explicit value or the environment."""
    raw = explicit if explicit is not None else os.environ.get(PROFILE_ENV_VAR)
    if raw is None:
        return Profile.DEVELOPMENT
    if isinstance(raw, Profile):
        return raw
    try:
        return Profile(str(raw).strip().lower())
    except ValueError as exc:
        valid = ", ".join(p.value for p in Profile)
        msg = f"Unknown profile {raw!r}. Valid profiles: {valid}."
        raise InvalidConfigurationValueError(
            msg,
            user_message=f"The configuration profile {raw!r} is not recognised.",
            context={"profile": str(raw)},
            cause=exc,
        ) from exc


def resolve_config_dir(explicit: Path | None = None) -> Path:
    """Determine the configuration directory."""
    if explicit is not None:
        return explicit
    from_env = os.environ.get(CONFIG_DIR_ENV_VAR)
    if from_env:
        return Path(from_env)
    return default_config_dir()


def load_config(
    *,
    profile: Profile | str | None = None,
    config_dir: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> LoadedConfig:
    """Load, layer and validate the application configuration.

    Args:
        profile: Profile to apply. Defaults to ``TGASSIST_PROFILE``, then
            ``development``.
        config_dir: Directory holding the configuration files. Defaults to
            ``TGASSIST_CONFIG_DIR``, then the repository ``config`` directory.
        overrides: Highest-precedence values, such as command-line flags.

    Returns:
        The resolved configuration with the provenance of each value.

    Raises:
        ConfigurationError: If any source is malformed, or if the merged result
            fails validation.
    """
    active_profile = resolve_profile(profile)
    directory = resolve_config_dir(config_dir)

    merged: dict[str, Any] = {"profile": active_profile.value}
    origins: dict[str, str] = {"profile": _profile_origin(profile)}
    loaded: list[Path] = []
    missing: list[Path] = []

    candidates = (
        directory / DEFAULT_FILE,
        directory / PROFILES_DIR / f"{active_profile.value}.yaml",
        directory / LOCAL_FILE,
    )
    for candidate in candidates:
        if not candidate.is_file():
            missing.append(candidate)
            continue
        loaded.append(candidate)
        _deep_merge(merged, _read_yaml(candidate), origins, str(candidate), prefix="")

    env_values = _read_environment()
    _deep_merge(merged, env_values, origins, "environment", prefix="")

    if overrides:
        _deep_merge(merged, overrides, origins, "override", prefix="")

    # The profile chosen above wins: a profile named in a configuration file
    # cannot silently change which profile file was already applied.
    merged["profile"] = active_profile.value

    config = _validate(merged)
    return LoadedConfig(
        config=config,
        origins=origins,
        layers=tuple(loaded),
        missing_layers=tuple(missing),
    )


def _profile_origin(explicit: Profile | str | None) -> str:
    if explicit is not None:
        return "override"
    if os.environ.get(PROFILE_ENV_VAR):
        return "environment"
    return "default"


def _read_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML configuration file into a mapping."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"Configuration file {path} is not valid YAML: {exc}"
        raise InvalidConfigurationValueError(
            msg,
            user_message=f"The configuration file {path.name} could not be read.",
            context={"path": str(path)},
            cause=exc,
        ) from exc
    except OSError as exc:
        msg = f"Configuration file {path} could not be read: {exc}"
        raise ConfigurationError(
            msg,
            user_message=f"The configuration file {path.name} could not be opened.",
            context={"path": str(path)},
            cause=exc,
        ) from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        msg = f"Configuration file {path} must contain a mapping at the top level."
        raise InvalidConfigurationValueError(
            msg,
            user_message=f"The configuration file {path.name} has the wrong shape.",
            context={"path": str(path)},
        )
    return raw


def _read_environment() -> dict[str, Any]:
    """Collect configuration values supplied through environment variables."""
    source = EnvSettingsSource(
        AppConfig,
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        case_sensitive=False,
    )
    try:
        return dict(source())
    except ValidationError as exc:
        raise _translate(exc) from exc


def _deep_merge(
    base: dict[str, Any],
    overlay: dict[str, Any],
    origins: dict[str, str],
    source: str,
    *,
    prefix: str,
) -> None:
    """Merge ``overlay`` into ``base``, recording the origin of each leaf value.

    Nested mappings are always merged key by key, even when the section is new
    to ``base``. Assigning a whole section wholesale would attribute its origin
    to the section rather than to each value, so a later layer overriding one
    key would leave the others credited to the wrong file.
    """
    for key, value in overlay.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            existing = base.get(key)
            if not isinstance(existing, dict):
                existing = {}
                base[key] = existing
            _deep_merge(existing, value, origins, source, prefix=f"{dotted}.")
        else:
            base[key] = value
            origins[dotted] = source


def _validate(values: dict[str, Any]) -> AppConfig:
    """Validate merged values, translating pydantic errors into domain errors."""
    try:
        return AppConfig(**values)
    except ValidationError as exc:
        raise _translate(exc) from exc


def _translate(exc: ValidationError) -> ConfigurationError:
    """Convert a pydantic validation failure into the domain error taxonomy."""
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first["loc"])
    kind = first["type"]
    detail = first["msg"]

    if kind == "extra_forbidden":
        msg = f"Unknown configuration key {location!r}."
        return UnknownConfigurationKeyError(
            msg,
            user_message=(
                f"The configuration contains an unrecognised setting: {location}. "
                "Check for a typo, or see docs/CONFIGURATION.md for valid keys."
            ),
            context={"key": location, "error_count": exc.error_count()},
            cause=exc,
        )
    if kind == "missing":
        msg = f"Required configuration key {location!r} was not supplied."
        return MissingRequiredSettingError(
            msg,
            user_message=f"A required setting is missing: {location}.",
            context={"key": location, "error_count": exc.error_count()},
            cause=exc,
        )

    msg = f"Invalid value for configuration key {location!r}: {detail}"
    return InvalidConfigurationValueError(
        msg,
        user_message=f"The setting {location} has an invalid value: {detail}",
        context={"key": location, "error_count": exc.error_count()},
        cause=exc,
    )
