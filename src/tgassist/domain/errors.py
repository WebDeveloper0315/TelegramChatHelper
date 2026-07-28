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


class DomainValidationError(DomainError, ValueError):
    """A value violates a domain invariant.

    Inherits from ``ValueError`` as well as ``DomainError``, because it is
    genuinely both: an invalid argument in Python's vocabulary, and a broken
    business rule in this application's. The dual inheritance means idiomatic
    ``except ValueError`` still works while the error also carries a code and a
    user-facing message, so it can reach a person without a traceback.
    """

    code = "DOMAIN_VALIDATION"


class ConflictError(DomainError):
    """The requested change conflicts with something that already exists.

    Distinct from ConstraintViolationError, which reports a database constraint
    after the fact. This is raised by a use case that checked first, so the
    message can name the conflict in domain terms rather than naming a column.
    """

    code = "DOMAIN_CONFLICT"


class PersistenceError(AppError):
    """Storage could not satisfy a request.

    Adapters normalise driver exceptions into this family at the boundary, so a
    use case never catches ``sqlite3.IntegrityError`` or a SQLAlchemy type.
    """

    code = "PERSISTENCE_ERROR"


class DatabaseUnavailableError(PersistenceError):
    """The database could not be reached or opened."""

    code = "PERSISTENCE_DATABASE_UNAVAILABLE"
    retryable = True


class ConstraintViolationError(PersistenceError):
    """A write violated a uniqueness, foreign key or check constraint.

    Usually a defect rather than a condition: the constraints exist to make
    domain invariants unbreakable, so violating one means the caller tried to
    break an invariant.
    """

    code = "PERSISTENCE_CONSTRAINT_VIOLATION"


class RecordNotFoundError(PersistenceError):
    """A record required by the operation does not exist.

    Only raised by methods that promise a record. Ordinary lookups return
    ``None``, because absence is usually an expected state.
    """

    code = "PERSISTENCE_RECORD_NOT_FOUND"


class TransactionFailedError(PersistenceError):
    """A transaction could not be started, committed or rolled back."""

    code = "PERSISTENCE_TRANSACTION_FAILED"
    retryable = True


class MigrationFailedError(PersistenceError):
    """A schema migration did not complete.

    The database is left at its previous revision: migrations run inside a
    transaction so a failure rolls back rather than leaving a partial schema.
    """

    code = "PERSISTENCE_MIGRATION_FAILED"


class SchemaVersionError(PersistenceError):
    """The database schema revision is incompatible with this application.

    Raised when the database is *newer* than the application understands.
    Downgrading user data is never attempted, so the only safe response is to
    refuse to start and say which version is required.
    """

    code = "PERSISTENCE_SCHEMA_VERSION"


class IntegrityCheckFailedError(PersistenceError):
    """The database reported internal corruption."""

    code = "PERSISTENCE_INTEGRITY_CHECK_FAILED"


class SecurityError(AppError):
    """A security control could not be applied.

    These fail closed: an unavailable control denies the operation rather than
    letting it proceed unprotected.
    """

    code = "SECURITY_ERROR"


class SecretStoreUnavailableError(SecurityError):
    """The credential backend cannot be reached.

    Fatal at startup when ``security.require_secret_store`` is set, because
    continuing would mean storing a Telegram session without encryption.
    """

    code = "SECURITY_SECRET_STORE_UNAVAILABLE"


class ReadOnlySecretStoreError(SecurityError):
    """A write was attempted against a backend that cannot store secrets.

    Raised rather than silently discarding the write, so that a secret the
    caller believes is saved is never quietly lost.
    """

    code = "SECURITY_SECRET_STORE_READ_ONLY"


class EventDispatchError(AppError):
    """An event could not be dispatched.

    Handler failures are isolated and never surface as this error. It is raised
    only when the bus itself refuses -- currently when publishing from a handler
    exceeds the recursion bound, which indicates an event cycle.
    """

    code = "EVENT_DISPATCH_ERROR"
