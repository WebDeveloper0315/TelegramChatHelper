"""What a model invocation is, and what it costs.

This module is the vocabulary every later AI capability speaks: memory
extraction, summarisation, planning, reply generation. None of them exists yet,
and that is deliberate -- the boundary is built first so that each of them is a
*use* of it rather than a reason to change it.

What is here, and what is not
-----------------------------

Here: what an invocation is (:class:`AiRequest` lives on the port, this is the
record of one having happened), which model performed it, what it consumed, and
how it ended.

Not here: prompts, schemas, parsing, retries, repair. Those are the *task's*
concerns, and a task is what slice 9b builds. Keeping them out is what makes an
``AiCall`` mean one thing -- a request went out and something came back -- rather
than a summary of a pipeline nobody has written.

Append-only
-----------

An :class:`AiCall` records something that happened at an instant: a request was
made, a model answered or did not, tokens were spent, money was owed. None of
those become untrue later. The aggregate therefore has no transitions at all --
the same shape ``Message`` has, and for the same reason (ADR-046) -- and its
repository has no update and no delete (ADR-057).

Content
-------

An ``AiCall`` records **metadata**. The prompt text and the response text are
conversation content once a real task passes real messages through them, and
``SECURITY.md`` section 9 does not make an exception for instrumentation. What is
always stored is a *digest* of the response, which is what deterministic replay
needs and what content is not.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.identifiers import (
    AccountId,
    AiCallId,
    ChatId,
    require_positive_identifier,
)

#: How many characters of a response digest are stored. A SHA-256 prefix this
#: long distinguishes every response any real workload produces, and a truncated
#: hash is a further reminder that this is an identity check rather than a way to
#: recover what was said.
DIGEST_LENGTH: Final = 32

MAX_TASK_KIND_LENGTH: Final = 64
MAX_PROMPT_ID_LENGTH: Final = 64
MAX_VERSION_LENGTH: Final = 32
MAX_MODEL_LENGTH: Final = 128


class AiVendor(StrEnum):
    """Who provides a model.

    An enumeration rather than free text because each member needs an adapter,
    so adding one is a code change whatever this type says. Naming them makes
    the set of things that *can* be configured the set of things that work.

    ``DOMAIN_MODEL.md`` section 5.24 calls this attribute ``provider_name`` and
    the goal for this slice called it ``AiProvider``. It is named ``AiVendor``
    here because :class:`~tgassist.domain.ports.ai_provider.AiProvider` is the
    port, and two things of that name cannot be imported into one module.
    """

    ANTHROPIC = "anthropic"
    """Claude models, over Anthropic's HTTP API. External by nature."""

    FAKE = "fake"
    """A deterministic double. Never reaches a network, and its presence in this
    enumeration is deliberate: a scripted provider is a legitimate configuration
    for a developer and for the test suite, and hiding it behind a flag would
    make the tested path different from the shipped one."""


class DataBoundary(StrEnum):
    """Whether using a model sends content off the device.

    The distinction ADR-024 is built on. It belongs to the *model*, not to the
    configuration: a cloud model is external however it is configured, and
    letting a setting say otherwise would put the privacy guarantee in a file
    the user can edit.
    """

    LOCAL = "local"
    """Nothing leaves the device."""

    EXTERNAL = "external"
    """Content is transmitted to a third party."""


class FinishReason(StrEnum):
    """Why the model stopped generating.

    The model's own account of itself, distinct from :class:`AiOutcome`, which
    is *this application's* account of the call. A response can finish for the
    reason ``length`` and still be a successful call; conflating the two would
    hide the most common cause of a truncated answer.
    """

    STOP = "stop"
    """The model finished what it had to say."""

    LENGTH = "length"
    """It hit the output-token ceiling. The answer is truncated."""

    CONTENT_FILTER = "content_filter"
    """The provider refused to continue."""

    OTHER = "other"
    """Something this version does not recognise. Recorded rather than refused:
    a provider that adds a stop reason must not make calls unrecordable."""


