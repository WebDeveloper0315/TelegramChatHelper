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

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from tgassist.domain.model.ai import AiVendor
from tgassist.domain.model.chat import ChatType
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


class DatabaseSection(_Section):
    """SQLite connection settings.

    See ``docs/DATABASE.md`` section 2 for why each pragma is set the way it is.
    """

    path: Path | None = Field(
        default=None,
        description="Database file. Defaults to <data_dir>/tgassist.db.",
    )
    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"] = Field(
        default="WAL",
        description=(
            "Write-ahead logging allows readers to proceed during a write, which "
            "is what keeps the interface responsive while a sync runs."
        ),
    )
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = Field(
        default="NORMAL",
        description=(
            "NORMAL is durable against application crashes under WAL, and only "
            "risks the last transaction on a power loss."
        ),
    )
    busy_timeout_ms: int = Field(
        default=5000,
        ge=0,
        le=120_000,
        description=(
            "How long a statement waits for a lock. Writes are serialised through "
            "one thread, so this covers overlap with maintenance and backups."
        ),
    )
    auto_migrate: bool = Field(
        default=True,
        description="Apply pending migrations at startup, after a backup.",
    )
    archive_dir: Path | None = Field(
        default=None,
        description="Archived message databases. Defaults to <data_dir>/archives.",
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
            "Checked by Container.start() before the database is opened."
        ),
    )


class AiSection(_Section):
    """Which model runs AI tasks, and what is recorded about them (ADR-057)."""

    vendor: AiVendor = Field(
        default=AiVendor.FAKE,
        description=(
            "Which provider answers. Defaults to the deterministic fake, so a "
            "fresh installation has a working AI boundary that reaches no "
            "network and costs nothing. Set to a real vendor when a key is "
            "configured."
        ),
    )
    model: str = Field(
        default="fake-local-1",
        min_length=1,
        max_length=128,
        description=(
            "The model identifier, as the vendor spells it. Recorded verbatim "
            "on every call, so an expensive one can be traced to the exact "
            "model that made it."
        ),
    )
    api_key_ref: str = Field(
        default="ANTHROPIC_API_KEY",
        min_length=1,
        description=(
            "A NAME in the credential store, never the key itself (ADR-021). "
            "Unused by the fake vendor, which reaches nothing."
        ),
    )
    endpoint: str | None = Field(
        default=None,
        description=(
            "Where to send requests. Null uses the vendor's own. Set for a "
            "proxy or a compatible host -- not for a different vendor, which "
            "needs its own adapter."
        ),
    )
    input_cost_per_million: Decimal | None = Field(
        default=None,
        ge=0,
        description=(
            "What a million input tokens cost, for the estimate recorded on "
            "each call. Null records no cost at all, which is honest: a cost "
            "of zero would claim the call was free."
        ),
    )
    output_cost_per_million: Decimal | None = Field(
        default=None,
        ge=0,
        description="The same for output tokens.",
    )
    cost_currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3,
        description="What those prices are quoted in, ISO 4217.",
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=600,
        description=(
            "How long to wait for a model. Generous: a large request to a "
            "cloud model routinely takes tens of seconds, and a timeout "
            "shorter than the work reports failures for calls that were about "
            "to succeed -- while still being billed for them."
        ),
    )
    max_output_tokens: int = Field(
        default=4096,
        ge=1,
        le=200_000,
        description="A ceiling on any one answer, so a runaway generation is bounded.",
    )
    store_responses: bool = Field(
        default=False,
        description=(
            "Store the text of each response beside its digest. **Off**, and "
            "rejected outright in the production profile: a response carries "
            "conversation content once a real task runs, and SECURITY.md "
            "section 9 makes no exception for instrumentation. The digest is "
            "always stored, which is what deterministic replay needs "
            "(ADR-057)."
        ),
    )


