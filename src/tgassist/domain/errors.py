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


class TelegramRuntimeError(AppError):
    """The Telegram native runtime is unusable.

    A family rather than one error, because the remedies differ completely: an
    absent library needs installing, an unrecognised one needs its provenance
    established, and an old one needs upgrading. A caller that only knows "TDLib
    failed" cannot tell the user which.

    These are :class:`AppError` rather than :class:`DomainError` because nothing
    about them is a business rule -- they describe the machine the application
    is running on.
    """

    code = "TELEGRAM_RUNTIME_ERROR"


class TdlibNotFoundError(TelegramRuntimeError):
    """No candidate library exists in any searched location.

    Distinct from a library that exists but is untrusted: this one is a setup
    step the user has not performed, and the remedy is a download or a build.
    """

    code = "TELEGRAM_TDLIB_NOT_FOUND"


class TdlibUnverifiedError(SecurityError):
    """A library was found whose digest is not in the pinned manifest.

    A :class:`SecurityError`, not merely a runtime one. ``tdjson`` is loaded
    into this process and sees the session key, every message and the network,
    so an unrecognised binary is a security question rather than a
    configuration inconvenience (ADR-047).

    Never resolved by searching elsewhere. Falling through to the next candidate
    would mean an attacker who plants a library in a high-precedence location
    gets a silent retry rather than a refusal.
    """

    code = "TELEGRAM_TDLIB_UNVERIFIED"


class TdlibLoadFailedError(TelegramRuntimeError):
    """A verified library could not be loaded by the platform.

    Usually a missing transitive dependency -- OpenSSL or zlib on Linux, the
    Visual C++ runtime on Windows -- or an architecture mismatch between the
    library and the interpreter.
    """

    code = "TELEGRAM_TDLIB_LOAD_FAILED"


class TdlibIncompatibleError(TelegramRuntimeError):
    """The library loaded but is not one this application can use.

    Either it does not export the entry points the client API requires, or it
    reports a version below the supported minimum. Both mean the file is a real
    TDLib but the wrong one.
    """

    code = "TELEGRAM_TDLIB_INCOMPATIBLE"


class TdlibNotRunningError(TelegramRuntimeError):
    """An operation needed a running client and there was not one.

    Covers "not started yet", "already stopped" and "the receive thread died",
    because from a caller's position those are the same fact: there is nothing
    to talk to. Which of them applies is in the context.
    """

    code = "TELEGRAM_TDLIB_NOT_RUNNING"


class TdlibRequestFailedError(TelegramRuntimeError):
    """TDLib answered a request with an error.

    The request reached TDLib and was refused, which is different from the
    client being unable to send it. TDLib's own code and message are carried in
    the context: they name conditions like an invalid code or a flood wait, and
    a caller that cannot see them cannot react to them.
    """

    code = "TELEGRAM_TDLIB_REQUEST_FAILED"


class TdlibShutdownTimeoutError(TelegramRuntimeError):
    """The receive thread did not stop when asked.

    Raised *after* every waiter has been released, so a thread that will not
    die does not also hang the application. It is still reported, because a
    thread ignoring a stop request is a defect rather than a tidy-up detail.
    """

    code = "TELEGRAM_TDLIB_SHUTDOWN_TIMEOUT"


class TelegramError(TelegramRuntimeError):
    """Telegram refused an operation for a reason of its own.

    The catch-all beneath the specific cases below: the request reached
    Telegram, was understood, and was declined. TDLib's own code and message
    travel in the context, because a caller that cannot see them cannot react
    to them.
    """

    code = "TELEGRAM_ERROR"


class TelegramNotConfiguredError(TelegramRuntimeError):
    """The application credentials Telegram requires are absent or incomplete.

    ``api_id`` and ``api_hash`` identify *this application* to Telegram and are
    obtained once, by hand, from https://my.telegram.org. There is no default
    and no way to derive them, so this is a setup step rather than a failure --
    which is why it is reported separately from every other connection problem.
    """

    code = "TELEGRAM_NOT_CONFIGURED"


class AuthorizationError(TelegramError):
    """A credential submitted during login was rejected.

    Recoverable by definition: a mistyped code is the ordinary case, and the
    handler decides whether to try again. **The rejected value never appears in
    this error**, in its message or its context -- an error object is exactly
    the thing that reaches a log or a crash report.
    """

    code = "TELEGRAM_AUTHORIZATION_FAILED"


