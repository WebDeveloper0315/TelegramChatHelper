"""Domain entities and value objects.

Plain dataclasses and enums with no persistence, transport or framework
concerns. Entities arrive with Milestone 1; see ``docs/DOMAIN_MODEL.md``.
"""

from tgassist.domain.model.page import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, clamp_page_size
from tgassist.domain.model.secret import SecretValue

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Page",
    "SecretValue",
    "clamp_page_size",
]
