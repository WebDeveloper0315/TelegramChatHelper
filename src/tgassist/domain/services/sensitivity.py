"""Which values are sensitive, and how they are masked.

This is a privacy policy, not a logging detail. Deciding that ``api_key`` must
never be written down and that ``message_text`` must not be written down unless
the user has deliberately enabled diagnostics is a business rule, so it lives in
the domain where it can be read, reviewed and tested in one place.

Two consumers apply it: the logging processor, which strips these fields from
every record before it is emitted, and the command line adapter, which masks
them when displaying configuration. Both must agree, which is the second reason
the rule has a single home.

See ``docs/SECURITY.md`` section 9.
"""

from __future__ import annotations

import re

REDACTED = "[redacted]"
"""Replacement for a secret value."""

REDACTED_CONTENT = "[content redacted]"
"""Replacement for conversation content outside diagnostic mode."""

SECRET_KEY_FRAGMENTS: frozenset[str] = frozenset(
    {
        "api_hash",
        "api_key",
        "apikey",
        "auth_code",
        "authorization",
        "credential",
        "encryption_key",
        "passcode",
        "passphrase",
        "password",
        "phone",
        "private_key",
        "secret",
        "session_key",
        "token",
        "two_factor",
    }
)
"""Key fragments whose values are always removed, regardless of configuration."""

CONTENT_KEY_FRAGMENTS: frozenset[str] = frozenset(
    {
        "body",
        "caption",
        "memory_value",
        "message_text",
        "prompt",
        "reply_text",
        "response_text",
        "summary_text",
        "transcript",
    }
)
"""Key fragments holding conversation content, removed unless diagnostic mode."""

CONTENT_KEYS: frozenset[str] = frozenset({"text"})
"""Whole key names holding conversation content.

Matched exactly rather than as fragments, which is the whole reason this set
exists separately. ``Message.text`` is the most sensitive field in the
application, but ``text`` cannot join :data:`CONTENT_KEY_FRAGMENTS`: it is a
substring of ``context``, a structural key carried by every application error,
and redacting that would hide the diagnostic information errors exist to give.
"""

SAFE_KEYS: frozenset[str] = frozenset({"event", "level", "logger", "timestamp"})
"""Structural keys whose names never indicate sensitivity.

``event`` is a developer-authored event name rather than user data, and
redacting it would make logs unreadable. Values under these keys are still
scanned for secret-shaped substrings, because a formatted message can carry a
credential the key name gives no hint of.
"""

REFERENCE_KEY_SUFFIXES: tuple[str, ...] = ("_ref", "_name", "_backend", "_store")
"""Suffixes marking a key that names a secret rather than holding one.

Configuration stores secret *names*; the values live in the credential store
(ADR-021). Masking ``api_key_ref`` or ``require_secret_store`` would hide
information the user needs while protecting nothing.
"""

_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Known provider key shapes. A narrow, low-false-positive net: key-name
    # matching is the primary defence and this is the backstop for a value that
    # reaches a log under an innocuous key.
    #
    # These are deliberately unanchored. A secret frequently arrives embedded in
    # a larger string -- a formatted message from a third-party library, or an
    # interpolated URL -- and an anchored pattern would miss every such case.
    re.compile(r"(?<![A-Za-z0-9_\-])sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_\-])sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_\-])ghp_[A-Za-z0-9]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_\-])AIza[A-Za-z0-9_\-]{20,}"),
    # Telegram bot token: digits, colon, long opaque string.
    re.compile(r"(?<![A-Za-z0-9_\-])\d{6,}:[A-Za-z0-9_\-]{30,}"),
)


def is_secret_key(key: str) -> bool:
    """Report whether a key name indicates that its value is a secret."""
    lowered = key.lower()
    if lowered in SAFE_KEYS or lowered.endswith(REFERENCE_KEY_SUFFIXES):
        return False
    return any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS)


def is_content_key(key: str) -> bool:
    """Report whether a key name indicates conversation content."""
    lowered = key.lower()
    if lowered in SAFE_KEYS or lowered.endswith(REFERENCE_KEY_SUFFIXES):
        return False
    if lowered in CONTENT_KEYS:
        return True
    return any(fragment in lowered for fragment in CONTENT_KEY_FRAGMENTS)


def looks_like_secret_value(value: str) -> bool:
    """Report whether a string contains something matching a known secret shape."""
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def mask_secret_values(value: str) -> str:
    """Replace any secret-shaped substring, preserving the rest of the text.

    Substituting only the matched span rather than discarding the whole string
    keeps a formatted message from a third-party library readable while still
    removing the credential embedded in it.
    """
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def is_sensitive_key(key: str) -> bool:
    """Report whether a key must be masked when displayed to the user."""
    return is_secret_key(key) or is_content_key(key)
