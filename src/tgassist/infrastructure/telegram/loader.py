"""Locating, verifying and loading ``tdjson``.

Four steps, in order, each of which can fail with a distinct remedy:

1. **Resolve** -- find a candidate file, in a documented precedence order.
2. **Verify** -- its SHA-256 must be in the pinned manifest (ADR-047).
3. **Load** -- ``ctypes`` must be able to open it and find the entry points.
4. **Probe** -- ``td_execute`` must answer, and the version must be supported.

Never falls through
-------------------

If the first candidate that *exists* fails verification, the search stops. It
does not try the next location. Falling through would mean that planting a
library in a high-precedence directory earns a silent retry elsewhere rather
than a refusal, and it would make "which library am I actually running" depend
on the failure mode. Only an **absent** candidate advances the search.

Why a probe rather than a client
--------------------------------

Version and capability come from ``td_execute``, which is synchronous, needs no
client, starts no thread and touches no network. That is the whole of what this
slice needs. The receive thread and request correlation belong to
``TdjsonClient`` and ADR-048, which is the next slice -- building them here would
be building authentication's foundation while claiming not to.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import platform as platform_module
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Protocol

from tgassist.domain.errors import (
    TdlibIncompatibleError,
    TdlibLoadFailedError,
    TdlibNotFoundError,
    TdlibUnverifiedError,
)
from tgassist.domain.model.tdlib import (
    Architecture,
    DependencyVerdict,
    LibraryCandidate,
    LibrarySource,
    PlatformInfo,
    TdlibRuntime,
    VerificationOutcome,
)
from tgassist.infrastructure.telegram.binary import current_architecture, inspect_binary
from tgassist.infrastructure.telegram.dependencies import classify_dependencies
from tgassist.infrastructure.telegram.manifest import ChecksumManifest, file_digest

REQUIRED_SYMBOLS: Final = (
    "td_create_client_id",
    "td_send",
    "td_receive",
    "td_execute",
)
"""The client API this application uses.

