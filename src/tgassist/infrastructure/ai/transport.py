"""The seam between a provider adapter and the network.

One protocol, one real implementation, and the reason both exist: a provider
adapter has two jobs -- speaking a vendor's JSON, and getting bytes to a host --
and only the first is worth testing. Separating them means the Anthropic adapter
can be exercised against a scripted transport that never opens a socket, which
is what "a real provider adapter, without network" means.

Why the standard library
------------------------

``urllib.request`` rather than a client library. The requirement here is one
POST with three headers and a JSON body; an HTTP library would be a dependency
added for convenience the size of the thing it replaces, and `CLAUDE.md` asks
for large dependencies to be explained before they are introduced rather than
after.

It is a blocking call, so it runs on a thread -- the same arrangement the
database and the TDLib receive loop already use, which is why it needs no new
concurrency concept.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

from tgassist.domain.errors import AiProviderError, AiRateLimitedError, AiTimeoutError

#: HTTP statuses this application distinguishes. Anything else is a provider
#: error with its status in the context.
_TOO_MANY_REQUESTS: Final = 429
_OK_FLOOR: Final = 200
_OK_CEILING: Final = 300


@dataclass(frozen=True, slots=True)
class HttpRequest:
    """One request to send.

    Attributes:
        url: Where to send it.
        headers: What to send with it. **Never logged**: one of these carries an
            API key.
        body: The JSON payload, already encoded.
        timeout_seconds: How long to wait.
    """

    url: str
    headers: dict[str, str]
    body: bytes
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """What came back.

    Attributes:
        status: The HTTP status.
        payload: The decoded JSON body, or an empty mapping when the body was
            not JSON. Deciding what a missing field means belongs to the
            adapter, not here.
    """

    status: int
    payload: dict[str, Any]


@runtime_checkable
class HttpTransport(Protocol):
    """Sends one request and returns what came back.

    Contract:

    1. It raises the AI error taxonomy, never a transport exception. A caller
       that had to know which HTTP library was underneath would be coupled to
       the thing the provider port exists to hide.
    2. It never logs a request, a response or a header.
    3. Cancellation propagates.
    """

    async def send(self, request: HttpRequest) -> HttpResponse:
        """Send a request.

        Raises:
            AiTimeoutError: If the host did not answer in time.
            AiRateLimitedError: If it answered 429.
            AiProviderError: If it could not be reached, or answered with
                anything else outside the 2xx range.
        """
        ...


class UrllibTransport:
    """An :class:`HttpTransport` over the standard library.

    Blocking work on a worker thread, so the event loop is never held by a
    network wait.
    """

    __slots__ = ()

    async def send(self, request: HttpRequest) -> HttpResponse:
        """Send a request on a thread, and normalise what comes back."""
        return await asyncio.to_thread(self._send, request)

    @staticmethod
    def _send(request: HttpRequest) -> HttpResponse:
        """Perform one blocking POST.

        Every failure mode is translated here, so nothing above this line has to
        know what ``urllib`` raises.
        """
        prepared = urllib.request.Request(  # noqa: S310 - the URL is configuration, not input
            request.url,
            data=request.body,
            headers=request.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - as above
                prepared, timeout=request.timeout_seconds
            ) as answer:
                return HttpResponse(status=answer.status, payload=_decode(answer.read()))
        except urllib.error.HTTPError as exc:
            # The host answered, and the answer was an error. The body often
            # explains why, so it is decoded -- and only its *type* and status
            # ever leave this function.
            payload = _decode(exc.read())
            if exc.code == _TOO_MANY_REQUESTS:
                msg = "The provider is rate limiting this client"
                raise AiRateLimitedError(
                    msg,
                    user_message="The AI provider is busy. Try again shortly.",
                    context={"status": exc.code},
                ) from exc
            msg = f"The provider answered {exc.code}"
            raise AiProviderError(
                msg,
                user_message="The AI provider refused the request.",
                context={"status": exc.code, "error_type": _error_type(payload)},
            ) from exc
        except TimeoutError as exc:
            msg = f"The provider did not answer within {request.timeout_seconds}s"
            raise AiTimeoutError(
                msg, user_message="The AI provider took too long to answer."
            ) from exc
        except urllib.error.URLError as exc:
            msg = "The provider could not be reached"
            raise AiProviderError(
                msg,
                user_message="The AI provider could not be reached.",
                context={"reason": type(exc.reason).__name__},
            ) from exc


def _decode(body: bytes) -> dict[str, Any]:
    """Return a JSON body as a mapping, or an empty one.

    A body that is not JSON is not an error here: what a missing field means is
    the adapter's decision, and failing at this level would report "malformed
    JSON" for a provider that answered with plain text.
    """
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _error_type(payload: dict[str, Any]) -> str | None:
    """Return the provider's own name for an error, if it gave one.

    The *type*, never the message: a provider's error message can quote the
    request, and the request is conversation content.
    """
    error = payload.get("error")
    if isinstance(error, dict):
        kind = error.get("type")
        return str(kind) if kind else None
    return None


def is_success(status: int) -> bool:
    """Whether a status means the request was answered."""
    return _OK_FLOOR <= status < _OK_CEILING


__all__ = [
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "UrllibTransport",
    "is_success",
]
