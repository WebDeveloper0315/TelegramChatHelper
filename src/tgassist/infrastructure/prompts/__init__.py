"""Prompt loading.

Prompts are versioned assets shipped with the application, not strings in
source (ADR-008). This package reads them, validates them at startup, and holds
them immutably; the prompt *model* and the rendering rule are in the domain,
where they can be tested without a filesystem.
"""

from tgassist.infrastructure.prompts.registry import (
    DEFAULT_PROMPT_DIR,
    REGISTRY_FILE,
    REGISTRY_VERSION,
    FilePromptRegistry,
)

__all__ = [
    "DEFAULT_PROMPT_DIR",
    "REGISTRY_FILE",
    "REGISTRY_VERSION",
    "FilePromptRegistry",
]