The older ``td_json_client_create`` interface is deprecated and behaves
differently around client lifetime, so a library exporting only that one is
rejected rather than adapted to.
"""

DEFAULT_MINIMUM_VERSION: Final = "1.8.0"
"""The client API above stabilised in 1.8. Below it, the symbols may exist and
behave differently, which is worse than their being absent."""

VENDOR_DIRECTORY: Final = "tdlib"
"""Subdirectory of the data directory searched for a vendored library."""


class NativeLibrary(Protocol):
    """The part of a loaded shared library this module uses.

    A protocol with two implementations -- ``ctypes.CDLL`` and the test double --
    which is the demonstrated reason for it to exist. Loading real native code
    is otherwise untestable: a test would need a compiler, and the suite would
    stop being deterministic.
    """

    def has_symbol(self, name: str) -> bool:
        """Report whether the library exports an entry point."""
        ...

    def execute(self, request: str) -> str | None:
        """Call ``td_execute`` with a JSON request and return its JSON reply."""
        ...


LibraryOpener = Callable[[Path], NativeLibrary]
"""Opens a shared library at a path, or raises ``OSError``."""


class CtypesLibrary:
    """A real shared library, reached through ``ctypes``."""

    __slots__ = ("_handle",)

    def __init__(self, handle: ctypes.CDLL) -> None:
        """Wrap an opened handle."""
        self._handle = handle

    def has_symbol(self, name: str) -> bool:
        """Report whether the library exports an entry point."""
        try:
            getattr(self._handle, name)
        except AttributeError:
            return False
        return True

    def execute(self, request: str) -> str | None:
        """Call ``td_execute`` synchronously.

        TDLib owns the returned buffer and it stays valid only until this
        thread's next call, so ``c_char_p`` is used deliberately: ctypes copies
        it into a Python ``bytes`` before returning.
        """
        function = self._handle.td_execute
        function.restype = ctypes.c_char_p
        function.argtypes = [ctypes.c_char_p]
        reply = function(request.encode("utf-8"))
        return reply.decode("utf-8") if reply is not None else None


def open_with_ctypes(path: Path) -> NativeLibrary:
    """Open a shared library with ``ctypes``.

    Raises:
        OSError: If the platform refuses to load it -- most often a missing
            transitive dependency or an architecture mismatch.
    """
    return CtypesLibrary(ctypes.CDLL(str(path)))


def detect_platform() -> PlatformInfo:
    """Describe the platform a library must match.

    Normalised, because ``platform.machine()`` says ``AMD64`` on Windows and
    ``x86_64`` on Linux for the same architecture, and a manifest keyed on the
    raw value would need an entry for each spelling.
    """
    system = sys.platform
    if system.startswith("win"):
        normalised_system, filename = "windows", "tdjson.dll"
    elif system == "darwin":
        normalised_system, filename = "darwin", "libtdjson.dylib"
    else:
        normalised_system, filename = "linux", "libtdjson.so"

    machine = platform_module.machine().lower()
    normalised_machine = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine or "unknown")

    return PlatformInfo(
        system=normalised_system,
        machine=normalised_machine,
        library_filename=filename,
    )


def parse_version(value: str) -> tuple[int, ...]:
    """Parse a dotted version into comparable integers.

    Trailing non-numeric parts are ignored, so ``1.8.29-beta`` compares as
    ``(1, 8, 29)``. A version that yields nothing comparable returns an empty
    tuple, which sorts below every real version and therefore fails a minimum
    check rather than passing one by accident.
    """
    parts: list[int] = []
    for chunk in value.strip().split("."):
        digits = ""
        for character in chunk:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class LoaderSettings:
    """What the loader needs from configuration.

    A small structure rather than the whole ``AppConfig``, so the loader can be
    exercised without building one.
    """

    configured_path: Path | None = None
    data_dir: Path | None = None
    minimum_version: str = DEFAULT_MINIMUM_VERSION
    log_verbosity: int = 0
    search_system: bool = True


class TdjsonLoader:
    """Finds, verifies and loads ``tdjson``, or explains why it could not."""

    __slots__ = ("_manifest", "_opener", "_platform", "_settings")

    def __init__(
        self,
        settings: LoaderSettings,
        *,
        manifest: ChecksumManifest | None = None,
        opener: LibraryOpener | None = None,
        platform_info: PlatformInfo | None = None,
    ) -> None:
        """Build a loader.

        Args:
            settings: Where to look and what to accept.
            manifest: Trusted digests. Defaults to the one shipped beside this
                module.
            opener: How to open a library. Defaults to ``ctypes``, resolved
                when the loader is built rather than when this module is
                imported -- a default bound at import time would not be
                replaceable, which would make the seam decorative.
            platform_info: Overridden in tests to exercise the Windows and Linux
                paths from one machine.
        """
        self._settings = settings
        self._manifest = manifest if manifest is not None else ChecksumManifest.load()
        self._opener = opener if opener is not None else open_with_ctypes
        self._platform = platform_info if platform_info is not None else detect_platform()

    # -- Inspection -------------------------------------------------------

    def inspect(self) -> TdlibRuntime:
        """Report on the runtime without raising.

        Diagnostics need the whole picture -- including the parts that failed --
        so this returns a report where :meth:`load` would raise. It is the same
        work, and :meth:`load` is written in terms of it, so the two can never
        disagree about whether the runtime is usable.
        """
        candidates = self._resolve_candidates()
        base = TdlibRuntime(
            platform=self._platform,
            candidates=candidates,
            manifest_entries=len(self._manifest.entries_for(self._platform.key)),
            minimum_version=self._settings.minimum_version,
            expected_architecture=current_architecture(),
        )

        selected = next((candidate for candidate in candidates if candidate.is_usable), None)
        if selected is None or selected.path is None:
            return _with(
                base,
                problem="No tdjson library was found in any searched location.",
                remedy=self._not_found_remedy(),
            )

        base = _with(base, selected=selected)
        return self._verify_and_probe(base, selected.path)

    def load(self) -> tuple[NativeLibrary, TdlibRuntime]:
        """Return a loaded, verified library.

        Raises:
            TdlibNotFoundError: No candidate exists.
            TdlibUnverifiedError: A candidate exists but is not in the manifest.
            TdlibLoadFailedError: The platform refused to load it.
            TdlibIncompatibleError: It loaded but is the wrong TDLib.
        """
        runtime = self.inspect()
        if not runtime.is_usable:
            raise self._error_for(runtime)

        library = self._open(runtime.library_path)
        if library is None:  # pragma: no cover - inspect already proved it opens
            raise TdlibLoadFailedError(
                f"tdjson at {runtime.library_path} could not be opened",
                user_message="The Telegram library could not be loaded.",
            )
        self._apply_log_verbosity(library)
        return library, runtime

    # -- Steps ------------------------------------------------------------

    def _resolve_candidates(self) -> tuple[LibraryCandidate, ...]:
        """Return every location considered, in precedence order.

        The configured path and its environment variable are one candidate, not
        two: ``TGASSIST_TELEGRAM__TDJSON_PATH`` is how the configuration system
        already layers environment over file, so a separate lookup here would be
        a second, divergent implementation of precedence that ADR-047 describes
        as one.
        """
        candidates: list[LibraryCandidate] = []

        configured = self._settings.configured_path
        if configured is not None:
            candidates.append(
                _candidate(LibrarySource.CONFIGURED, configured, "named by telegram.tdjson_path")
            )

        for path in self._vendored_paths():
            candidates.append(_candidate(LibrarySource.VENDORED, path, "vendored"))

        if self._settings.search_system:
            found = ctypes.util.find_library("tdjson")
            candidates.append(
                _candidate(LibrarySource.SYSTEM, Path(found), "system loader")
                if found
                else LibraryCandidate(
                    source=LibrarySource.SYSTEM,
                    path=None,
                    exists=False,
                    detail="not found by the platform library loader",
                )
            )

        return tuple(candidates)

    def _vendored_paths(self) -> tuple[Path, ...]:
        """Return the vendored locations, most specific first."""
        data_dir = self._settings.data_dir
        if data_dir is None:
            return ()
        root = data_dir / VENDOR_DIRECTORY
        filename = self._platform.library_filename
        paths = [root / filename]
        if root.is_dir():
            # A versioned layout, newest first, so several TDLib versions can sit
            # side by side during an upgrade.
            paths.extend(
                sorted(
                    (child / filename for child in root.iterdir() if child.is_dir()), reverse=True
                )
            )
        return tuple(paths)

    def _verify_and_probe(  # noqa: PLR0911 - one return per distinct failure, each with its own remedy
        self, base: TdlibRuntime, path: Path
    ) -> TdlibRuntime:
        """Verify the digest, inspect the file, then load and probe.

        Stops at the first failure, and each failure returns separately because
        each has a different remedy. Collapsing them into one exit would mean
        one generic message where the user needs to know whether to install a
        file, rebuild it, or upgrade it.
        """
        try:
            digest = file_digest(path)
        except OSError as exc:
            return _with(
                base,
                verification=VerificationOutcome.UNREADABLE,
                problem=f"{path} could not be read: {exc}",
                remedy=("Check the file's permissions.",),
            )

        base = _with(base, digest=digest)
        if self._manifest.find(self._platform.key, digest) is None:
            return _with(
                base,
                verification=VerificationOutcome.UNKNOWN_DIGEST,
                problem=(
                    f"{path} is not a binary this application trusts: its digest "
                    f"is not in the pinned manifest."
                ),
                remedy=self._unverified_remedy(path, digest),
            )

        base = _with(base, verification=VerificationOutcome.VERIFIED)

        # Read the headers before loading. A mismatched architecture otherwise
        # surfaces as a generic OSError naming nothing useful, and a library
        # that pulls in OpenSSL at runtime would load perfectly while putting
        # unverified code inside the boundary the digest is meant to cover.
        inspection = inspect_binary(path)
        dependencies = classify_dependencies(
            inspection.imports, readable=inspection.imports_readable
        )
        base = _with(
            base,
            binary_format=inspection.format,
            architecture=inspection.architecture,
            dependencies=dependencies,
        )

        if (
            inspection.architecture is not Architecture.UNKNOWN
            and inspection.architecture is not base.expected_architecture
        ):
            return _with(
                base,
                problem=(
                    f"{path} is a {inspection.architecture.value} library, but this "
                    f"interpreter is {base.expected_architecture.value}."
                ),
                remedy=(
                    f"Obtain a {base.expected_architecture.value} build of TDLib, "
                    f"or run a {inspection.architecture.value} interpreter.",
                ),
            )

        if dependencies.verdict is DependencyVerdict.FORBIDDEN:
            return _with(
                base,
                problem=(
                    f"{path} {dependencies.detail}. The manifest verifies this file "
                    f"only, so those libraries are unverified code inside the trust "
                    f"boundary."
                ),
                remedy=(
                    "Rebuild TDLib with OpenSSL and zlib linked statically, so the "
                    "file that is checksummed is the whole of what gets loaded.",
                    "See DEVELOPMENT_WORKFLOW.md, 'Obtaining tdjson'.",
                ),
            )

        if dependencies.verdict is DependencyVerdict.UNRECOGNISED:
            return _with(
                base,
                problem=f"{path} {dependencies.detail}.",
                remedy=(
                    "Establish what those libraries are and where they come from.",
                    "They load into this process alongside tdjson and are not "
                    "covered by the manifest.",
                ),
            )

        library = self._open(path)
        if library is None:
            return _with(
                base,
                problem=f"{path} is trusted but the platform refused to load it.",
                remedy=self._load_failure_remedy(),
            )

        base = _with(base, loaded=True)

        missing = tuple(name for name in REQUIRED_SYMBOLS if not library.has_symbol(name))
        if missing:
            return _with(
                base,
                missing_symbols=missing,
                problem=(
                    f"{path} does not export the TDLib client API: missing {', '.join(missing)}."
                ),
                remedy=(
                    "Use a TDLib 1.8 or newer build; the deprecated "
                    "td_json_client_* interface is not supported.",
                ),
            )

        self._apply_log_verbosity(library)
        return self._check_version(base, library, path, digest)

    def _check_version(
        self, base: TdlibRuntime, library: NativeLibrary, path: Path, digest: str
    ) -> TdlibRuntime:
        """Ask the library its version and compare it with what is required."""
        version = _query_version(library)
        if version is None:
            return _with(
                base,
                problem=f"{path} loaded but did not answer a version query.",
                remedy=("The file exports the right names but is not TDLib.",),
            )

        base = _with(base, version=version)

        minimum = parse_version(self._settings.minimum_version)
        if parse_version(version) < minimum:
            return _with(
                base,
                problem=(
                    f"TDLib {version} is older than the minimum supported "
                    f"version {self._settings.minimum_version}."
                ),
                remedy=("Build or obtain a newer TDLib and record its digest.",),
            )

        entry = self._manifest.find(self._platform.key, digest)
        if entry is not None and entry.version and entry.version != version:
            # A stale manifest entry pointing at a swapped file: the digest
            # matches something recorded, but not what that record described.
            return _with(
                base,
                problem=(
                    f"The manifest records this digest as TDLib {entry.version}, "
                    f"but the library reports {version}."
                ),
                remedy=("Re-check the binary's provenance before updating the entry.",),
            )

        return base

    # -- Helpers ----------------------------------------------------------

    def _open(self, path: Path | None) -> NativeLibrary | None:
        """Open a library, returning ``None`` if the platform refuses."""
        if path is None:  # pragma: no cover - callers check first
            return None
        try:
            return self._opener(path)
        except OSError:
            return None

    def _apply_log_verbosity(self, library: NativeLibrary) -> None:
        """Silence TDLib's own logging before anything else uses it.

        TDLib writes to standard error at verbosity 5 by default, which would
        put library chatter into command output -- the exact defect ADR-040
        exists to prevent, arriving by a different route.
        """
        request = json.dumps(
            {
                "@type": "setLogVerbosityLevel",
                "new_verbosity_level": self._settings.log_verbosity,
            }
        )
        try:
            library.execute(request)
        except Exception:
            return

    def _error_for(self, runtime: TdlibRuntime) -> Exception:
        """Choose the error whose remedy matches the failure."""
        context: dict[str, Any] = {
            "platform": runtime.platform.key,
            "path": str(runtime.library_path) if runtime.library_path else None,
        }
        problem = runtime.problem or "The Telegram runtime is unusable."

        if runtime.selected is None:
            return TdlibNotFoundError(
                problem,
                user_message="No Telegram library was found. Run 'tgassist tdlib doctor'.",
                context=context,
            )
        if runtime.architecture is not Architecture.UNKNOWN and not runtime.architecture_matches:
            return TdlibIncompatibleError(
                problem,
                user_message="The Telegram library is built for a different architecture.",
                context=context | {"architecture": runtime.architecture.value},
            )
        if runtime.dependencies is not None and runtime.dependencies.verdict in {
            DependencyVerdict.FORBIDDEN,
            DependencyVerdict.UNRECOGNISED,
        }:
            return TdlibUnverifiedError(
                problem,
                user_message=(
                    "The Telegram library loads code this application has not "
                    "verified. Run 'tgassist tdlib doctor'."
                ),
                context=context | {"dependencies": list(runtime.dependencies.forbidden)},
            )
        if runtime.verification is not VerificationOutcome.VERIFIED:
            return TdlibUnverifiedError(
                problem,
                user_message=(
                    "The Telegram library on this machine is not one this "
                    "application trusts. Run 'tgassist tdlib verify'."
                ),
                context=context | {"digest": runtime.digest},
            )
        if not runtime.loaded:
            return TdlibLoadFailedError(
                problem,
                user_message="The Telegram library could not be loaded.",
                context=context,
            )
        return TdlibIncompatibleError(
            problem,
            user_message="The Telegram library on this machine is not compatible.",
            context=context | {"version": runtime.version},
        )

    def _not_found_remedy(self) -> tuple[str, ...]:
        return (
            f"Obtain a TDLib build for {self._platform.key} ({self._platform.library_filename}).",
            "Set telegram.tdjson_path, or place it in "
            f"<data_dir>/{VENDOR_DIRECTORY}/{self._platform.library_filename}.",
            "See DEVELOPMENT_WORKFLOW.md, 'Obtaining tdjson'.",
        )

    def _unverified_remedy(self, path: Path, digest: str) -> tuple[str, ...]:
        return (
            "Establish where this file came from before trusting it.",
            "Then record it by adding this entry to tdjson_manifest.json:",
            json.dumps(
                {
                    "platform": self._platform.key,
                    "sha256": digest,
                    "version": None,
                    "source": f"describe where {path.name} came from",
                    "recorded": "YYYY-MM-DD",
                },
                indent=2,
            ),
        )

    def _load_failure_remedy(self) -> tuple[str, ...]:
        if self._platform.system == "windows":
            return (
                "Install the Visual C++ runtime, or check that the library and "
                "the Python interpreter are the same architecture.",
            )
        return (
            "Check its dependencies with 'ldd' (Linux) or 'otool -L' (macOS); "
            "OpenSSL and zlib are the usual omissions.",
        )


def _query_version(library: NativeLibrary) -> str | None:
    """Ask a loaded library for its TDLib version.

    ``getOption`` for ``version`` is one of the few requests ``td_execute``
    answers synchronously, so this needs no client, no thread and no network.
    """
    try:
        reply = library.execute(json.dumps({"@type": "getOption", "name": "version"}))
    except Exception:
        return None
    if not reply:
        return None
    try:
        document = json.loads(reply)
    except json.JSONDecodeError:
        return None
    value = document.get("value") if isinstance(document, dict) else None
    return value if isinstance(value, str) and value else None


def _candidate(source: LibrarySource, path: Path, detail: str) -> LibraryCandidate:
    """Describe one searched location."""
    exists = path.is_file()
    return LibraryCandidate(
        source=source,
        path=path,
        exists=exists,
        detail=detail if exists else f"{detail}: absent",
    )


def _with(runtime: TdlibRuntime, **changes: Any) -> TdlibRuntime:
    """Return a copy of a report with fields replaced."""
    return replace(runtime, **changes)


__all__ = [
    "DEFAULT_MINIMUM_VERSION",
    "REQUIRED_SYMBOLS",
    "CtypesLibrary",
    "LoaderSettings",
    "NativeLibrary",
    "TdjsonLoader",
    "detect_platform",
    "open_with_ctypes",
    "parse_version",
]
