"""Telegram integration.

Slices 0 to 3 of ``docs/TELEGRAM_ARCHITECTURE.md``: locating and verifying the
native library, bridging its blocking receive call to asyncio, and signing an
account in.

Reading, updates and sending are later slices. The gateway declares only what
has a caller (ADR-051), so nothing here is a guess about a shape nothing uses.

``gateway``, ``mapping`` and ``errors`` are imported by module path rather than
re-exported here: they are one adapter's internals, and a caller reaching for
them is reaching past the port.
"""

from tgassist.infrastructure.telegram.client import (
    DEFAULT_QUEUE_CAPACITY,
    DEFAULT_RECEIVE_TIMEOUT,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SHUTDOWN_TIMEOUT,
    THREAD_NAME,
    ClientHealth,
    ClientState,
    TdjsonClient,
)
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
    "DEFAULT_QUEUE_CAPACITY",
    "DEFAULT_RECEIVE_TIMEOUT",
    "DEFAULT_REQUEST_TIMEOUT",
    "DEFAULT_SHUTDOWN_TIMEOUT",
    "MANIFEST_PATH",
    "REQUIRED_SYMBOLS",
    "THREAD_NAME",
    "ChecksumManifest",
    "ClientHealth",
    "ClientState",
    "CtypesLibrary",
    "LoaderSettings",
    "ManifestEntry",
    "NativeLibrary",
    "TdjsonClient",
    "TdjsonLoader",
    "detect_platform",
    "file_digest",
    "open_with_ctypes",
    "parse_version",
]
