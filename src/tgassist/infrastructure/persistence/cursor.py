"""Opaque pagination cursors.

A cursor encodes where a page ended. It is base64-wrapped JSON: not a security
measure, since anyone can decode it, but enough to discourage constructing one
by hand -- which matters because a cursor's shape is coupled to the ``ORDER BY``
of the query that issued it and has no meaning anywhere else.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime
from typing import Any


def _encode_value(value: Any) -> Any:
    """Render a value JSON can carry without losing precision."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


class Cursor:
    """Encodes and decodes opaque pagination cursors.

    Base64-wrapped JSON. The encoding is not a security measure -- a caller can
    trivially decode it -- but it does discourage constructing cursors by hand,
    which matters because a cursor's shape is coupled to the ``ORDER BY`` of the
    query that issued it and has no meaning anywhere else.
    """

    __slots__ = ()

    @staticmethod
    def encode(values: dict[str, Any]) -> str:
        """Encode cursor values into an opaque token.

        Datetimes become ISO-8601 text, which is lossless and round-trips
        exactly. The generic ``str()`` fallback is not: it would render a
        datetime in a form that parses back differently, and a cursor that
        decodes to a slightly different instant skips rows silently.
        """
        payload = json.dumps(values, sort_keys=True, separators=(",", ":"), default=_encode_value)
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def decode(token: str | None) -> dict[str, Any] | None:
        """Decode a cursor token, or return ``None`` if absent or malformed.

        A malformed cursor is treated as absent rather than as an error. It
        almost always means a stale bookmark or a hand-edited URL, and starting
        from the beginning is a better answer than a stack trace.
        """
        if not token:
            return None
        try:
            padded = token + "=" * (-len(token) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        except (ValueError, TypeError):
            return None
        return decoded if isinstance(decoded, dict) else None
