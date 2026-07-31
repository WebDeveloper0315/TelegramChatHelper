"""A deterministic AI provider that answers from a script.

The second implementation of :class:`AiProvider`, and the reason the port
exists. It answers from a script, accounts for tokens, and fails the way a
provider fails -- so a test that passes against it is evidence about the real
flow rather than about a recorded call.

**Shipped, not confined to the test tree.** ``ai.vendor: fake`` is a legitimate
configuration: it gives a fresh installation a working AI boundary that reaches
no network and costs nothing, and it is what a developer runs against while
building a task. Keeping it here rather than in ``tests/`` is what makes the
tested path and the shipped path the same path -- and it is what stops the
composition root having to import the test tree to satisfy a configuration
value (ADR-057).

**Nothing here is random.** Not the answers, not the token counts, not the
latency. An AI boundary tested against a random double would have tests that
pass or fail depending on the run, which is the opposite of what this slice is
for. Token counts are a deterministic function of the text; latency is a number
the caller names.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from decimal import Decimal

from tgassist.domain.model.ai import (
    AiModel,
    AiVendor,
    DataBoundary,
    FinishReason,
    TokenUsage,
)
from tgassist.domain.ports.ai_provider import AiProvider, AiRequest, AiResponse
from tgassist.domain.services.memory_selection import estimate_tokens

#: The model a fake reports unless a test says otherwise. Local, so the privacy
#: gate lets it run against any chat -- a test about something else should not
#: have to think about permissions.
LOCAL_MODEL = AiModel(
    vendor=AiVendor.FAKE,
    identifier="fake-local-1",
    data_boundary=DataBoundary.LOCAL,
)

#: A priced, external model, for the tests that are about the gate or about
#: cost. The rates are round numbers so an expected cost can be read rather than
#: computed: a thousand input tokens cost exactly 0.003.
CLOUD_MODEL = AiModel(
    vendor=AiVendor.FAKE,
    identifier="fake-cloud-1",
    data_boundary=DataBoundary.EXTERNAL,
    input_cost_per_million=Decimal(3),
    output_cost_per_million=Decimal(15),
)


def token_count(text: str) -> int:
    """Return a deterministic token count for a string.

    Delegates to the domain's estimate, so this fake's accounting and the budget
    the memory selector enforces cannot drift apart -- two rules of thumb that
    disagree would make a context that fits the budget cost more than the fake
    charged for it.
    """
    return estimate_tokens(text)


class ScriptedAiProvider(AiProvider):
    """An AI provider that answers from a script.

    Attributes:
        requests: Every request it was given, in order. A test asserts on what
            was *sent* -- that untrusted content went in the content and not the
            instructions, for instance -- which a mock's call record would show
            less clearly.
        calls: How many times :meth:`generate` was entered, including the ones
            that failed.
    """

    __slots__ = (
        "_answers",
        "_default",
        "_failures",
        "_latency",
        "_model",
        "calls",
        "requests",
    )

    def __init__(
        self,
        *,
        model: AiModel = LOCAL_MODEL,
        answer: str = "scripted answer",
        latency_seconds: float = 0.0,
    ) -> None:
        """Build a provider that answers with one thing.

        Args:
            model: Which model to report. ``CLOUD_MODEL`` for a test about the
                privacy gate or about cost.
            answer: What to say when no answer has been scripted.
            latency_seconds: How long to take. Deterministic: an
                ``asyncio.sleep`` of exactly this, which a test that wants a
                timeout sets longer than the timeout it passes.
        """
        self._model = model
        self._default = answer
        self._latency = latency_seconds
        self._answers: deque[str | tuple[str, FinishReason]] = deque()
        self._failures: deque[Exception | None] = deque()
        self.requests: list[AiRequest] = []
        self.calls = 0

    @property
    def model(self) -> AiModel:
        """Return which model this provider reports."""
        return self._model

    async def generate(self, request: AiRequest) -> AiResponse:
        """Answer from the script, or fail from it.

        Failures are consumed in order and take precedence over answers, so a
        test can describe "fails once, then works" without a flag.
        """
        self.calls += 1
        self.requests.append(request)

        if self._latency:
            await asyncio.sleep(self._latency)

        if self._failures:
            failure = self._failures.popleft()
            if failure is not None:
                raise failure

        text, reason = self._next_answer()
        return AiResponse(
            text=text,
            finish_reason=reason,
            usage=TokenUsage(
                input_tokens=token_count(_sent(request)),
                output_tokens=token_count(text),
            ),
            model=self._model,
        )

    # -- Scripting ---------------------------------------------------------

    def script_answers(self, *answers: str | tuple[str, FinishReason]) -> None:
        """Queue what the model will say, in order.

        A bare string finishes with ``STOP``; a pair says why it stopped, which
        is how a test describes a truncated answer.
        """
        self._answers.extend(answers)

    def script_json(self, *payloads: object) -> None:
        """Queue structured answers, in order.

        Serialised with sorted keys and no spare whitespace, so the same payload
        always produces the same text -- which is what makes a digest comparison
        across two runs mean something (ADR-057).

        For the answers a structured task has to survive -- malformed JSON, a
        missing field, a confidence out of range -- use :meth:`script_answers`
        with the exact text. A helper that could only produce valid JSON could
        not describe the failures worth testing.
        """
        self._answers.extend(json.dumps(payload, sort_keys=True) for payload in payloads)

    def script_failures(self, *failures: Exception | None) -> None:
        """Queue how the model will fail, in order.

        ``None`` is a call that succeeds, so ``script_failures(error, None)``
        reads as "fail, then work" without needing a second method.
        """
        self._failures.extend(failures)

    def _next_answer(self) -> tuple[str, FinishReason]:
        """Return the next scripted answer, or the default."""
        if not self._answers:
            return self._default, FinishReason.STOP
        answer = self._answers.popleft()
        if isinstance(answer, tuple):
            return answer
        return answer, FinishReason.STOP


def _sent(request: AiRequest) -> str:
    """Return everything the request would put on the wire.

    Instructions and content together, because both are billed. Counting only
    the content would make every cost assertion quietly wrong in the direction
    that flatters the system.
    """
    return f"{request.instructions or ''}{request.content}"