class MemorySection(_Section):
    """What this application does with the facts a model proposes (ADR-058).

    Every value here is a *policy*: a judgement a user may reasonably change,
    and none of which makes a proposal invalid -- only unworthy of somebody's
    attention. The rules that decide whether a proposal is well formed are in
    the schema and the entity, where configuration cannot reach them.
    """

    min_confidence: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description=(
            "Below this, a proposal is discarded rather than queued. The "
            "confidence is self-reported and poorly calibrated, so this is a "
            "coarse filter and nothing more is claimed of it. Raising it "
            "shortens the queue and loses true facts the model was unsure of."
        ),
    )
    max_proposals_per_conversation: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "The most one extraction run may store. A model that returns thirty "
            "facts about one exchange has misunderstood the task, and a review "
            "queue is only useful while it is short enough to read."
        ),
    )
    context_message_limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description=(
            "How many of a conversation's messages to show the model, counting "
            "from the end. A bound on the request, and therefore on its cost."
        ),
    )
    max_message_chars: int = Field(
        default=2000,
        ge=100,
        le=100_000,
        description=(
            "How much of any one message to show. Bounds the payload space "
            "available to a prompt injection attempt (SECURITY.md section 12)."
        ),
    )
    context_token_budget: int = Field(
        default=800,
        ge=1,
        le=100_000,
        description=(
            "The most a memory context may cost, estimated at four characters "
            "to a token. Spent in ranking order: what does not fit is left out "
            "and reported, never shortened -- a truncated fact is a different "
            "fact (ADR-060)."
        ),
    )
    context_max_memories: int = Field(
        default=20,
        ge=1,
        le=500,
        description=(
            "The most memories one context may contain, however small they "
            "are. Forty true facts are worse than eight: the model has to "
            "weigh them all, and the eight that matter get diluted."
        ),
    )
    max_candidates: int = Field(
        default=500,
        ge=1,
        le=10_000,
        description=(
            "How many of a contact's memories to consider before ranking. A "
            "bound on the cost of building a context. Reaching it means "
            "ranking did not see everything, which is reported rather than "
            "hidden."
        ),
    )


class SuggestionSection(_Section):
    """What bounds a prompt asking for a reply (ADR-061).

    Budgets and limits only. The *order* things appear in, and the order they
    are removed in when the budget bites, are domain rules with stated
    justifications rather than preferences -- a configurable ordering would make
    every user's prompt a different experiment.
    """

    prompt_token_budget: int = Field(
        default=2000,
        ge=1,
        le=200_000,
        description=(
            "The most the assembled memories and conversation may cost, "
            "estimated at four characters to a token. Excludes the system "
            "prompt and the task, which are never trimmed. When it bites, the "
            "oldest messages go first and then the lowest-ranked memories."
        ),
    )
    recent_message_limit: int = Field(
        default=20,
        ge=1,
        le=500,
        description=(
            "How many recent messages to consider. A bound on the request, and "
            "on how far back a suggestion can be influenced from."
        ),
    )
    max_message_chars: int = Field(
        default=2000,
        ge=100,
        le=100_000,
        description=(
            "How much of any one message to show. Bounds the payload space "
            "available to a prompt injection attempt (SECURITY.md section 12). "
            "A message cut here is marked, so the model can tell."
        ),
    )
    minimum_messages: int = Field(
        default=1,
        ge=1,
        le=100,
        description=(
            "How many messages survive whatever the budget says. One: the "
            "thing being replied to. Without it a suggestion is a guess about "
            "a conversation the model cannot see."
        ),
    )


class ConversationSection(_Section):
    """What divides one conversation from the next (ADR-056)."""

    gap_minutes: int = Field(
        default=360,
        ge=1,
        le=100_000,
        description=(
            "Silence longer than this begins a new conversation. Six hours by "
            "default: long enough that an evening exchange with a break for "
            "dinner stays one conversation, short enough that yesterday's is a "
            "different one. Changing it changes boundaries, which is what "
            "'tgassist conversation rebuild' is for."
        ),
    )
    max_messages: int = Field(
        default=200,
        ge=1,
        le=10_000,
        description=(
            "A conversation holds at most this many messages. Not a claim about "
            "meaning -- an exchange does not stop being one exchange at message "
            "201 -- but a bound on how much context a later AI feature can be "
            "asked to hold, without which every downstream token budget is "
            "unbounded too."
        ),
    )
    segment_on_ingest: bool = Field(
        default=True,
        description=(
            "Re-segment a chat whenever messages are stored for it. Off leaves "
            "conversations to 'tgassist conversation rebuild', which is what a "
            "bulk import wants: segmenting after every batch of a 50 000-message "
            "backfill is correct but redundant."
        ),
    )