class SessionRevokedError(TelegramError):
    """Telegram no longer accepts this session.

    Raised for ``AUTH_KEY_UNREGISTERED`` and ``SESSION_REVOKED``: the user
    signed this device out from elsewhere, or Telegram invalidated it. Distinct
    from :class:`AuthorizationError` because nothing the user types will help --
    the remedy is a fresh login, and the local session material is now useless.
    """

    code = "TELEGRAM_SESSION_REVOKED"


class EventDispatchError(AppError):
    """An event could not be dispatched.

    Handler failures are isolated and never surface as this error. It is raised
    only when the bus itself refuses -- currently when publishing from a handler
    exceeds the recursion bound, which indicates an event cycle.
    """

    code = "EVENT_DISPATCH_ERROR"


class AiError(AppError):
    """The AI boundary could not do what was asked.

    The base every provider adapter normalises to. A caller catches these and
    never a transport exception, which is what stops the choice of HTTP library
    from reaching the application layer (ADR-057).
    """

    code = "AI_ERROR"


class AiNotConfiguredError(AiError):
    """No usable model is configured.

    Distinct from a provider failing: nothing was attempted, because there was
    nothing to attempt it with. Every AI feature is expected to degrade rather
    than break when this is raised (``PROJECT_SPEC.md`` section 4.2).
    """

    code = "AI_NOT_CONFIGURED"


class AiForbiddenError(AiError):
    """The privacy gate refused this call.

    Raised when a chat's ``ai_processing_mode`` does not permit the model that
    would answer -- an external model for a ``local_only`` chat, or any model at
    all for a ``disabled`` one (ADR-024). Not a failure of the provider: the
    request was never made, and that is the point.
    """

    code = "AI_FORBIDDEN"


class AiTimeoutError(AiError):
    """The model did not answer in time.

    Recorded as a call that happened, because it did: a request that timed out
    after generation had begun was still billed for.
    """

    code = "AI_TIMEOUT"


class AiRateLimitedError(AiError):
    """The provider refused for rate reasons.

    Distinct from :class:`AiProviderError` because it is the one failure worth
    retrying *later* rather than differently. Nothing retries yet; the
    distinction is recorded so that whatever does can tell them apart.
    """

    code = "AI_RATE_LIMITED"


class AiProviderError(AiError):
    """The provider refused for a reason of its own.

    The request reached the provider, was understood, and was declined. The
    provider's own status and message travel in the context, because a caller
    that cannot see them cannot react to them -- and neither ever carries the
    request or the response.
    """

    code = "AI_PROVIDER_ERROR"


class AiResponseError(AiError):
    """The provider answered with something this application cannot read.

    A malformed body, a missing field, a shape a version change introduced.
    Distinct from :class:`AiProviderError` because the remedy differs: one is
    the provider saying no, the other is this application not understanding yes.
    """

    code = "AI_RESPONSE_ERROR"


class PromptError(AppError):
    """Base for failures of the prompt registry.

    Separate from :class:`AiError` on purpose. An AI error is something a model
    or a provider did; a prompt error is something *this application* shipped
    wrong, and the two want different reactions -- one is retried or degraded
    around, the other is a defect that should stop startup.
    """

    code = "PROMPT_ERROR"


class PromptRegistryInvalidError(PromptError):
    """The prompt registry does not describe a usable set of prompts.

    A missing file, a version that is not declared, a template whose variables
    disagree with its declaration, a schema that uses a keyword the validator
    does not implement. Raised **at startup** rather than at generation time,
    because a prompt discovered to be broken while a user is waiting for a
    suggestion is the same defect discovered at the worst moment (ADR-026
    section 7).
    """

    code = "PROMPT_REGISTRY_INVALID"


class PromptNotFoundError(PromptError):
    """Something asked for a prompt the registry does not have.

    Distinct from an invalid registry: the registry is fine, the caller named
    a prompt that is not in it.
    """

    code = "PROMPT_NOT_FOUND"


class SchemaViolationError(AiError):
    """A model's answer did not satisfy the schema its prompt is bound to.

    Raised after the one repair attempt has also failed (ADR-020 section 4).
    An :class:`AiError` rather than a :class:`PromptError` because the *call*
    is what went wrong -- the prompt asked correctly and the model answered
    badly -- and because every AI feature is expected to degrade around it
    rather than break.

    The violations travel in the context. The payload does not: it is model
    output about a conversation, which is conversation content
    (``SECURITY.md`` section 9).
    """

    code = "AI_SCHEMA_VIOLATION"
