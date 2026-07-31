"""The AI boundary.

**One port.** Not a `ChatProvider`, an `EmbeddingProvider` and a
`CompletionProvider`: one thing that takes instructions and returns text. Every
later AI capability -- memory extraction, summarisation, planning, reply
generation -- is a *caller* of this, not a reason to widen it.

`API.md` §11.1 specified a `LLMProvider` with `capabilities()`,
`context_window()`, `stream_generate()`, `count_tokens()` and `health_check()`.
None of them has a caller, so none of them is here (ADR-051's rule, applied to
the AI boundary as it was to Telegram's). They return when something asks.

Two methods
-----------

:attr:`AiProvider.model` says which model this is and what using it implies --
the privacy gate reads ``data_boundary`` from it before every call, and the
recorded ``AiCall`` reads the rest.

:meth:`AiProvider.generate` makes one call.

That is the entire surface, and it is enough to build a memory extractor on.

What is deliberately *not* here
-------------------------------

**No retries.** A provider that retried internally would make one logical call
into several billed ones with one recorded latency, which is exactly the
measurement this slice exists to make honest.

**No parsing, no schema validation, no repair.** A provider returns text. What
that text is supposed to mean belongs to the task that asked for it, and putting
it here would mean every task's output shape is a provider concern.

**No prompt rendering.** ``AiRequest`` carries already-rendered text and the
:class:`~tgassist.domain.model.ai.PromptVersion` that produced it. A provider
that rendered would need to know about templates, variables and injection
defences -- three things that are the same for every provider and therefore
belong on the other side of this line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.ai import AiModel, FinishReason, PromptVersion, TokenUsage

#: How long to wait for a model before giving up. Generous: a large request to a
#: cloud model routinely takes tens of seconds, and a timeout shorter than the
#: work would report failures for calls that were about to succeed -- while
#: still being billed for them.
DEFAULT_TIMEOUT_SECONDS: Final = 60.0

#: How many tokens to allow in a response when a caller does not say. Small
#: enough that a runaway generation is bounded, large enough for the structured
#: answers every planned task asks for.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 4096

#: The sampling temperature when a caller does not say. Zero, because every task
#: this application has planned wants the *same* answer for the same input:
#: extraction, summarisation and classification are not creative writing, and a
#: default that varied would make deterministic replay impossible to even define.
DEFAULT_TEMPERATURE: Final = 0.0

MAX_TEMPERATURE: Final = 2.0


@dataclass(frozen=True, slots=True)
class AiRequest:
    """One thing to ask a model.

    Already rendered. The prompt template, its variables and the delimiting that
    keeps conversation content from being read as instructions
    (``SECURITY.md`` section 12) all happen before this, because they are the
    same whichever provider answers.

    Attributes:
        instructions: The system prompt -- what the model is, and what it is for.
            ``None`` when the task needs none.
        content: What to act on. Untrusted text belongs here and never in
            :attr:`instructions`, which is the whole structural defence against
            prompt injection.
        prompt: Which prompt at which revision produced this. Carried on the
            request rather than looked up afterwards, so the recorded call names
            the revision that actually ran.
        task_kind: What this call is for, recorded on the call.
        max_output_tokens: A ceiling on the answer.
        temperature: Sampling temperature. Zero by default.
        timeout_seconds: How long to wait.
    """

    instructions: str | None
    content: str
    prompt: PromptVersion
    task_kind: str
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Validate what a request cannot be without.

        Raises:
            DomainValidationError: If the request could not produce an answer.
        """
        if not self.content.strip():
            msg = "A request needs something to act on"
            raise DomainValidationError(
                msg, user_message="There is nothing for the model to work on."
            )
        if self.max_output_tokens < 1:
            msg = f"A response needs at least one token, got {self.max_output_tokens}"
            raise DomainValidationError(
                msg, user_message="Allow the model at least one token to answer with."
            )
        if not 0.0 <= self.temperature <= MAX_TEMPERATURE:
            msg = f"Temperature must be between 0 and {MAX_TEMPERATURE}, got {self.temperature}"
            raise DomainValidationError(msg, user_message="That temperature is out of range.")
        if self.timeout_seconds <= 0:
            msg = f"A request must be allowed some time, got {self.timeout_seconds}"
            raise DomainValidationError(msg, user_message="Allow the model some time to answer.")


@dataclass(frozen=True, slots=True)
class AiResponse:
    """What a model returned.

    Text, and what it cost to produce. Not parsed, not validated: what the text
    is supposed to *be* belongs to whoever asked.

    Attributes:
        text: The answer.
        finish_reason: Why the model stopped. ``LENGTH`` means the answer is
            truncated, which a caller can only handle if it is told.
        usage: What the call consumed, as far as the provider reported it.
        model: Which model actually answered. Read from the response rather than
            assumed from the request, because a provider may route to a
            different revision than the one that was asked for -- and an
            expensive call attributed to the wrong model is a cost report that
            cannot be acted on.
    """

    text: str
    finish_reason: FinishReason
    usage: TokenUsage
    model: AiModel


@runtime_checkable
class AiProvider(Protocol):
    """The sole route to a language model.

    Contract, guaranteed by every implementation and verified by the shared
    contract suite:

    1. **It never writes to the database.** Recording a call is the application
       layer's job, which is what lets every AI feature be tested with a scripted
       provider and a real database.
    2. **It never retries.** One call in, one call out, one latency. A provider
       that retried internally would make one logical call into several billed
       ones and report the sum as a single measurement.
    3. **Failures are normalised.** Every implementation raises the errors in
       ``domain/errors.py`` -- ``AiTimeoutError``, ``AiRateLimitedError``,
       ``AiProviderError``, ``AiResponseError`` -- and never a transport
       exception. A caller that had to know which HTTP library was underneath
       would be coupled to the thing this port exists to hide.
    4. **Cancellation propagates.** ``asyncio.CancelledError`` passes through
       untouched, so shutdown is never delayed by a model that is still
       thinking.
    5. **It reports the model it used**, on :attr:`model` and again on each
       response.
    6. **Nothing here logs the request or the response.** Both are conversation
       content once a real task runs (``SECURITY.md`` section 9).
    """

    @property
    def model(self) -> AiModel:
        """Return which model this provider uses, and what using it implies.

        A property rather than a method because it is a fact about the provider,
        not a question it has to go and answer. The privacy gate reads
        ``data_boundary`` from it before every call (ADR-024).
        """
        ...

    async def generate(self, request: AiRequest) -> AiResponse:
        """Make one call and return what came back.

        Raises:
            AiTimeoutError: If the model did not answer in time.
            AiRateLimitedError: If the provider refused for rate reasons. A
                distinct error because it is the one failure that is worth
                retrying later rather than differently.
            AiProviderError: If the provider refused for any other reason.
            AiResponseError: If it answered with something unreadable.
        """
        ...


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TIMEOUT_SECONDS",
    "AiProvider",
    "AiRequest",
    "AiResponse",
]