class AiOutcome(StrEnum):
    """How the call ended, from this application's side.

    Every member is written to the database, including the failures --
    success-only instrumentation hides exactly the expensive cases
    (``DOMAIN_MODEL.md`` section 5.25).
    """

    SUCCESS = "success"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    """The provider answered, and the answer was an error."""

    MALFORMED = "malformed"
    """The provider answered with something this application cannot read."""

    CANCELLED = "cancelled"
    """The caller went away. Recorded because the tokens were still spent."""

    REFUSED = "refused"
    """This application declined to make the call -- almost always the privacy
    gate (ADR-024). No tokens were spent, and the record exists so that a user
    asking "why did nothing happen" has an answer."""

    @property
    def is_success(self) -> bool:
        """Whether the call produced a usable answer."""
        return self is AiOutcome.SUCCESS

    @property
    def reached_provider(self) -> bool:
        """Whether anything was sent. ``False`` only for a refusal."""
        return self is not AiOutcome.REFUSED


@dataclass(frozen=True, slots=True)
class PromptVersion:
    """Which prompt, at which revision, produced a request.

    Recorded from the first call, before prompts are complicated enough to need
    it. The reason is that the question it answers -- "did the output change
    because the model changed or because we changed the prompt?" -- can only be
    answered by data that was already being collected when the change happened.
    Adding the field later means the first interesting comparison is the one you
    cannot make.

    Attributes:
        prompt_id: What the prompt is called, stable across revisions.
        version: Its revision, as text. Compared for equality, not ordered:
            this exists to group calls that used the same instructions, and an
            ordering would invite arithmetic on a value whose scheme belongs to
            whoever writes the prompts.
    """

    prompt_id: str
    version: str

    def __post_init__(self) -> None:
        """Validate both halves.

        Raises:
            DomainValidationError: If either is blank or too long.
        """
        _require_text(self.prompt_id, name="prompt_id", limit=MAX_PROMPT_ID_LENGTH)
        _require_text(self.version, name="prompt version", limit=MAX_VERSION_LENGTH)

    def __str__(self) -> str:
        """Render as ``prompt_id@version``, the form logs and reports use."""
        return f"{self.prompt_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What a call consumed.

    Both counts are optional, and that is not laziness: a provider may not
    report them, and a local model may not count them at all. Business logic has
    to tolerate absence rather than assume zero, because zero is a claim that a
    call was free.

    Attributes:
        input_tokens: Tokens in the request, or ``None`` if unreported.
        output_tokens: Tokens in the response, or ``None`` if unreported.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        """Refuse a negative count.

        Raises:
            DomainValidationError: If either count is below zero.
        """
        for value, name in ((self.input_tokens, "input"), (self.output_tokens, "output")):
            if value is not None and value < 0:
                msg = f"A call cannot consume {value} {name} tokens"
                raise DomainValidationError(
                    msg, user_message="The provider reported an impossible token count."
                )

    @property
    def total(self) -> int | None:
        """Tokens in and out, or ``None`` when either half is unknown.

        ``None`` rather than a partial sum. A total that silently counted only
        the half that was reported would read as a small call rather than as an
        unmeasured one.
        """
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    @property
    def is_measured(self) -> bool:
        """Whether the provider reported enough to compute a cost."""
        return self.input_tokens is not None and self.output_tokens is not None


