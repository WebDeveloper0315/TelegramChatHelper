"""Telegram integration.

Slice 0 of ``docs/TELEGRAM_ARCHITECTURE.md``: locating, verifying and loading
the native library. No client, no connection, no authentication -- those are
later slices, and building them here would be building authentication's
foundation while claiming not to.
"""

from tgassist.infrastructure.telegram.loader import (
    DEFAULT_MINIMUM_VERSION,
    REQUIRED_SYMBOLS,
    CtypesLibrary,
    LoaderSettings,
    NativeLibrary,
    TdjsonLoader,
    detect_platform,
    open_with_ctypes,
    parse_version,
)
from tgassist.infrastructure.telegram.manifest import (
    MANIFEST_PATH,
    ChecksumManifest,
    ManifestEntry,
    file_digest,
)

__all__ = [
    "DEFAULT_MINIMUM_VERSION",
    "MANIFEST_PATH",
    "REQUIRED_SYMBOLS",
    "ChecksumManifest",
    "CtypesLibrary",
    "LoaderSettings",
    "ManifestEntry",
    "NativeLibrary",
    "TdjsonLoader",
    "detect_platform",
    "file_digest",
    "open_with_ctypes",
    "parse_version",
]
