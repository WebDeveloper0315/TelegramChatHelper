"""The pinned checksum manifest for ``tdjson``.

``tdjson`` is loaded into this process with ``ctypes``. It sees the session key,
every message and the network, so whatever supplies it is as trusted as the
application itself (ADR-012's risk note, ADR-047's decision). The manifest is
how that trust is made explicit: a digest recorded by a human who knows where
the file came from, committed to the repository, and reviewed like any other
security-relevant change.

The file ships beside this module rather than in the user's configuration
directory. It is a statement about which binaries this *code* trusts, not a
preference the user sets -- a manifest a user could edit freely would verify
nothing.

An empty manifest is the correct initial state. It means no binary is trusted
yet, and every load fails with instructions for recording one. That is
deliberately inconvenient: the alternative is trusting whatever happens to be on
disk.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

MANIFEST_FILENAME: Final = "tdjson_manifest.json"

MANIFEST_PATH: Final = Path(__file__).with_name(MANIFEST_FILENAME)

_DIGEST_CHUNK: Final = 1024 * 1024
"""Read in chunks: ``tdjson`` is tens of megabytes and need not be resident."""

SHA256_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One binary this application trusts.

    Attributes:
        platform: Manifest key, ``windows-amd64``.
        sha256: Lowercase hex digest of the whole file.
        version: The TDLib version the recorder asserted, or ``None`` if they
            did not. When present it is cross-checked against what the loaded
            library reports, which catches a stale entry pointing at a swapped
            file.
        source: Where the binary came from, in the recorder's own words. Free
            text, because provenance is a sentence, not an enumeration.
        recorded: ISO date the entry was added.
    """

    platform: str
    sha256: str
    version: str | None = None
    source: str = ""
    recorded: str = ""

    def as_json(self) -> dict[str, Any]:
        """Render the entry as it appears in the manifest file."""
        return {
            "platform": self.platform,
            "sha256": self.sha256,
            "version": self.version,
            "source": self.source,
            "recorded": self.recorded,
        }


class ChecksumManifest:
    """The set of binaries this application will load."""

    __slots__ = ("_entries",)

    def __init__(self, entries: tuple[ManifestEntry, ...] = ()) -> None:
        """Hold a set of trusted entries."""
        self._entries = entries

    @classmethod
    def load(cls, path: Path | None = None) -> ChecksumManifest:
        """Read the manifest from disk.

        A missing file is an empty manifest rather than an error: a checkout
        that has never recorded a binary is a normal state, and it fails at the
        point of *use* with an actionable message rather than at import.

        Raises:
            ValueError: If the file exists but is not a readable manifest. A
                malformed manifest must not degrade to an empty one -- that
                would turn a corrupted trust store into "trust nothing", which
                looks identical to a fresh checkout and hides the corruption.
        """
        source = path if path is not None else MANIFEST_PATH
        if not source.is_file():
            return cls()

        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"The tdjson manifest at {source} could not be read: {exc}"
            raise ValueError(msg) from exc

        if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
            msg = f"The tdjson manifest at {source} has no 'entries' list"
            raise ValueError(msg)

        return cls(tuple(_parse_entry(raw, source) for raw in document["entries"]))

    def entries_for(self, platform_key: str) -> tuple[ManifestEntry, ...]:
        """Return every entry recorded for one platform."""
        return tuple(entry for entry in self._entries if entry.platform == platform_key)

    def find(self, platform_key: str, digest: str) -> ManifestEntry | None:
        """Return the entry matching a platform and digest, if any.

        Both must match. A digest recorded for another platform does not make a
        binary trusted here: the same bytes cannot be a valid library for two
        architectures, so a cross-platform match means the manifest is wrong.
        """
        lowered = digest.lower()
        for entry in self.entries_for(platform_key):
            if entry.sha256.lower() == lowered:
                return entry
        return None

    def __len__(self) -> int:
        """Return how many entries the manifest holds."""
        return len(self._entries)


def _parse_entry(raw: Any, source: Path) -> ManifestEntry:
    """Build one entry, refusing anything that would weaken verification."""
    if not isinstance(raw, dict):
        msg = f"The tdjson manifest at {source} contains a non-object entry"
        raise ValueError(msg)

    platform = raw.get("platform")
    digest = raw.get("sha256")
    if not isinstance(platform, str) or not platform:
        msg = f"A tdjson manifest entry in {source} has no platform"
        raise ValueError(msg)
    if not isinstance(digest, str) or len(digest) != SHA256_LENGTH:
        msg = f"A tdjson manifest entry for {platform} in {source} has no valid SHA-256 digest"
        raise ValueError(msg)

    version = raw.get("version")
    return ManifestEntry(
        platform=platform,
        sha256=digest.lower(),
        version=version if isinstance(version, str) and version else None,
        source=str(raw.get("source") or ""),
        recorded=str(raw.get("recorded") or ""),
    )


def file_digest(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of a file.

    Raises:
        OSError: If the file cannot be read.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_PATH",
    "ChecksumManifest",
    "ManifestEntry",
    "file_digest",
]