@dataclass(frozen=True, slots=True)
class Cost:
    """What a call is estimated to have cost.

    **Always an estimate.** Providers bill on their own accounting, prices
    change, and this is computed from rates held locally. Naming it as an
    estimate in the type is what stops it being summed into an invoice.

    ``Decimal`` rather than ``float``: fractions of a cent accumulated across
    fifty thousand calls are exactly where binary floating point drifts, and a
    cost report that does not add up is worse than none.

    Attributes:
        amount: The estimate, never negative.
        currency: ISO 4217 code. Carried because a provider's prices are quoted
            in one, and a bare number would be compared across currencies by
            somebody eventually.
    """

    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        """Validate the amount and the currency.

        Raises:
            DomainValidationError: If the amount is negative or the currency is
                not a three-letter code.
        """
        if self.amount < 0:
            msg = f"A call cannot cost {self.amount}"
            raise DomainValidationError(msg, user_message="That is not a possible cost.")
        if len(self.currency) != _CURRENCY_LENGTH or not self.currency.isalpha():
            msg = f"{self.currency!r} is not a currency code"
            raise DomainValidationError(msg, user_message="That is not a currency code.")

    @classmethod
    def free(cls, currency: str = "USD") -> Cost:
        """Return a cost of nothing.

        What a local model costs, and what a refused call costs. Distinct from
        *unknown*: a call whose tokens were never reported has ``None`` for its
        cost, not zero.
        """
        return cls(amount=Decimal(0), currency=currency)

    def __add__(self, other: Cost) -> Cost:
        """Add two costs in the same currency.

        Raises:
            DomainValidationError: If the currencies differ. Summing across them
                would need a rate this application has no business holding.
        """
        if self.currency != other.currency:
            msg = f"Cannot add {self.currency} to {other.currency}"
            raise DomainValidationError(
                msg, user_message="Those costs are in different currencies."
            )
        return Cost(amount=self.amount + other.amount, currency=self.currency)

    def __str__(self) -> str:
        """Render with enough places to show a fraction of a cent."""
        return f"{self.amount:.6f} {self.currency}"


@dataclass(frozen=True, slots=True)
class AiModel:
    """Which model, and what using it implies.

    Value object rather than a row: a model is identified by what it *is*, and
    two configurations naming the same vendor and identifier describe the same
    model. ``DOMAIN_MODEL.md`` section 5.24 gives ``ai_providers`` a table; this
    slice does not create one, because nothing yet needs to enumerate models at
    runtime and configuration already names the one in use.

    Attributes:
        vendor: Who provides it.
        identifier: Its name, as the vendor spells it. Recorded verbatim so an
            expensive call can be traced to the exact model that made it.
        data_boundary: Whether using it sends content off the device. A property
            of the model, not of the configuration (ADR-024).
        input_cost_per_million: Price of a million input tokens, in
            :attr:`currency`. ``None`` when the model is free or unpriced, which
            is different from zero only in that no cost is computed at all.
        output_cost_per_million: The same for output tokens.
        currency: What those prices are quoted in.
    """

    vendor: AiVendor
    identifier: str
    data_boundary: DataBoundary
    input_cost_per_million: Decimal | None = None
    output_cost_per_million: Decimal | None = None
    currency: str = "USD"

    def __post_init__(self) -> None:
        """Validate the identifier and the rates.

        Raises:
            DomainValidationError: If the identifier is blank or a rate is
                negative.
        """
        _require_text(self.identifier, name="model identifier", limit=MAX_MODEL_LENGTH)
        for rate, name in (
            (self.input_cost_per_million, "input"),
            (self.output_cost_per_million, "output"),
        ):
            if rate is not None and rate < 0:
                msg = f"A model cannot charge {rate} per million {name} tokens"
                raise DomainValidationError(msg, user_message="That is not a possible price.")

    @property
    def is_external(self) -> bool:
        """Whether using this model transmits content to a third party."""
        return self.data_boundary is DataBoundary.EXTERNAL

    @property
    def is_priced(self) -> bool:
        """Whether enough is known to estimate what a call costs."""
        return self.input_cost_per_million is not None and self.output_cost_per_million is not None

    def cost_of(self, usage: TokenUsage) -> Cost | None:
        """Estimate what a call consuming these tokens cost.

        ``None`` when either the model is unpriced or the provider did not
        report the tokens. Returning zero in those cases would record a call as
        free when what is true is that its cost is unknown, and a cost report
        built on that would understate exactly the calls worth investigating.
        """
        if not self.is_priced or not usage.is_measured:
            return None

        # mypy cannot see that is_priced and is_measured have narrowed these.
        assert self.input_cost_per_million is not None  # noqa: S101 - narrowing, not a check
        assert self.output_cost_per_million is not None  # noqa: S101
        assert usage.input_tokens is not None  # noqa: S101
        assert usage.output_tokens is not None  # noqa: S101

        per_million = Decimal(1_000_000)
        amount = (
            self.input_cost_per_million * Decimal(usage.input_tokens)
            + self.output_cost_per_million * Decimal(usage.output_tokens)
        ) / per_million
        return Cost(amount=amount, currency=self.currency)

    def __str__(self) -> str:
        """Render as ``vendor/identifier``, the form reports use."""
        return f"{self.vendor.value}/{self.identifier}"


