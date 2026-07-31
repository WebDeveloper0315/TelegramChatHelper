"""AI provider adapters.

One module per vendor, plus the transport seam they share. Each adapter
implements :class:`~tgassist.domain.ports.ai_provider.AiProvider` and speaks
exactly one vendor's wire format; everything else about running an AI task --
the privacy gate, the timeout, the cost accounting, the audit record -- is above
this layer and is the same whichever adapter answers (ADR-057).
"""

from tgassist.infrastructure.ai.anthropic import AnthropicProvider
from tgassist.infrastructure.ai.scripted import (
    CLOUD_MODEL,
    LOCAL_MODEL,
    ScriptedAiProvider,
    token_count,
)
from tgassist.infrastructure.ai.transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    UrllibTransport,
)

__all__ = [
    "CLOUD_MODEL",
    "LOCAL_MODEL",
    "AnthropicProvider",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "ScriptedAiProvider",
    "UrllibTransport",
    "token_count",
]
