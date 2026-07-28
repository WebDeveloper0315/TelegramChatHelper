"""Domain entities and value objects.

Plain dataclasses and enums with no persistence, transport or framework
concerns. Business entities arrive with the aggregate milestones; see
``docs/DOMAIN_MODEL.md``.
"""

from tgassist.domain.model.account import Account, validate_timezone
from tgassist.domain.model.identifiers import AccountId, TelegramUserId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    PageRequest,
    SortDirection,
    SortOrder,
    TimeWindow,
)
from tgassist.domain.model.secret import SecretValue
from tgassist.domain.model.user_profile import (
    EmojiUsage,
    MessageLength,
    TimeRange,
    TonePreference,
    UserProfile,
)

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Account",
    "AccountId",
    "EmojiUsage",
    "MessageLength",
    "Page",
    "PageRequest",
    "SecretValue",
    "SortDirection",
    "SortOrder",
    "TelegramUserId",
    "TimeRange",
    "TimeWindow",
    "TonePreference",
    "UserProfile",
    "validate_timezone",
]