@dataclass(frozen=True, slots=True)
class AiCall:
    """One model invocation, recorded.

    Immutable and append-only, like ``Message``: there is nothing an AiCall
    becomes. It has no transitions, its repository has no update and no delete,
    and the absence of both is what says so (ADR-057).

    Attributes:
        id: Local identifier.
        account_id: Whose call this was.
        chat_id: The chat whose content the call was about, or ``None`` for a
            task that is not about a conversation. Present because the privacy
            gate is per chat, so a record without it could not be audited
            against the permission that allowed it.
        model: Which model, and what using it implied.
        prompt: Which prompt at which revision.
        task_kind: What the call was for -- ``extract_memory``, ``summarise``.
            Free text rather than an enumeration, because the set grows with
            every later milestone and an enumeration would make this module
            change each time one is added.
        usage: What it consumed.
        cost: What it is estimated to have cost, or ``None`` when unknown.
        outcome: How it ended.
        finish_reason: Why the model stopped, or ``None`` when it never
            answered.
        latency_ms: How long it took, including a timeout's full wait.
        response_digest: A truncated SHA-256 of the response text, or ``None``
            when there was none. What deterministic replay compares; content is
            what it deliberately is not (``SECURITY.md`` section 9).
        response_text: The response itself, and **normally ``None``**. Stored
            only when ``ai.store_responses`` is on, which the production profile
            refuses -- the same arrangement ``logging.diagnostic_mode`` has, and
            for the same reason.
        created_at: When the call was made, UTC.
    """

    id: AiCallId
    account_id: AccountId
    chat_id: ChatId | None
    model: AiModel
    prompt: PromptVersion
    task_kind: str
    usage: TokenUsage
    cost: Cost | None
    outcome: AiOutcome
    finish_reason: FinishReason | None
    latency_ms: int
    response_digest: str | None
    response_text: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        """Validate every invariant this entity is responsible for.

        Raises:
            DomainValidationError: If any invariant is violated.
        """
        require_positive_identifier(self.id, name="AI call id")
        require_positive_identifier(self.account_id, name="Account id")
        if self.chat_id is not None:
            require_positive_identifier(self.chat_id, name="Chat id")

        _require_text(self.task_kind, name="task kind", limit=MAX_TASK_KIND_LENGTH)

        if self.latency_ms < 0:
            msg = f"A call cannot take {self.latency_ms}ms"
            raise DomainValidationError(msg, user_message="That is not a possible duration.")

        if self.outcome.is_success and self.finish_reason is None:
            # A successful call is one the model answered, and a model that
            # answered said why it stopped. Allowing the pair would make
            # "succeeded" mean two different things.
            msg = "A successful call records why the model stopped"
            raise DomainValidationError(
                msg, user_message="That call record is incomplete.", context={"id": int(self.id)}
            )
        if not self.outcome.is_success and self.finish_reason is not None:
            msg = f"A call that ended as {self.outcome.value} has no finish reason"
            raise DomainValidationError(msg, user_message="That call record is inconsistent.")

        if not self.outcome.reached_provider and self.usage.total not in (None, 0):
            msg = "A refused call spends no tokens"
            raise DomainValidationError(msg, user_message="That call record is inconsistent.")

        if self.response_text is not None and self.response_digest is None:
            msg = "A stored response is always accompanied by its digest"
            raise DomainValidationError(msg, user_message="That call record is inconsistent.")

        _require_utc(self.created_at, name="created_at")

    # -- Construction -----------------------------------------------------

    @classmethod
    def record(  # noqa: PLR0913 - an entity factory takes one argument per field
        cls,
        *,
        call_id: AiCallId,
        account_id: AccountId,
        model: AiModel,
        prompt: PromptVersion,
        task_kind: str,
        outcome: AiOutcome,
        latency_ms: int,
        now: datetime,
        chat_id: ChatId | None = None,
        usage: TokenUsage | None = None,
        finish_reason: FinishReason | None = None,
        response: str | None = None,
        keep_response: bool = False,
    ) -> AiCall:
        """Build a record of a call that has already happened.

        The cost is computed here rather than supplied, so that one place
        decides what a call cost and no caller can record a number the model's
        rates do not support.

        Args:
            call_id: Local identifier.
            account_id: Whose call this was.
            model: Which model performed it.
            prompt: Which prompt at which revision.
            task_kind: What it was for.
            outcome: How it ended.
            latency_ms: How long it took.
            now: When it happened, from the injected clock.
            chat_id: The chat it was about, if any.
            usage: What it consumed. Absent means unreported.
            finish_reason: Why the model stopped, on success.
            response: The response text, used to compute the digest.
            keep_response: Whether to store the text as well as its digest.
                Off unless diagnostics are enabled (ADR-057).
        """
        consumed = usage if usage is not None else TokenUsage()
        return cls(
            id=call_id,
            account_id=account_id,
            chat_id=chat_id,
            model=model,
            prompt=prompt,
            task_kind=task_kind,
            usage=consumed,
            cost=model.cost_of(consumed),
            outcome=outcome,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            response_digest=digest_of(response),
            response_text=response if keep_response else None,
            created_at=now,
        )

    # -- Derived state ----------------------------------------------------

    @property
    def succeeded(self) -> bool:
        """Whether the call produced a usable answer."""
        return self.outcome.is_success

    @property
    def was_billable(self) -> bool:
        """Whether this call plausibly cost money.

        True for any call that reached an external provider, including one that
        failed: a request that timed out after the model had begun generating
        was still charged for.
        """
        return self.model.is_external and self.outcome.reached_provider

    @property
    def latency_seconds(self) -> float:
        """How long it took, in the unit a person reads."""
        return self.latency_ms / 1000


