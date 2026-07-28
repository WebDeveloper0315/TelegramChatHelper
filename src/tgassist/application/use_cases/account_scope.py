"""Resolving which account a use case operates on.

Every account-scoped use case begins the same way: take an optional account
identifier, fall back to the active account, and report clearly when neither
resolves. The rule is one line to state and easy to get subtly wrong -- treating
"no active account" as an empty result, or looking up an account and then not
checking it exists -- so it lives here once rather than in each use case.

This is a function, not a service. It has no state, no configuration and one
caller shape; wrapping it in a class would add a constructor argument to every
use case that needs it and explain nothing.
"""

from __future__ import annotations

from tgassist.domain.errors import RecordNotFoundError
from tgassist.domain.model.identifiers import AccountId
from tgassist.domain.ports.account_repository import AccountRepository


async def resolve_account(accounts: AccountRepository, account_id: AccountId | None) -> AccountId:
    """Return the account to operate on.

    Args:
        accounts: Repository to resolve through, already enlisted in the
            caller's transaction.
        account_id: Account to use. ``None`` selects the active account, which
            is what a caller usually wants.

    Returns:
        The identifier of an account that exists.

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
        return active.id

    account = await accounts.get(account_id)
    if account is None:
        msg = f"No account with identifier {int(account_id)}"
        raise RecordNotFoundError(
            msg,
            user_message="That account was not found.",
            context={"account_id": int(account_id)},
        )
    return account.id


__all__ = ["resolve_account"]
