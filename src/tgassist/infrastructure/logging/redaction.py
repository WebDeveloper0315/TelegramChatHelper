"""structlog processor applying the domain's sensitivity policy to log records.

Redaction is a processor in the pipeline, not a responsibility of call sites. A
single forgetful call site would defeat a call-site policy; a processor cannot
be forgotten, and it also covers records emitted by third-party packages.

The policy itself -- which keys are secret, which hold conversation content --
lives in ``tgassist.domain.services.sensitivity``. This module only applies it.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from tgassist.domain.services.sensitivity import (
    REDACTED,
    REDACTED_CONTENT,
    is_content_key,
    is_secret_key,
    mask_secret_values,
)

MAX_DEPTH = 6
"""Recursion limit. Deeply nested log payloads are a smell, not a use case."""


def redact_value(value: Any, *, allow_content: bool, depth: int = 0) -> Any:
    """Recursively redact a value that is not itself keyed as sensitive."""
    if depth >= MAX_DEPTH:
        return value
    if isinstance(value, str):
        return mask_secret_values(value)
    if isinstance(value, dict):
        return {
            key: redact_entry(key, item, allow_content=allow_content, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted = [
            redact_value(item, allow_content=allow_content, depth=depth + 1) for item in value
        ]
        return tuple(redacted) if isinstance(value, tuple) else redacted
    return value


def redact_entry(key: str, value: Any, *, allow_content: bool, depth: int = 0) -> Any:
    """Redact one key/value pair according to the sensitivity policy."""
    if is_secret_key(key):
        return REDACTED
    if is_content_key(key) and not allow_content:
        return REDACTED_CONTENT
    return redact_value(value, allow_content=allow_content, depth=depth)


def build_redaction_processor(*, allow_content: bool = False) -> Any:
    """Build a structlog processor that redacts sensitive fields.

    Args:
        allow_content: Permit conversation content through. Set only when
            diagnostic mode is active. Secrets are removed either way.

    Returns:
        A structlog processor suitable for the shared processor chain.
    """

    def processor(
        _logger: Any,
        _method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        for key in list(event_dict):
            event_dict[key] = redact_entry(key, event_dict[key], allow_content=allow_content)
        return event_dict

    return processor