def digest_of(response: str | None) -> str | None:
    """Return the stored fingerprint of a response, or ``None``.

    A truncated SHA-256 over UTF-8. It exists so that two runs of the same
    request can be compared without either of them being readable, which is what
    deterministic replay needs and what ``SECURITY.md`` section 9 permits.
    """
    if response is None:
        return None
    return hashlib.sha256(response.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


_CURRENCY_LENGTH: Final = 3


def _require_text(value: str, *, name: str, limit: int) -> None:
    """Raise unless a value is non-blank and within its length."""
    if not value.strip():
        msg = f"A {name} is required"
        raise DomainValidationError(msg, user_message=f"A {name} is required.")
    if len(value) > limit:
        msg = f"A {name} may be at most {limit} characters, got {len(value)}"
        raise DomainValidationError(msg, user_message=f"That {name} is too long to store.")


def _require_utc(value: datetime, *, name: str) -> None:
    """Raise unless ``value`` is timezone-aware and in UTC."""
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware; naive datetimes have no defined instant"
        raise DomainValidationError(msg, user_message="That call has an invalid timestamp.")
    if value.utcoffset() != UTC.utcoffset(None):
        msg = f"{name} must be UTC, got offset {value.utcoffset()}"
        raise DomainValidationError(msg, user_message="That call has an invalid timestamp.")


__all__ = [
    "DIGEST_LENGTH",
    "AiCall",
    "AiModel",
    "AiOutcome",
    "AiVendor",
    "Cost",
    "DataBoundary",
    "FinishReason",
    "PromptVersion",
    "TokenUsage",
    "digest_of",
]
