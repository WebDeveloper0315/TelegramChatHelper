"""Translation from TDLib error frames to the domain error taxonomy.

TDLib answers a refused request with ``{"@type": "error", "code": n,
"message": "..."}``. The message is a short constant like
``PHONE_CODE_INVALID`` — stable enough to switch on, and the only thing that
distinguishes "try again" from "this session is dead".

Implements the taxonomy in ``TELEGRAM_ARCHITECTURE.md`` section 10.1. Errors
this slice has no consumer for are absent: a mapping to an error nothing catches
is a guess about a handler that does not exist.

**No mapped error carries the value that was rejected.** An error object is
exactly the thing that ends up in a log or a crash report.
"""

from __future__ import annotations

from typing import Any, Final

from tgassist.domain.errors import (
    AuthorizationError,
    SessionRevokedError,
    TdlibRequestFailedError,
    TelegramError,
)

#: TDLib messages meaning a credential was wrong and another attempt is sensible.
AUTHORIZATION_FAILURES: Final[dict[str, str]] = {
    "PHONE_CODE_INVALID": "That code is not correct.",
    "PHONE_CODE_EXPIRED": "That code has expired. Request a new one.",
    "PHONE_CODE_EMPTY": "No code was submitted.",
    "PHONE_NUMBER_INVALID": "That phone number is not valid.",
    "PASSWORD_HASH_INVALID": "That password is not correct.",
    "PASSWORD_EMPTY": "No password was submitted.",
}

#: TDLib messages meaning this session is finished and no retry will help.
REVOCATIONS: Final[frozenset[str]] = frozenset(
    {"AUTH_KEY_UNREGISTERED", "SESSION_REVOKED", "SESSION_EXPIRED", "USER_DEACTIVATED"}
)

#: TDLib messages meaning the application's own credentials are wrong.
#:
#: Not an authorization failure: no code the user types will fix an api_id that
#: Telegram does not recognise, and telling them to retype it would waste their
#: time on the wrong problem.
CONFIGURATION_FAILURES: Final[frozenset[str]] = frozenset(
    {"API_ID_INVALID", "API_ID_PUBLISHED_FLOOD", "ACCESS_TOKEN_INVALID"}
)


def error_message(frame: dict[str, Any]) -> str:
    """Return a TDLib error frame's message, or a placeholder."""
    message = frame.get("message")
    return message if isinstance(message, str) and message else "unspecified error"


def error_code(frame: dict[str, Any]) -> int:
    """Return a TDLib error frame's numeric code, or ``0``."""
    code = frame.get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else 0


def translate(frame: dict[str, Any], *, operation: str) -> TelegramError:
    """Return the domain error for a TDLib error frame.

    Args:
        frame: The ``error`` frame TDLib returned.
        operation: What was being attempted, for the context. A safe label such
            as ``"check_code"`` — never the value that was submitted.

    Returns:
        The most specific error the message identifies, falling back to
        :class:`TelegramError`. An unrecognised message is still reported with
        TDLib's own code and text, because a caller that cannot see them cannot
        act on them.
    """
    message = error_message(frame)
    code = error_code(frame)
    context: dict[str, Any] = {
        "operation": operation,
        "telegram_code": code,
        "telegram_message": message,
    }
    detail = f"Telegram refused {operation}: {message} (code {code})"

    if message in REVOCATIONS:
        return SessionRevokedError(
            detail,
            user_message=("This Telegram session is no longer valid. Sign in again to continue."),
            context=context,
        )

    if message in CONFIGURATION_FAILURES:
        return TelegramError(
            detail,
            user_message=(
                "Telegram rejected this application's credentials. Check "
                "telegram.api_id and the api_hash in the credential store."
            ),
            context=context,
        )

    # A prefix match, because Telegram appends detail to some of these, such as
    # PHONE_CODE_INVALID variants carrying a suffix.
    for known, explanation in AUTHORIZATION_FAILURES.items():
        if message.startswith(known):
            return AuthorizationError(detail, user_message=explanation, context=context)

    return TelegramError(
        detail,
        user_message=f"Telegram refused the request: {message}",
        context=context,
    )


def translate_failure(error: TdlibRequestFailedError, *, operation: str) -> TelegramError:
    """Return the domain error for a refused request the client already raised.

    ``TdjsonClient.request`` raises rather than returning an error frame, so the
    frame is reconstructed from the context it carried. Reconstruction rather
    than a second code path: there is one translation table, and a second entry
    point into it would eventually disagree with the first.
    """
    context = error.context or {}
    frame = {"code": context.get("code", 0), "message": context.get("message", "")}
    return translate(frame, operation=operation)


def is_flood_wait(frame: dict[str, Any]) -> int | None:
    """Return the seconds Telegram asked us to wait, or ``None``.

    Recognised here but **not** acted on: absorbing flood waits belongs with the
    code that issues enough requests to cause one, which is the sync engine.
    Detecting it now means the login path can report *why* it stalled rather
    than showing a bare error code.
    """
    message = error_message(frame)
    prefix = "FLOOD_WAIT_"
    if not message.startswith(prefix):
        return None
    remainder = message[len(prefix) :]
    return int(remainder) if remainder.isdigit() else None


__all__ = [
    "AUTHORIZATION_FAILURES",
    "CONFIGURATION_FAILURES",
    "REVOCATIONS",
    "error_code",
    "error_message",
    "is_flood_wait",
    "translate",
    "translate_failure",
]
