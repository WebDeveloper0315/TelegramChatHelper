"""A secret value that resists accidental disclosure.

The logging pipeline already strips fields whose *name* looks sensitive and
values whose *shape* looks like a known credential. Neither catches a secret
passed under an innocuous name in an unrecognised format::

    logger.info("configured", value=api_key)  # name innocuous, shape unknown

Wrapping the value closes that gap at the type level: the wrapper's ``repr`` and
``str`` are masked, so it stays masked in log records, exception tracebacks,
debugger output, f-strings and assertion messages. Reading the real value
requires calling :meth:`SecretValue.reveal`, which is greppable and reviewable.
"""

from __future__ import annotations

import hmac
from typing import Any, final

MASK = "********"


@final
class SecretValue:
    """A string whose value is hidden from every incidental rendering path.

    This is a safety net, not an encryption boundary. The value is plain text in
    process memory; anything that can read process memory can read it. What the
    wrapper prevents is the far more likely accident of a credential reaching a
    log file, a crash report or a screenshot.
    """

    __slots__ = ("_value",)

    def __init__(self, value: object) -> None:
        """Wrap a secret string.

        The parameter is typed ``object`` rather than ``str`` deliberately. This
        is a trust boundary: secrets arrive from environment variables,
        configuration files and third-party credential backends, none of which
        the type checker can vouch for. Declaring ``str`` would make the runtime
        guard below statically unreachable, and the guard is the more valuable
        of the two here -- wrapping ``None`` would otherwise produce a
        ``SecretValue`` whose ``reveal()`` returns ``None``, failing somewhere
        far from the cause.

        Args:
            value: The secret. Must be a string; "absent" and "present but
                empty" are different states that callers must distinguish, so
                ``None`` is rejected rather than coerced.

        Raises:
            TypeError: If ``value`` is not a string.
        """
        if not isinstance(value, str):
            # Defensive despite the annotation. Secrets arrive from
            # configuration files, environment variables and third-party
            # backends -- none of which the type checker sees -- and wrapping
            # ``None`` would produce a SecretValue whose reveal() returns None,
            # failing somewhere far from the cause.
            msg = f"SecretValue requires a string, got {type(value).__name__}"
            raise TypeError(msg)
        self._value: str = value

    def reveal(self) -> str:
        """Return the underlying string.

        The deliberately conspicuous name makes every disclosure point easy to
        find in review and in a code search.
        """
        return self._value

    def __repr__(self) -> str:
        """Return a masked representation."""
        return f"SecretValue({MASK})"

    def __str__(self) -> str:
        """Return a masked representation."""
        return MASK

    def __format__(self, format_spec: str) -> str:
        """Return a masked representation, ignoring any format specification."""
        del format_spec
        return MASK

    def __eq__(self, other: object) -> bool:
        """Compare in constant time, to avoid leaking content through timing."""
        if isinstance(other, SecretValue):
            return hmac.compare_digest(self._value, other._value)
        return NotImplemented

    def __hash__(self) -> int:
        """Hash the wrapper so it can be used in sets and as a mapping key."""
        return hash(self._value)

    def __len__(self) -> int:
        """Return the length of the secret.

        Length is not itself sensitive and is useful for validation, for example
        rejecting an empty API key before a request is attempted.
        """
        return len(self._value)

    def __bool__(self) -> bool:
        """Report whether the secret is non-empty."""
        return bool(self._value)

    def __reduce__(self) -> Any:
        """Refuse pickling.

        Serialising a secret is almost always an accident -- a cached object, a
        multiprocessing argument, a persisted session. Failing loudly is better
        than silently writing the value somewhere durable.
        """
        msg = "SecretValue cannot be serialised; call reveal() explicitly if disclosure is intended"
        raise TypeError(msg)
