"""The Anthropic implementation of :class:`AiProvider`.

Speaks the Messages API and nothing else. It has no opinion about prompts,
schemas, retries or cost policy -- those are above this line, which is the whole
point of the line.

What it translates
------------------

*Out*: an :class:`~tgassist.domain.ports.ai_provider.AiRequest` becomes one
``messages`` call with the instructions as ``system`` and the content as a
single user turn. One turn rather than a conversation, because that is what
``AiRequest`` carries -- assembling a multi-turn context is a task's job and
arrives with the first task that needs one.

*In*: a response becomes an
:class:`~tgassist.domain.ports.ai_provider.AiResponse` with its text, its stop
reason and its usage. The model reported on the response is the one recorded,
not the one requested: Anthropic may serve a dated snapshot of an alias, and an
expensive call attributed to the wrong model is a cost report that cannot be
acted on.

What it never does
------------------

**It never logs a request or a response.** Both are conversation content once a
real task runs (``SECURITY.md`` section 9).

**It never retries.** One call in, one call out, one latency -- the port's
contract, and what keeps the recorded measurement honest.

**It holds the key as a `SecretValue`**, revealed once, at the moment it is put
into a header (ADR-021).
"""

from __future__ import annotations

import json
from typing import Any, Final

from tgassist.domain.errors import AiResponseError
from tgassist.domain.model.ai import AiModel, FinishReason, TokenUsage
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.ports.ai_provider import AiRequest, AiResponse
from tgassist.infrastructure.ai.transport import (
    HttpRequest,
    HttpTransport,
    UrllibTransport,
    is_success,
)

#: Where the Messages API lives, and which version of it this adapter speaks.
#: The version header is required and is what stops a provider-side change from
#: silently altering the shape this adapter parses.
DEFAULT_ENDPOINT: Final = "https://api.anthropic.com/v1/messages"
API_VERSION: Final = "2023-06-01"

#: Anthropic's stop reasons, mapped onto the domain's. Anything unlisted becomes
#: ``OTHER`` rather than being refused: a provider that adds a stop reason must
#: not make calls unrecordable.
STOP_REASONS: Final[dict[str, FinishReason]] = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "tool_use": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "refusal": FinishReason.CONTENT_FILTER,
}


class AnthropicProvider:
    """Talks to Anthropic's Messages API.

    The transport is injected, which is what makes this adapter testable without
    a network: a scripted transport returns the payloads Anthropic returns, and
    every line of translation below runs against them.
    """

    __slots__ = ("_api_key", "_endpoint", "_model", "_transport")

    def __init__(
        self,
        model: AiModel,
        api_key: SecretValue,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        transport: HttpTransport | None = None,
    ) -> None:
        """Bind to a model and the credential that reaches it.

        Args:
            model: Which model to call, and what using it implies.
            api_key: The key, wrapped so it stays masked on every incidental
                rendering path (ADR-021).
            endpoint: Where to send requests. Configurable for a proxy or a
                compatible host, not for a different vendor.
            transport: How to send them. Defaults to the standard library.
        """
        self._model = model
        self._api_key = api_key
        self._endpoint = endpoint
        self._transport = transport if transport is not None else UrllibTransport()

    @property
    def model(self) -> AiModel:
        """Return which model this provider uses, and what using it implies."""
        return self._model

    async def generate(self, request: AiRequest) -> AiResponse:
        """Make one call and return what came back.

        Raises:
            AiTimeoutError: If the model did not answer in time.
            AiRateLimitedError: If the provider is rate limiting.
            AiProviderError: If it refused for any other reason.
            AiResponseError: If it answered with something unreadable.
        """
        answer = await self._transport.send(
            HttpRequest(
                url=self._endpoint,
                headers=self._headers(),
                body=json.dumps(self._payload(request)).encode("utf-8"),
                timeout_seconds=request.timeout_seconds,
            )
        )
        if not is_success(answer.status):  # pragma: no cover - the transport raises first
            msg = f"The provider answered {answer.status}"
            raise AiResponseError(msg, user_message="The AI provider answered unexpectedly.")
        return self._translate(answer.payload)

    def _headers(self) -> dict[str, str]:
        """Build the request headers.

        The only place the key is revealed, and it is revealed straight into the
        header it is needed in.
        """
        return {
            "content-type": "application/json",
            "anthropic-version": API_VERSION,
            "x-api-key": self._api_key.reveal(),
        }

    def _payload(self, request: AiRequest) -> dict[str, Any]:
        """Build the request body.

        ``system`` and the user turn are kept apart deliberately: untrusted
        content goes in the turn, never in the instructions, which is the
        structural half of the prompt-injection defence (``SECURITY.md``
        section 12).
        """
        payload: dict[str, Any] = {
            "model": self._model.identifier,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "messages": [{"role": "user", "content": request.content}],
        }
        if request.instructions is not None:
            payload["system"] = request.instructions
        return payload

    def _translate(self, payload: dict[str, Any]) -> AiResponse:
        """Turn one Messages response into an :class:`AiResponse`.

        Raises:
            AiResponseError: If the shape is not one this version can read. The
                error names *what* was missing and never what was said.
        """
        text = _text_of(payload)
        if text is None:
            msg = "The response carried no text block"
            raise AiResponseError(
                msg,
                user_message="The AI provider answered with nothing usable.",
                context={"missing": "content.text"},
            )

        return AiResponse(
            text=text,
            finish_reason=STOP_REASONS.get(str(payload.get("stop_reason")), FinishReason.OTHER),
            usage=_usage_of(payload),
            # The model that actually answered, which may be a dated snapshot of
            # the alias that was asked for.
            model=self._answering_model(payload),
        )

    def _answering_model(self, payload: dict[str, Any]) -> AiModel:
        """Return the model the response says produced it, falling back to ours."""
        reported = payload.get("model")
        if not isinstance(reported, str) or not reported:
            return self._model
        if reported == self._model.identifier:
            return self._model
        # Same vendor, same boundary, same rates -- a snapshot of the alias that
        # was requested, recorded under the name it answered as.
        return AiModel(
            vendor=self._model.vendor,
            identifier=reported,
            data_boundary=self._model.data_boundary,
            input_cost_per_million=self._model.input_cost_per_million,
            output_cost_per_million=self._model.output_cost_per_million,
            currency=self._model.currency,
        )


def _text_of(payload: dict[str, Any]) -> str | None:
    """Return the concatenated text blocks of a response, or ``None``.

    Concatenated because the API returns a list of blocks and a long answer
    arrives as several. Blocks that are not text -- a tool use, an image -- are
    skipped rather than refused: nothing here asks for them, and one appearing
    should not make an otherwise readable answer unreadable.
    """
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return None

    parts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "".join(parts) if parts else None


def _usage_of(payload: dict[str, Any]) -> TokenUsage:
    """Return what the response says the call consumed.

    Absence is tolerated at every level. A provider that stopped reporting usage
    would make costs unknown, which is a worse report but not a failed call --
    and refusing the answer would turn a billing gap into a lost result.
    """
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_count(usage.get("input_tokens")),
        output_tokens=_count(usage.get("output_tokens")),
    )


def _count(value: Any) -> int | None:
    """Return a non-negative token count, or ``None`` when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


__all__ = [
    "API_VERSION",
    "DEFAULT_ENDPOINT",
    "STOP_REASONS",
    "AnthropicProvider",
]