class TelegramSection(_Section):
    """How Telegram is reached, and what is done with what it returns.

    The library half follows ADR-047; the synchronisation half follows ADR-053.
    """

    tdjson_path: Path | None = Field(
        default=None,
        description=(
            "Explicit path to the tdjson shared library. Highest precedence, "
            "and still checksum-verified: naming a file does not make it "
            "trusted."
        ),
    )
    minimum_version: str = Field(
        default="1.8.0",
        description=(
            "Lowest TDLib version accepted. The client API this application "
            "uses stabilised in 1.8; below it the same symbols behave "
            "differently, which is worse than their being absent."
        ),
    )
    log_verbosity: int = Field(
        default=0,
        ge=0,
        le=10,
        description=(
            "TDLib's own log level, applied immediately after loading. It "
            "defaults to 5 and writes to standard error, which would put "
            "library chatter into command output."
        ),
    )
    search_system_library_path: bool = Field(
        default=True,
        description=(
            "Consider a system-installed tdjson as the last candidate. Turn "
            "off to require an explicitly configured or vendored library."
        ),
    )
    api_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Application identifier issued by https://my.telegram.org. It "
            "identifies this installation to Telegram, not the user, and has "
            "no default: there is nothing to derive it from."
        ),
    )
    api_hash_ref: str = Field(
        default="TELEGRAM_API_HASH",
        min_length=1,
        description=(
            "Name under which the application hash is held in the SecretStore. "
            "A name, never the value (ADR-021); the _ref suffix is what the "
            "logging pipeline redacts on."
        ),
    )
    device_model: str = Field(
        default="Desktop",
        min_length=1,
        description=(
            "What this client calls itself in Telegram's active-sessions list. "
            "The user sees it when deciding whether to revoke a session, so it "
            "should be recognisable rather than accurate."
        ),
    )
    backfill_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description=(
            "Messages per history fetch and per transaction. Aligned with "
            "TDLib's practical page size, so a batch is one round trip rather "
            "than a fraction of one. The whole application has one transaction "
            "at a time (ADR-034), so this is also how long everything else "
            "waits while a backfill writes."
        ),
    )
    backfill_horizon_days: int = Field(
        default=365,
        ge=0,
        description=(
            "How far back a history backfill reaches. Zero means no limit, "
            "which is possible but never the default (PROJECT_SPEC section "
            "4.1). Lowering it does not delete anything already stored; "
            "raising it reopens a completed backfill (ADR-054)."
        ),
    )
    catch_up_pages: int = Field(
        default=20,
        ge=1,
        le=1000,
        description=(
            "How many history pages 'tgassist sync live' reads forward, per "
            "chat, to recover what arrived while nothing was running. A chat "
            "that received more than this while the process was down is better "
            "served by re-running the backfill than by an unbounded walk that "
            "holds the connection (ADR-055)."
        ),
    )
    live_max_restarts: int = Field(
        default=5,
        ge=0,
        le=100,
        description=(
            "How many recoverable failures 'tgassist sync live' rides out "
            "before giving up and reporting the last one. Zero means the first "
            "failure ends the run, which is what a script wants and a person "
            "watching a terminal usually does not."
        ),
    )
    update_queue_size: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description=(
            "How many Telegram updates to hold before backpressure reaches "
            "TDLib. A full queue stops the dispatch loop, which stops the "
            "receive thread, which leaves TDLib holding the backlog -- nothing "
            "is dropped anywhere along that chain. Raising it buys tolerance "
            "for a slow consumer at the cost of memory."
        ),
    )
    sync_chat_types: tuple[ChatType, ...] = Field(
        default=(ChatType.PRIVATE,),
        description=(
            "Which kinds of chat are synchronised when first discovered. Every "
            "kind is recorded either way, so nothing is hidden; this decides "
            "only the initial sync_enabled, and nothing revisits it afterwards "
            "-- a chat the user switched off stays off (ADR-053)."
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
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    logging: LoggingSection = Field(default_factory=LoggingSection)
    security: SecuritySection = Field(default_factory=SecuritySection)
    telegram: TelegramSection = Field(default_factory=TelegramSection)
    conversation: ConversationSection = Field(default_factory=ConversationSection)
    ai: AiSection = Field(default_factory=AiSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    suggestion: SuggestionSection = Field(default_factory=SuggestionSection)

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
        if self.profile is Profile.PRODUCTION and self.ai.store_responses:
            # The same rule, for the same reason: a model response carries
            # conversation content once a real task runs, and storing it is a
            # deliberate diagnostic act rather than a configuration-file
            # decision (ADR-057).
            msg = (
                "ai.store_responses cannot be enabled in the production profile; "
                "it stores model responses, which carry conversation content"
            )
            raise ValueError(msg)
        return self

    @property
    def paths(self) -> AppPaths:
        """Return the resolved directory layout."""
        return AppPaths.from_data_dir(self.app.data_dir or default_data_dir())

    @property
    def database_path(self) -> Path:
        """Return the resolved database file path."""
        return self.database.path or (self.paths.data_dir / "tgassist.db")

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
