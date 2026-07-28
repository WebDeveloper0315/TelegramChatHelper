"""Use cases.

One class per use case, each exposing a single ``execute`` method and defining
one transaction boundary.
"""

from tgassist.application.use_cases.account import (
    CreateAccount,
    CreateAccountRequest,
    GetAccount,
    ListAccounts,
    SetActiveAccount,
)
from tgassist.application.use_cases.user_profile import (
    GetUserProfile,
    ProfileChanges,
    UpdateUserProfile,
)

__all__ = [
    "CreateAccount",
    "CreateAccountRequest",
    "GetAccount",
    "GetUserProfile",
    "ListAccounts",
    "ProfileChanges",
    "SetActiveAccount",
    "UpdateUserProfile",
]
