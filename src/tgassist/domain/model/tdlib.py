"""Value objects describing the TDLib runtime.

These live in the domain because the presentation layer must be able to report
on the runtime without importing infrastructure (ADR-011), and the loader that
produces them is infrastructure. They are pure data: no ``ctypes``, no paths to
libraries being *held open*, nothing that could not be written down on paper.

The same arrangement ``domain/ports/database.py`` uses for its health report, and
for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class LibrarySource(StrEnum):
    """Where a candidate library came from.

    Recorded because provenance is the whole point of the verification: a
    checksum tells you a file has not changed, and this tells you what the file
    was in the first place.
    """

    CONFIGURED = "configured"
    """Named by ``telegram.tdjson_path``, or the environment variable for it."""

    VENDORED = "vendored"
    """Found under the application's own data directory."""

    SYSTEM = "system"
    """Resolved by the platform's library loader."""


class Architecture(StrEnum):
    """The machine a binary was built for.

    A library must match the *interpreter*, not the machine: a 32-bit Python on
    a 64-bit host needs a 32-bit library.
    """

    X86 = "x86"
    AMD64 = "amd64"
    ARM = "arm"
    ARM64 = "arm64"
    UNKNOWN = "unknown"


class BinaryFormat(StrEnum):
    """The container format of a library file."""

    PE = "pe"
    ELF = "elf"
    UNKNOWN = "unknown"


class DependencyVerdict(StrEnum):
    """What classifying a library's runtime dependencies concluded."""

    ACCEPTABLE = "acceptable"
    """Every import is a system library, or a noted redistributable."""

    FORBIDDEN = "forbidden"
    """At least one import defeats the checksum's meaning."""

    UNRECOGNISED = "unrecognised"
    """At least one import is not a library this check knows to be safe."""

    NOT_CHECKED = "not_checked"
    """Imports could not be read for this format. Not a pass."""


@dataclass(frozen=True, slots=True)
class DependencyReport:
    """How a library's runtime dependencies were classified.

    The manifest checksums one file; whatever that file loads at runtime is
    inside the trust boundary and unverified (ADR-047). This records what was
    found there.
    """

    verdict: DependencyVerdict
    system: tuple[str, ...] = ()
    redistributable: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    unrecognised: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_acceptable(self) -> bool:
        """Whether the dependencies permit trusting the checksum."""
        return self.verdict is DependencyVerdict.ACCEPTABLE

    @property
    def needs_redistributable(self) -> bool:
        """Whether the target machine needs the Visual C++ runtime installed."""
        return bool(self.redistributable)


class VerificationOutcome(StrEnum):
    """What checksum verification concluded about a candidate."""

    VERIFIED = "verified"
    """Its digest matches a manifest entry for this platform."""

    UNKNOWN_DIGEST = "unknown_digest"
    """The file exists and was read, but no manifest entry matches its digest."""

    UNREADABLE = "unreadable"
    """The file could not be read to compute a digest."""


@dataclass(frozen=True, slots=True)
class LibraryCandidate:
    """One place a library was looked for, and what was found there.

    Every candidate considered is reported, including the ones that were absent.
    A diagnostic that shows only the failure tells the user what went wrong; one
    that shows the whole search tells them where to put the file.
    """

    source: LibrarySource
    path: Path | None
    exists: bool
    detail: str
    """Why this candidate was rejected, or how it was accepted. Human-readable."""

    @property
    def is_usable(self) -> bool:
        """Whether this candidate is a file that could be verified."""
        return self.exists and self.path is not None


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    """The platform a library must match.

    Held as plain strings rather than an enumeration: the set of platforms is
    open, and a value this code does not recognise should appear in a diagnostic
    rather than fail to be represented.
    """

    system: str
    """``windows``, ``linux``, ``darwin`` -- normalised, lowercase."""

    machine: str
    """``amd64``, ``arm64`` -- normalised, lowercase."""

    library_filename: str
    """What the shared library is called here: ``tdjson.dll``, ``libtdjson.so``."""

    @property
    def key(self) -> str:
        """The manifest key for this platform, ``windows-amd64``."""
        return f"{self.system}-{self.machine}"


@dataclass(frozen=True, slots=True)
class TdlibRuntime:
    """Everything known about the TDLib runtime after an inspection.

    One object answers every question the diagnostics need: was a library found,
    where, is it trusted, does it work, and what does it support. A caller that
    has this needs nothing else to decide whether Telegram work can proceed.

    Attributes:
        platform: What was being searched for.
        candidates: Every location considered, in precedence order.
        selected: The candidate that was chosen, or ``None`` if none existed.
        digest: SHA-256 of the selected file, or ``None`` if none was read.
        verification: What verification concluded.
        manifest_entries: How many entries the manifest holds for this platform.
        binary_format: The container format read from the file's headers.
        architecture: The machine the library targets, read without loading it.
        expected_architecture: What the running interpreter requires.
        dependencies: How its runtime dependencies were classified, or ``None``
            if the file was never read.
        loaded: Whether the library was successfully loaded into the process.
        missing_symbols: Required entry points the library does not export.
        version: The version TDLib reported, or ``None`` if it was not asked.
        minimum_version: The lowest version this application accepts.
        problem: The single reason the runtime is unusable, or ``None``.
    """

    platform: PlatformInfo
    candidates: tuple[LibraryCandidate, ...] = ()
    selected: LibraryCandidate | None = None
    digest: str | None = None
    verification: VerificationOutcome | None = None
    manifest_entries: int = 0
    binary_format: BinaryFormat = BinaryFormat.UNKNOWN
    architecture: Architecture = Architecture.UNKNOWN
    expected_architecture: Architecture = Architecture.UNKNOWN
    dependencies: DependencyReport | None = None
    loaded: bool = False
    missing_symbols: tuple[str, ...] = ()
    version: str | None = None
    minimum_version: str = ""
    problem: str | None = None
    remedy: tuple[str, ...] = field(default_factory=tuple)
    """Concrete steps that would fix ``problem``. Empty when there is none."""

    @property
    def architecture_matches(self) -> bool:
        """Whether the library targets the interpreter's architecture."""
        return (
            self.architecture is not Architecture.UNKNOWN
            and self.architecture is self.expected_architecture
        )

    @property
    def is_verified(self) -> bool:
        """Whether the selected library's digest is in the manifest."""
        return self.verification is VerificationOutcome.VERIFIED

    @property
    def is_usable(self) -> bool:
        """Whether Telegram work could proceed against this runtime.

        Every condition must hold: a library was found, its digest is trusted,
        it loaded, it exports what is required, and its version is acceptable.
        ``problem`` names the first that did not.
        """
        return self.problem is None

    @property
    def library_path(self) -> Path | None:
        """The path of the selected library, or ``None``."""
        return self.selected.path if self.selected is not None else None


__all__ = [
    "Architecture",
    "BinaryFormat",
    "DependencyReport",
    "DependencyVerdict",
    "LibraryCandidate",
    "LibrarySource",
    "PlatformInfo",
    "TdlibRuntime",
    "VerificationOutcome",
]
