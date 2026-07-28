"""Typed, validated, immutable application configuration.

Configuration is deployment-scoped and immutable at runtime (ADR-028). User
preferences live in the database as settings, and secret values live in the
credential store; neither belongs here.

Only the sections whose subsystems exist are modelled. Adding a section before
its subsystem would be a placeholder, and unknown keys are rejected, so
``config/default.yaml`` must stay in step with these models. Sections arrive
with their milestones: ``database`` and ``telegram`` in Milestones 1 and 2,
``ai``, ``sync`` and ``embeddings`` later.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from tgassist.infrastructure.config.paths import AppPaths, default_data_dir

ENV_PREFIX = "TGASSIST_"
"""Prefix for environment variables that override configuration keys."""

ENV_NESTED_DELIMITER = "__"
"""Separator mapping ``TGASSIST_LOGGING__LEVEL`` to ``logging.level``."""


class Profile(StrEnum):
    """Environment profile.

    Profiles supply a layer of defaults between the shipped baseline and the
    user's local overrides, so that development ergonomics and production
    safety do not have to be reconciled in a single file.
    """

    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    """Logging verbosity."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class _Section(BaseModel):
    """Base for configuration sections: immutable and closed to unknown keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSection(_Section):
    """General application settings."""

    data_dir: Path | None = Field(
        default=None,
        description="Root for generated data. Defaults to the platform data directory.",
    )
    locale: str = Field(
        default="system",
        description="Interface language. 'system' follows the operating system.",
    )


class LoggingSection(_Section):
    """Logging configuration.

    See ``docs/SECURITY.md`` section 9 for what may and may not be logged.
    """

    level: LogLevel = LogLevel.INFO
    console_enabled: bool = True
    file_enabled: bool = True
    dir: Path | None = Field(
        default=None,
        description="Log directory. Defaults to <data_dir>/logs.",
    )
    format: Literal["json", "console"] = Field(
        default="console",
        description="Renderer for file output. Console output is always human-readable.",
    )
    max_file_mb: int = Field(default=50, ge=1, le=1024)
    backup_count: int = Field(default=5, ge=0, le=100)
    retention_days: int = Field(default=14, ge=1, le=365)
    diagnostic_mode: bool = Field(
        default=False,
        description=(
            "Log message content for troubleshooting. Requires explicit opt-in, "
            "displays a persistent indicator, and must never be enabled by default."
        ),
    )
    component_levels: dict[str, LogLevel] = Field(
        default_factory=dict,
        description="Per-logger level overrides, keyed by logger name.",
    )


class SecuritySection(_Section):
    """Security controls that apply from the first milestone."""

    enforce_file_permissions: bool = Field(
        default=True,
        description="Apply owner-only permissions to created directories.",
    )
    require_secret_store: bool = Field(
        default=True,
        description=(
            "Refuse to start when the operating system credential store is "
            "unavailable, rather than proceeding without session encryption. "
            "Enforced once the secret store is implemented."
        ),
    )


class AppConfig(BaseSettings):
    """The complete, resolved application configuration.

    Immutable after construction. Reloading builds a new instance and swaps it
    atomically, so no component can observe a half-applied change.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        extra="forbid",
        frozen=True,
        validate_default=True,
        nested_model_default_partial_update=True,
    )

    profile: Profile = Profile.DEVELOPMENT
    app: AppSection = Field(default_factory=AppSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    security: SecuritySection = Field(default_factory=SecuritySection)

    @field_validator("profile", mode="before")
    @classmethod
    def _normalise_profile(cls, value: Any) -> Any:
        """Accept profile names case-insensitively."""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @model_validator(mode="after")
    def _check_diagnostic_mode(self) -> AppConfig:
        """Reject diagnostic logging in the production profile.

        Diagnostic mode logs message content. Enabling it in production would
        write third-party conversation data to disk in plain text, which the
        privacy commitments do not permit as a configuration-file decision.
        """
        if self.profile is Profile.PRODUCTION and self.logging.diagnostic_mode:
            msg = (
                "logging.diagnostic_mode cannot be enabled in the production profile; "
                "it logs message content and must be turned on deliberately at runtime"
            )
            raise ValueError(msg)
        return self

    @property
    def paths(self) -> AppPaths:
        """Return the resolved directory layout."""
        return AppPaths.from_data_dir(self.app.data_dir or default_data_dir())

    @property
    def log_dir(self) -> Path:
        """Return the resolved log directory."""
        return self.logging.dir or self.paths.logs_dir

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order configuration sources, highest precedence first.

        The YAML layer source is injected by the loader, which needs the profile
        and config directory to know which files to read. When this class is
        constructed directly — in tests, for example — only defaults, explicit
        arguments and the environment apply.
        """
        del settings_cls, file_secret_settings
        return (init_settings, env_settings, dotenv_settings)
