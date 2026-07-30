"""Resolving which account a use case operates on.

Every account-scoped use case begins the same way: take an optional account
identifier, fall back to the active account, and report clearly when neither
resolves. The rule is one line to state and easy to get subtly wrong -- treating
"no active account" as an empty result, or looking up an account and then not
checking it exists -- so it lives here once rather than in each use case.

These are functions, not a service. They have no state, no configuration and one
caller shape; wrapping them in a class would add a constructor argument to every
use case that needs one and explain nothing.

:func:`require_gateway_account` lives here for the same reason: it answers the
same question from the other direction -- "is this collaborator scoped to the
account we resolved" -- and two copies of an ownership rule is one copy too
many.
"""

from __future__ import annotations

from tgassist.domain.errors import AuthorizationError, RecordNotFoundError
from tgassist.domain.model.account import Account
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.telegram_gateway import TelegramGateway


async def require_account(accounts: AccountRepository, account_id: AccountId | None) -> Account:
    """Return the account to operate on, whole.

    Most use cases need only the identifier and use :func:`resolve_account`.
    This exists for the ones that need something the Account itself knows --
    synchronisation asks it who the operator is (ADR-052) -- and it is the same
    lookup either way, so asking for the entity costs no extra query.

    Args:
        accounts: Repository to resolve through, already enlisted in the
            caller's transaction.
        account_id: Account to use. ``None`` selects the active account, which
            is what a caller usually wants.

    Raises:
        RecordNotFoundError: If no account matches, or if none is active.
            Account-owned data cannot exist without an account to own it, so
            this is reported rather than silently returning nothing.
    """
    if account_id is None:
        active = await accounts.get_active()
        if active is None:
            msg = "No account is active"
            raise RecordNotFoundError(
                msg,
                user_message="No account is active. Create one first.",
            )
        return active

    account = await accounts.get(account_id)
    if account is None:
        msg = f"No account with identifier {int(account_id)}"
        raise RecordNotFoundError(
            msg,
            user_message="That account was not found.",
            context={"account_id": int(account_id)},
        )
    return account


async def resolve_account(accounts: AccountRepository, account_id: AccountId | None) -> AccountId:
    """Return the identifier of the account to operate on.

    Raises:
        RecordNotFoundError: If no account matches, or if none is active.
    """
    return (await require_account(accounts, account_id)).id


def require_gateway_account(gateway: TelegramGateway, account_id: AccountId) -> None:
    """Refuse a gateway bound to a different account.

    A gateway is bound to one account at construction (ADR-039), so this is a
    wiring mistake rather than a user one -- but it is the mistake that would
    write one person's chats under another person's account, so it is checked
    at every point where a gateway and an account meet rather than trusted.

    Raises:
        AuthorizationError: If the gateway belongs to another account.
    """
    if gateway.account_id == account_id:
        return

    msg = f"Gateway is bound to account {int(gateway.account_id)}, not {int(account_id)}"
    raise AuthorizationError(
        msg,
        user_message="That connection belongs to a different account.",
        context={"gateway": int(gateway.account_id), "account": int(account_id)},
    )


__all__ = ["require_account", "require_gateway_account", "resolve_account"]
