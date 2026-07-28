"""Domain error taxonomy.

Errors are typed, never strings: behaviour depends on the type, not on message
text. Every error distinguishes a developer-facing ``message`` from a
user-facing ``user_message``, and declares whether retrying could help.

Only the branches with a live consumer are defined. The remaining families
(``PersistenceError``, ``TelegramError``, ``AIProviderError``, ``PluginError``,
``SecurityError``, ``OperationError``) arrive with the milestone that raises
them. The full taxonomy is specified in ``docs/ERROR_HANDLING.md``.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error raised by this application.

    Attributes:
        code: Stable identifier such as ``CONFIG_INVALID_VALUE``. Tests, logs
            and support conversations refer to this; unlike ``message`` it must
            not change between releases.
        message: Developer-facing description. May be detailed.
        user_message: User-facing description. Simple, actionable, and free of
            internal detail, file paths and sensitive values.
        context: Identifiers and metadata for diagnosis. Never message content
            and never secret values.
        retryable: Whether retrying the same operation could succeed.
        retry_after_seconds: Server-directed delay, when one was supplied.
    """

    code: str = "APP_ERROR"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        user_message: str | None = None,
        context: dict[str, Any] | None = None,
        retry_after_seconds: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        """Initialise the error."""
        super().__init__(message)
        self.message = message
        self.user_message = user_message or "Something went wrong."
        self.context: dict[str, Any] = context or {}
        self.retry_after_seconds = retry_after_seconds
        self.cause = cause

    def __str__(self) -> str:
        """Return the code and developer-facing message."""
        return f"[{self.code}] {self.message}"


class DomainError(AppError):
    """A business rule was violated. Always a defect; never retried."""

    code = "DOMAIN_ERROR"


class InvariantViolationError(DomainError):
    """A domain invariant would be broken by the requested operation."""

    code = "DOMAIN_INVARIANT_VIOLATION"


class InvalidStateTransitionError(DomainError):
    """An entity was asked to move to a state it cannot reach from its current one."""

    code = "DOMAIN_INVALID_STATE_TRANSITION"


class ConfigurationError(AppError):
    """Configuration is missing, malformed or contradictory.

    Always fatal at startup. A misconfigured application that starts and behaves
    subtly wrongly is worse than one that refuses to start with a clear message.
    """

    code = "CONFIG_ERROR"


class MissingRequiredSettingError(ConfigurationError):
    """A setting with no default was not supplied by any configuration source."""

    code = "CONFIG_MISSING_REQUIRED"


class InvalidConfigurationValueError(ConfigurationError):
    """A configuration value failed type, range or enum validation."""

    code = "CONFIG_INVALID_VALUE"


class ConfigurationConflictError(ConfigurationError):
    """The same key was declared in two stores that must not both own it.

    Configuration files and the settings table have disjoint key sets by rule
    (ADR-028). This error reports a violation of that rule.
    """

    code = "CONFIG_CONFLICT"


class UnknownConfigurationKeyError(ConfigurationError):
    """An unrecognised key was present in a configuration source.

    Unknown keys are an error rather than a silent ignore, so a typo stops
    startup instead of being discovered later as an ineffective setting.
    """

    code = "CONFIG_UNKNOWN_KEY"
