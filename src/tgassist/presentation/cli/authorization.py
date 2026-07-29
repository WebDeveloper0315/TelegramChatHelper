"""A console implementation of :class:`AuthorizationHandler`.

The one place in the application where a Telegram credential is typed. It reads
each value, returns it, and keeps no reference — there is no attribute here that
a code or a password could survive in, which is the structural version of
"never retained".

Passwords are read with :func:`getpass.getpass` so they do not appear on screen
or in the terminal's scrollback. Codes are not: a code is short-lived, useless
once submitted, and a user who cannot see what they typed will mistype it.
"""

from __future__ import annotations

import getpass

import typer

from tgassist.domain.model.session import AuthorizationState
from tgassist.domain.model.telegram import CodeHint, PasswordHint
from tgassist.domain.ports.telegram_gateway import RetryDecision

#: How many times a rejected credential may be retried before giving up.
#: Telegram invalidates a code after a few wrong attempts, so an unbounded
#: prompt would loop the user into a lockout rather than towards a login.
MAX_ATTEMPTS = 3

#: What each state means for the person waiting at the prompt.
_ANNOUNCEMENTS = {
    AuthorizationState.WAITING_PHONE: "Telegram needs your phone number.",
    AuthorizationState.WAITING_CODE: "Telegram has sent a login code.",
    AuthorizationState.WAITING_PASSWORD: "This account has two-step verification enabled.",
    AuthorizationState.LOGGED_OUT: "This account has been signed out.",
}


class ConsoleAuthorizationHandler:
    """Collects login credentials from whoever is at the terminal.

    Attributes:
        attempts: How many rejections have been reported so far. The only state
            this class keeps, and deliberately the only thing worth keeping.
    """

    __slots__ = ("_max_attempts", "attempts")

    def __init__(self, *, max_attempts: int = MAX_ATTEMPTS) -> None:
        """Build a handler that gives up after ``max_attempts`` rejections."""
        self.attempts = 0
        self._max_attempts = max_attempts

    async def request_phone_number(self) -> str:
        """Prompt for the phone number, in international format."""
        return str(typer.prompt("Phone number (international format, e.g. +441234567890)"))

    async def request_code(self, hint: CodeHint) -> str:
        """Prompt for the login code, saying where it was sent."""
        where = _describe_delivery(hint)
        digits = f", {hint.length} digits" if hint.length else ""
        return str(typer.prompt(f"Login code{where}{digits}"))

    async def request_password(self, hint: PasswordHint) -> str:
        """Prompt for the two-step password, without echoing it."""
        if hint.hint:
            typer.echo(f"  Password hint: {hint.hint}")
        if hint.has_recovery_email and hint.recovery_email_pattern:
            typer.echo(f"  Recovery email: {hint.recovery_email_pattern}")
        # Not typer.prompt(hide_input=True): getpass reads from the terminal
        # directly, so the value never reaches the shell's history or a pipe.
        return getpass.getpass("Password: ")

    async def on_state_change(self, state: AuthorizationState) -> None:
        """Say what Telegram is waiting for. Never blocks."""
        announcement = _ANNOUNCEMENTS.get(state)
        if announcement is not None:
            typer.echo(announcement)

    async def on_error(self, error: Exception) -> RetryDecision:
        """Report a rejection and decide whether to ask again.

        The rejected value is not shown, not echoed and not stored — only
        Telegram's reason for refusing it.
        """
        self.attempts += 1
        message = getattr(error, "user_message", None) or str(error)
        typer.echo(f"  {message}", err=True)

        if self.attempts >= self._max_attempts:
            typer.echo("  Too many attempts. Run the command again to retry.", err=True)
            return RetryDecision.ABORT
        return RetryDecision.RETRY


def _describe_delivery(hint: CodeHint) -> str:
    """Return a short phrase naming where the code was sent."""
    delivery = hint.delivery.strip().lower()
    if delivery in {"", "other"}:
        return ""
    if delivery == "sms":
        return " (sent by SMS)"
    if delivery == "call":
        return " (dictated by phone call)"
    if delivery == "telegram message":
        return " (sent to another Telegram client)"
    return f" (sent by {delivery})"


__all__ = [
    "MAX_ATTEMPTS",
    "ConsoleAuthorizationHandler",
]
