"""Locating, verifying and loading the native Telegram library.

Every failure path is reachable without a compiler, a binary or a network, which
is what keeps this suite deterministic. The Windows and Linux search paths are
both exercised from whichever machine runs the tests, by injecting the platform
rather than detecting it.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import sys
from pathlib import Path

import pytest

from tests.fakes.tdjson import (
    TDLIB_VERSION,
    FakeTdjson,
    HostileLibrary,
    MalformedReplyLibrary,
    SilentLibrary,
    make_elf,
    make_pe,
    opener_for,
    refusing_opener,
    write_library,
)
from tgassist.domain.errors import (
    TdlibIncompatibleError,
    TdlibLoadFailedError,
    TdlibNotFoundError,
    TdlibUnverifiedError,
)
from tgassist.domain.model.tdlib import (
    Architecture,
    DependencyVerdict,
    LibrarySource,
    PlatformInfo,
    VerificationOutcome,
)
from tgassist.infrastructure.telegram import (
    MANIFEST_PATH,
    ChecksumManifest,
    LoaderSettings,
    ManifestEntry,
    TdjsonLoader,
    file_digest,
    parse_version,
)
from tgassist.infrastructure.telegram.binary import (
    current_architecture,
    inspect_binary,
)
from tgassist.infrastructure.telegram.dependencies import classify_dependencies
from tgassist.infrastructure.telegram.loader import (
    DEFAULT_MINIMUM_VERSION,
    REQUIRED_SYMBOLS,
    CtypesLibrary,
    LibraryOpener,
    NativeLibrary,
    detect_platform,
    open_with_ctypes,
)

WINDOWS = PlatformInfo(system="windows", machine="amd64", library_filename="tdjson.dll")
LINUX = PlatformInfo(system="linux", machine="amd64", library_filename="libtdjson.so")

LIBRARY_BYTES = b"pretend this is a shared library"
LIBRARY_DIGEST = hashlib.sha256(LIBRARY_BYTES).hexdigest()
SHA256_HEX_LENGTH = 64


def trusting_manifest(
    platform: PlatformInfo = WINDOWS, *, version: str | None = None
) -> ChecksumManifest:
    """A manifest that trusts the fixture library on one platform."""
    return ChecksumManifest(
        (
            ManifestEntry(
                platform=platform.key,
                sha256=LIBRARY_DIGEST,
                version=version,
                source="test fixture",
                recorded="2026-07-28",
            ),
        )
    )


def build_loader(  # noqa: PLR0913 - one knob per thing a test varies
    tmp_path: Path,
    *,
    platform: PlatformInfo = WINDOWS,
    manifest: ChecksumManifest | None = None,
    library: NativeLibrary | None = None,
    opener: LibraryOpener | None = None,
    minimum_version: str = DEFAULT_MINIMUM_VERSION,
    configured: Path | None = None,
) -> TdjsonLoader:
    """Build a loader that searches only the temporary directory."""
    resolved_opener = opener if opener is not None else opener_for(library or FakeTdjson())
    return TdjsonLoader(
        LoaderSettings(
            configured_path=configured,
            data_dir=tmp_path,
            minimum_version=minimum_version,
            # The system loader is off so the search is entirely under the
            # test's control; a machine with tdjson installed must not change
            # the result.
            search_system=False,
        ),
        manifest=manifest if manifest is not None else trusting_manifest(platform),
        opener=resolved_opener,
        platform_info=platform,
    )


def vendored(tmp_path: Path, platform: PlatformInfo = WINDOWS) -> Path:
    """Write the fixture library where the loader will find it."""
    return write_library(tmp_path / "tdlib" / platform.library_filename, LIBRARY_BYTES)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


class TestPlatformDetection:
    def test_reports_this_machine(self) -> None:
        info = detect_platform()

        assert info.system in {"windows", "linux", "darwin"}
        assert info.library_filename.startswith(("tdjson", "libtdjson"))

    @pytest.mark.parametrize(
        ("platform", "filename"),
        [(WINDOWS, "tdjson.dll"), (LINUX, "libtdjson.so")],
    )
    def test_each_platform_names_its_library(self, platform: PlatformInfo, filename: str) -> None:
        assert platform.library_filename == filename

    def test_the_manifest_key_joins_system_and_machine(self) -> None:
        assert WINDOWS.key == "windows-amd64"
        assert LINUX.key == "linux-amd64"


class TestVersionParsing:
    @pytest.mark.parametrize(
        ("value", "parsed"),
        [
            ("1.8.29", (1, 8, 29)),
            ("1.8", (1, 8)),
            ("1.8.29-beta", (1, 8, 29)),
            ("  1.8.0  ", (1, 8, 0)),
        ],
    )
    def test_parses_dotted_versions(self, value: str, parsed: tuple[int, ...]) -> None:
        assert parse_version(value) == parsed

    @pytest.mark.parametrize("value", ["", "not-a-version", "beta"])
    def test_an_unparseable_version_sorts_below_everything(self, value: str) -> None:
        # So it fails a minimum check rather than passing one by accident.
        assert parse_version(value) < parse_version("1.0.0")

    def test_ordering_is_numeric_not_lexicographic(self) -> None:
        assert parse_version("1.8.10") > parse_version("1.8.9")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_reports_every_location_searched(self, tmp_path: Path) -> None:
        # Including the ones that were empty: a diagnostic showing only the
        # failure says what went wrong, one showing the search says where to put
        # the file.
        runtime = build_loader(tmp_path, configured=tmp_path / "explicit.dll").inspect()

        sources = [candidate.source for candidate in runtime.candidates]
        assert LibrarySource.CONFIGURED in sources
        assert LibrarySource.VENDORED in sources
        assert all(not candidate.exists for candidate in runtime.candidates)

    def test_finds_a_vendored_library(self, tmp_path: Path) -> None:
        path = vendored(tmp_path)

        runtime = build_loader(tmp_path).inspect()

        assert runtime.library_path == path
        assert runtime.selected is not None
        assert runtime.selected.source is LibrarySource.VENDORED

    def test_a_configured_path_outranks_a_vendored_one(self, tmp_path: Path) -> None:
        vendored(tmp_path)
        explicit = write_library(tmp_path / "elsewhere" / "tdjson.dll", LIBRARY_BYTES)

        runtime = build_loader(tmp_path, configured=explicit).inspect()

        assert runtime.library_path == explicit

    def test_finds_a_versioned_vendored_layout(self, tmp_path: Path) -> None:
        # So several TDLib versions can sit side by side during an upgrade.
        path = write_library(tmp_path / "tdlib" / "1.8.29" / "tdjson.dll", LIBRARY_BYTES)

        runtime = build_loader(tmp_path).inspect()

        assert runtime.library_path == path

    def test_the_linux_search_looks_for_the_linux_filename(self, tmp_path: Path) -> None:
        # Exercised from any machine: the platform is injected, not detected.
        path = vendored(tmp_path, LINUX)

        runtime = build_loader(
            tmp_path, platform=LINUX, manifest=trusting_manifest(LINUX)
        ).inspect()

        assert runtime.library_path == path
        assert runtime.is_usable

    def test_the_windows_search_ignores_a_linux_library(self, tmp_path: Path) -> None:
        vendored(tmp_path, LINUX)

        runtime = build_loader(tmp_path, platform=WINDOWS).inspect()

        assert runtime.selected is None

    def test_a_directory_is_not_a_library(self, tmp_path: Path) -> None:
        (tmp_path / "tdlib" / "tdjson.dll").mkdir(parents=True)

        runtime = build_loader(tmp_path).inspect()

        assert runtime.selected is None


class TestMissingBinary:
    def test_reports_that_nothing_was_found(self, tmp_path: Path) -> None:
        runtime = build_loader(tmp_path).inspect()

        assert not runtime.is_usable
        assert runtime.problem is not None
        assert "No tdjson library was found" in runtime.problem

    def test_the_remedy_names_the_platform_and_the_place_to_put_it(self, tmp_path: Path) -> None:
        runtime = build_loader(tmp_path).inspect()

        joined = " ".join(runtime.remedy)
        assert "windows-amd64" in joined
        assert "tdjson.dll" in joined
        assert "DEVELOPMENT_WORKFLOW.md" in joined

    def test_loading_raises_the_not_found_error(self, tmp_path: Path) -> None:
        with pytest.raises(TdlibNotFoundError):
            build_loader(tmp_path).load()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerification:
    def test_a_digest_in_the_manifest_verifies(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(tmp_path).inspect()

        assert runtime.verification is VerificationOutcome.VERIFIED
        assert runtime.digest == LIBRARY_DIGEST

    def test_an_empty_manifest_trusts_nothing(self, tmp_path: Path) -> None:
        # The correct initial state of a fresh checkout.
        vendored(tmp_path)

        runtime = build_loader(tmp_path, manifest=ChecksumManifest()).inspect()

        assert runtime.verification is VerificationOutcome.UNKNOWN_DIGEST
        assert not runtime.is_usable

    def test_a_changed_binary_stops_verifying(self, tmp_path: Path) -> None:
        # The whole point: the same path, different bytes, no longer trusted.
        write_library(tmp_path / "tdlib" / "tdjson.dll", b"swapped for something else")

        runtime = build_loader(tmp_path).inspect()

        assert runtime.verification is VerificationOutcome.UNKNOWN_DIGEST

    def test_an_unverified_library_is_never_loaded(self, tmp_path: Path) -> None:
        # Verification precedes loading, so a corrupt or hostile binary is never
        # mapped into the process.
        write_library(tmp_path / "tdlib" / "tdjson.dll", b"hostile")
        library = FakeTdjson()

        runtime = build_loader(tmp_path, library=library).inspect()

        assert not runtime.loaded
        assert library.requests == []

    def test_a_digest_recorded_for_another_platform_does_not_transfer(self, tmp_path: Path) -> None:
        vendored(tmp_path, WINDOWS)

        runtime = build_loader(
            tmp_path, platform=WINDOWS, manifest=trusting_manifest(LINUX)
        ).inspect()

        assert runtime.verification is VerificationOutcome.UNKNOWN_DIGEST

    def test_the_remedy_is_a_pasteable_manifest_entry(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(tmp_path, manifest=ChecksumManifest()).inspect()

        entry = json.loads(runtime.remedy[-1])
        assert entry["sha256"] == LIBRARY_DIGEST
        assert entry["platform"] == "windows-amd64"

    def test_the_remedy_asks_for_provenance_first(self, tmp_path: Path) -> None:
        # A digest recorded from whatever happened to be on disk makes the whole
        # mechanism theatre (ADR-047's risk note).
        vendored(tmp_path)

        runtime = build_loader(tmp_path, manifest=ChecksumManifest()).inspect()

        assert "came from" in runtime.remedy[0]

    def test_loading_raises_a_security_error(self, tmp_path: Path) -> None:
        # Not a configuration inconvenience: tdjson sees the session key.
        vendored(tmp_path)

        with pytest.raises(TdlibUnverifiedError):
            build_loader(tmp_path, manifest=ChecksumManifest()).load()


class TestNeverFallsBack:
    def test_an_unverified_candidate_stops_the_search(self, tmp_path: Path) -> None:
        # The configured path holds an untrusted file and the vendored path a
        # trusted one. Falling through would mean planting a library in a
        # high-precedence location earns a silent retry rather than a refusal.
        untrusted = write_library(tmp_path / "planted" / "tdjson.dll", b"planted")
        vendored(tmp_path)

        runtime = build_loader(tmp_path, configured=untrusted).inspect()

        assert runtime.library_path == untrusted
        assert runtime.verification is VerificationOutcome.UNKNOWN_DIGEST
        assert not runtime.is_usable

    def test_an_absent_candidate_does_advance_the_search(self, tmp_path: Path) -> None:
        # Absence is not a failure, so the next location is tried.
        path = vendored(tmp_path)

        runtime = build_loader(tmp_path, configured=tmp_path / "nothing-here.dll").inspect()

        assert runtime.library_path == path
        assert runtime.is_usable


# ---------------------------------------------------------------------------
# Loading and capabilities
# ---------------------------------------------------------------------------


class TestLoading:
    def test_a_verified_library_loads(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(tmp_path).inspect()

        assert runtime.loaded
        assert runtime.is_usable

    def test_a_platform_refusal_is_reported_not_raised_by_inspect(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(tmp_path, opener=refusing_opener()).inspect()

        assert not runtime.loaded
        assert runtime.problem is not None
        assert "refused to load" in runtime.problem

    def test_a_platform_refusal_raises_on_load(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        with pytest.raises(TdlibLoadFailedError):
            build_loader(tmp_path, opener=refusing_opener()).load()

    @pytest.mark.parametrize(
        ("platform", "hint"),
        [(WINDOWS, "Visual C++"), (LINUX, "ldd")],
    )
    def test_the_refusal_remedy_matches_the_platform(
        self, tmp_path: Path, platform: PlatformInfo, hint: str
    ) -> None:
        vendored(tmp_path, platform)

        runtime = build_loader(
            tmp_path,
            platform=platform,
            manifest=trusting_manifest(platform),
            opener=refusing_opener(),
        ).inspect()

        assert hint in " ".join(runtime.remedy)


class TestCapabilities:
    def test_a_library_missing_the_client_api_is_rejected(self, tmp_path: Path) -> None:
        # The deprecated td_json_client_* interface: a real TDLib, the wrong one.
        vendored(tmp_path)
        old = FakeTdjson(symbols=("td_json_client_create", "td_json_client_send"))

        runtime = build_loader(tmp_path, library=old).inspect()

        assert set(runtime.missing_symbols) == set(REQUIRED_SYMBOLS)
        assert not runtime.is_usable

    def test_one_missing_symbol_is_enough(self, tmp_path: Path) -> None:
        vendored(tmp_path)
        partial = FakeTdjson(symbols=tuple(s for s in REQUIRED_SYMBOLS if s != "td_receive"))

        runtime = build_loader(tmp_path, library=partial).inspect()

        assert runtime.missing_symbols == ("td_receive",)

    def test_missing_symbols_raise_incompatible(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        with pytest.raises(TdlibIncompatibleError):
            build_loader(tmp_path, library=FakeTdjson(symbols=())).load()

    def test_the_version_comes_from_the_library_not_configuration(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(tmp_path, library=FakeTdjson(version="1.8.42")).inspect()

        assert runtime.version == "1.8.42"

    def test_tdlib_logging_is_silenced_before_anything_else(self, tmp_path: Path) -> None:
        # TDLib writes to standard error at verbosity 5 by default. Command
        # output must not carry library chatter (the concern behind ADR-040,
        # arriving by a different route).
        vendored(tmp_path)
        library = FakeTdjson()

        build_loader(tmp_path, library=library).inspect()

        assert library.requests[0]["@type"] == "setLogVerbosityLevel"

    def test_the_configured_verbosity_is_applied(self, tmp_path: Path) -> None:
        vendored(tmp_path)
        library = FakeTdjson()
        loader = TdjsonLoader(
            LoaderSettings(data_dir=tmp_path, log_verbosity=3, search_system=False),
            manifest=trusting_manifest(),
            opener=opener_for(library),
            platform_info=WINDOWS,
        )

        loader.inspect()

        assert library.requests[0]["new_verbosity_level"] == 3


class TestUnsupportedVersion:
    def test_a_version_below_the_minimum_is_rejected(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(
            tmp_path, library=FakeTdjson(version="1.7.9"), minimum_version="1.8.0"
        ).inspect()

        assert not runtime.is_usable
        assert runtime.problem is not None
        assert "older than the minimum" in runtime.problem

    def test_the_minimum_version_is_reported(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(tmp_path, minimum_version="1.8.5").inspect()

        assert runtime.minimum_version == "1.8.5"

    def test_an_old_version_raises_incompatible(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        with pytest.raises(TdlibIncompatibleError):
            build_loader(tmp_path, library=FakeTdjson(version="1.6.0")).load()

    def test_a_library_that_answers_nothing_is_rejected(self, tmp_path: Path) -> None:
        # A file with the right symbol names that is not TDLib.
        vendored(tmp_path)

        runtime = build_loader(tmp_path, library=SilentLibrary()).inspect()

        assert runtime.version is None
        assert not runtime.is_usable

    def test_a_library_that_raises_is_rejected_not_propagated(self, tmp_path: Path) -> None:
        # A ctypes call into a mismatched binary fails unpredictably; the loader
        # must conclude "unusable" rather than let it escape.
        vendored(tmp_path)

        runtime = build_loader(tmp_path, library=HostileLibrary()).inspect()

        assert runtime.version is None
        assert not runtime.is_usable

    def test_a_malformed_reply_is_rejected(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(tmp_path, library=MalformedReplyLibrary()).inspect()

        assert runtime.version is None
        assert not runtime.is_usable

    def test_a_stale_manifest_entry_is_caught(self, tmp_path: Path) -> None:
        # The digest matches something recorded, but not what that record said
        # it was: the manifest entry is stale or the file was swapped.
        vendored(tmp_path)

        runtime = build_loader(
            tmp_path,
            manifest=trusting_manifest(version="1.8.0"),
            library=FakeTdjson(version="1.8.29"),
        ).inspect()

        assert not runtime.is_usable
        assert runtime.problem is not None
        assert "records this digest as TDLib 1.8.0" in runtime.problem

    def test_a_matching_manifest_version_passes(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(
            tmp_path,
            manifest=trusting_manifest(version=TDLIB_VERSION),
            library=FakeTdjson(version=TDLIB_VERSION),
        ).inspect()

        assert runtime.is_usable


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------


class TestSuccessfulLoad:
    def test_load_returns_the_library_and_the_report(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        library, runtime = build_loader(tmp_path).load()

        assert library.has_symbol("td_execute")
        assert runtime.is_usable
        assert runtime.version == TDLIB_VERSION

    def test_inspect_and_load_agree(self, tmp_path: Path) -> None:
        # load() is written in terms of inspect(), so the two can never disagree
        # about whether the runtime is usable.
        vendored(tmp_path)
        loader = build_loader(tmp_path)

        runtime = loader.inspect()
        _, from_load = loader.load()

        assert runtime.is_usable == from_load.is_usable
        assert runtime.digest == from_load.digest

    def test_a_usable_runtime_has_no_problem_and_no_remedy(self, tmp_path: Path) -> None:
        vendored(tmp_path)

        runtime = build_loader(tmp_path).inspect()

        assert runtime.problem is None
        assert runtime.remedy == ()


# ---------------------------------------------------------------------------
# The manifest itself
# ---------------------------------------------------------------------------


class TestChecksumManifest:
    def test_the_shipped_manifest_parses(self) -> None:
        # It is loaded on every inspection; a malformed one would break the
        # diagnostics that exist to explain breakage.
        assert isinstance(ChecksumManifest.load(), ChecksumManifest)

    def test_an_absent_file_is_an_empty_manifest(self, tmp_path: Path) -> None:
        # A checkout that has never recorded a binary is a normal state, and it
        # should fail at the point of use rather than at import.
        assert len(ChecksumManifest.load(tmp_path / "absent.json")) == 0

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[]",
            '{"no_entries": true}',
            '{"entries": [{"platform": "windows-amd64"}]}',
            '{"entries": [{"platform": "windows-amd64", "sha256": "tooshort"}]}',
            '{"entries": ["not an object"]}',
            '{"entries": [{"sha256": "' + "a" * 64 + '"}]}',
        ],
    )
    def test_a_malformed_manifest_raises_rather_than_reading_as_empty(
        self, tmp_path: Path, content: str
    ) -> None:
        # An empty manifest looks exactly like a fresh checkout, so degrading to
        # one would hide a corrupted trust store.
        path = tmp_path / "manifest.json"
        path.write_text(content, encoding="utf-8")

        with pytest.raises(ValueError, match="manifest"):
            ChecksumManifest.load(path)

    def test_digests_are_matched_case_insensitively(self) -> None:
        manifest = ChecksumManifest(
            (ManifestEntry(platform="windows-amd64", sha256=LIBRARY_DIGEST.upper()),)
        )

        assert manifest.find("windows-amd64", LIBRARY_DIGEST) is not None

    def test_entries_are_counted_per_platform(self) -> None:
        manifest = ChecksumManifest(
            (
                ManifestEntry(platform="windows-amd64", sha256="a" * 64),
                ManifestEntry(platform="linux-amd64", sha256="b" * 64),
                ManifestEntry(platform="windows-amd64", sha256="c" * 64),
            )
        )

        assert len(manifest.entries_for("windows-amd64")) == 2
        assert len(manifest.entries_for("darwin-arm64")) == 0


class TestFileDigest:
    def test_matches_hashlib(self, tmp_path: Path) -> None:
        path = write_library(tmp_path / "lib.bin", LIBRARY_BYTES)

        assert file_digest(path) == LIBRARY_DIGEST

    def test_handles_a_file_larger_than_one_chunk(self, tmp_path: Path) -> None:
        # tdjson is tens of megabytes; the digest is computed in chunks.
        content = b"x" * (2 * 1024 * 1024 + 7)
        path = write_library(tmp_path / "big.bin", content)

        assert file_digest(path) == hashlib.sha256(content).hexdigest()

    def test_an_unreadable_file_is_reported_not_crashed(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone.bin"

        with pytest.raises(OSError, match=r"gone\.bin"):
            file_digest(missing)


# ---------------------------------------------------------------------------
# The real ctypes wrapper
# ---------------------------------------------------------------------------


class TestCtypesLoading:
    """The one piece the fakes cannot cover, tested against the real loader.

    ``open_with_ctypes`` and ``CtypesLibrary`` are production code that every
    other test replaces. These exercise them directly, using files and libraries
    that exist on any machine, so the suite stays deterministic.
    """

    def test_a_missing_file_raises_oserror(self, tmp_path: Path) -> None:
        # The loader catches OSError specifically; anything else would escape as
        # an unhandled error instead of a diagnostic.
        with pytest.raises(OSError, match="does-not-exist"):
            open_with_ctypes(tmp_path / "does-not-exist.dll")

    def test_a_file_that_is_not_a_library_raises_oserror(self, tmp_path: Path) -> None:
        path = write_library(tmp_path / "impostor.dll", b"MZ but not really")

        with pytest.raises(OSError, match=r"impostor|not a valid|cannot open"):
            open_with_ctypes(path)

    def test_symbol_detection_against_a_real_system_library(self) -> None:
        # Proves has_symbol answers from the actual export table rather than
        # from a lookup that always succeeds.
        name, present, absent = (
            ("kernel32", "GetTickCount", "NoSuchExportExists")
            if sys.platform.startswith("win")
            else ("libc.so.6", "printf", "NoSuchExportExists")
        )
        try:
            library = CtypesLibrary(ctypes.CDLL(name))
        except OSError:  # pragma: no cover - platform without the chosen library
            pytest.skip(f"{name} is not loadable here")

        assert library.has_symbol(present)
        assert not library.has_symbol(absent)


# ---------------------------------------------------------------------------
# Reading a binary without loading it
# ---------------------------------------------------------------------------


class TestBinaryInspection:
    """Architecture and imports, read from headers on any machine."""

    @pytest.mark.parametrize("machine", ["amd64", "x86", "arm64"])
    def test_reads_pe_architecture(self, tmp_path: Path, machine: str) -> None:
        path = write_library(tmp_path / "lib.dll", make_pe(machine))

        assert inspect_binary(path).architecture is Architecture(machine)

    @pytest.mark.parametrize("machine", ["amd64", "x86", "arm64"])
    def test_reads_elf_architecture(self, tmp_path: Path, machine: str) -> None:
        # Exercised from any machine, because the header is written by the test.
        path = write_library(tmp_path / "lib.so", make_elf(machine))

        assert inspect_binary(path).architecture is Architecture(machine)

    def test_reads_the_pe_import_table(self, tmp_path: Path) -> None:
        path = write_library(tmp_path / "lib.dll", make_pe(imports=["KERNEL32.dll", "WS2_32.dll"]))

        inspection = inspect_binary(path)

        assert inspection.imports_readable
        assert inspection.imports == ("kernel32.dll", "ws2_32.dll")

    def test_a_pe_importing_nothing_is_readable_and_empty(self, tmp_path: Path) -> None:
        # Distinct from "could not read": one is a fact, the other is ignorance.
        path = write_library(tmp_path / "lib.dll", make_pe(imports=[]))

        inspection = inspect_binary(path)

        assert inspection.imports_readable
        assert inspection.imports == ()

    def test_elf_imports_are_unreadable_rather_than_empty(self, tmp_path: Path) -> None:
        # DT_NEEDED is not parsed, and an empty list would read as "no
        # dependencies" when it means "not checked".
        path = write_library(tmp_path / "lib.so", make_elf())

        assert not inspect_binary(path).imports_readable

    @pytest.mark.parametrize(
        "content",
        [b"", b"MZ", b"not a binary at all", b"\x7fELF", b"MZ" + b"\x00" * 200],
    )
    def test_a_malformed_file_never_raises(self, tmp_path: Path, content: bytes) -> None:
        # A loader that crashed on a corrupt binary would be worse at its job
        # than one that reports it.
        path = write_library(tmp_path / "lib.dll", content)

        assert inspect_binary(path).detail

    def test_a_missing_file_is_reported(self, tmp_path: Path) -> None:
        assert "could not be read" in inspect_binary(tmp_path / "absent.dll").detail

    def test_the_current_architecture_is_known(self) -> None:
        assert current_architecture() is not Architecture.UNKNOWN


class TestDependencyClassification:
    """Which runtime dependencies are acceptable for a trusted library."""

    def test_system_libraries_are_acceptable(self) -> None:
        report = classify_dependencies(("kernel32.dll", "ws2_32.dll"), readable=True)

        assert report.is_acceptable
        assert report.system == ("kernel32.dll", "ws2_32.dll")

    def test_api_set_stubs_are_acceptable(self) -> None:
        report = classify_dependencies(("api-ms-win-crt-runtime-l1-1-0.dll",), readable=True)

        assert report.is_acceptable

    @pytest.mark.parametrize(
        "name",
        [
            "libcrypto-3-x64.dll",
            "libssl-3-x64.dll",
            "zlib1.dll",
            "libcrypto.so.3",
            "ssleay32.dll",
        ],
    )
    def test_crypto_and_compression_are_forbidden(self, name: str) -> None:
        # Their presence means the digest covers less than the trust boundary.
        report = classify_dependencies((name,), readable=True)

        assert report.verdict is DependencyVerdict.FORBIDDEN
        assert report.forbidden == (name,)

    def test_a_forbidden_import_outweighs_acceptable_ones(self) -> None:
        report = classify_dependencies(("kernel32.dll", "libcrypto-3-x64.dll"), readable=True)

        assert report.verdict is DependencyVerdict.FORBIDDEN

    def test_an_unknown_library_is_not_silently_admitted(self) -> None:
        # An allow-list that admits the unknown is not an allow-list.
        report = classify_dependencies(("kernel32.dll", "something-else.dll"), readable=True)

        assert report.verdict is DependencyVerdict.UNRECOGNISED
        assert report.unrecognised == ("something-else.dll",)

    def test_the_visual_cpp_runtime_is_allowed_but_noted(self) -> None:
        # Not a security problem, but the target machine needs it installed.
        report = classify_dependencies(("kernel32.dll", "vcruntime140.dll"), readable=True)

        assert report.is_acceptable
        assert report.needs_redistributable
        assert "redistributable" in report.detail

    def test_unreadable_imports_are_not_checked_rather_than_accepted(self) -> None:
        # An empty list from a format we cannot parse is absent evidence.
        report = classify_dependencies((), readable=False)

        assert report.verdict is DependencyVerdict.NOT_CHECKED
        assert not report.is_acceptable


def _install_pe(tmp_path: Path, machine: str, imports: list[str]) -> ChecksumManifest:
    """Write a synthetic PE where the loader looks, and trust its digest."""
    content = make_pe(machine, imports=imports)
    write_library(tmp_path / "tdlib" / "tdjson.dll", content)
    return ChecksumManifest(
        (ManifestEntry(platform=WINDOWS.key, sha256=hashlib.sha256(content).hexdigest()),)
    )


def _other_architecture() -> str:
    """Return an architecture this interpreter is not."""
    return "x86" if current_architecture() is Architecture.AMD64 else "amd64"


class TestArchitectureEnforcement:
    """A library must match the interpreter, and is rejected before loading."""

    def test_a_matching_architecture_passes(self, tmp_path: Path) -> None:
        manifest = _install_pe(tmp_path, current_architecture().value, ["KERNEL32.dll"])

        runtime = build_loader(tmp_path, manifest=manifest).inspect()

        assert runtime.architecture_matches
        assert runtime.is_usable

    def test_a_mismatched_architecture_is_rejected(self, tmp_path: Path) -> None:
        wrong = _other_architecture()
        manifest = _install_pe(tmp_path, wrong, ["KERNEL32.dll"])

        runtime = build_loader(tmp_path, manifest=manifest).inspect()

        assert not runtime.is_usable
        assert runtime.problem is not None
        assert wrong in runtime.problem

    def test_a_mismatched_architecture_is_never_loaded(self, tmp_path: Path) -> None:
        # Reading the header first turns an opaque OSError into a sentence.
        manifest = _install_pe(tmp_path, _other_architecture(), ["KERNEL32.dll"])
        library = FakeTdjson()

        build_loader(tmp_path, manifest=manifest, library=library).inspect()

        assert library.requests == []

    def test_it_raises_incompatible_not_a_generic_load_failure(self, tmp_path: Path) -> None:
        manifest = _install_pe(tmp_path, _other_architecture(), ["KERNEL32.dll"])

        with pytest.raises(TdlibIncompatibleError):
            build_loader(tmp_path, manifest=manifest).load()


class TestDependencyEnforcement:
    """A trusted library must not load untrusted code."""

    def test_a_self_contained_library_passes(self, tmp_path: Path) -> None:
        manifest = _install_pe(
            tmp_path, current_architecture().value, ["KERNEL32.dll", "WS2_32.dll", "CRYPT32.dll"]
        )

        runtime = build_loader(tmp_path, manifest=manifest).inspect()

        assert runtime.dependencies is not None
        assert runtime.dependencies.is_acceptable
        assert runtime.is_usable

    def test_a_library_loading_openssl_is_rejected(self, tmp_path: Path) -> None:
        # It would load and work perfectly, which is exactly why this is checked.
        manifest = _install_pe(
            tmp_path, current_architecture().value, ["KERNEL32.dll", "libcrypto-3-x64.dll"]
        )

        runtime = build_loader(tmp_path, manifest=manifest).inspect()

        assert not runtime.is_usable
        assert runtime.problem is not None
        assert "libcrypto-3-x64.dll" in runtime.problem

    def test_the_remedy_says_to_link_statically(self, tmp_path: Path) -> None:
        manifest = _install_pe(tmp_path, current_architecture().value, ["zlib1.dll"])

        runtime = build_loader(tmp_path, manifest=manifest).inspect()

        assert "statically" in " ".join(runtime.remedy)

    def test_a_rejected_library_is_never_loaded(self, tmp_path: Path) -> None:
        manifest = _install_pe(tmp_path, current_architecture().value, ["libssl-3-x64.dll"])
        library = FakeTdjson()

        build_loader(tmp_path, manifest=manifest, library=library).inspect()

        assert library.requests == []

    def test_an_unrecognised_dependency_is_rejected(self, tmp_path: Path) -> None:
        manifest = _install_pe(
            tmp_path, current_architecture().value, ["KERNEL32.dll", "mystery.dll"]
        )

        runtime = build_loader(tmp_path, manifest=manifest).inspect()

        assert not runtime.is_usable
        assert runtime.problem is not None
        assert "mystery.dll" in runtime.problem

    def test_a_forbidden_dependency_raises_a_security_error(self, tmp_path: Path) -> None:
        # Unverified code inside the trust boundary is a security question.
        manifest = _install_pe(tmp_path, current_architecture().value, ["libcrypto-3-x64.dll"])

        with pytest.raises(TdlibUnverifiedError):
            build_loader(tmp_path, manifest=manifest).load()


class TestShippedManifest:
    """The committed trust store itself.

    Platform-agnostic on purpose: asserting that a *particular* platform has an
    entry would fail on every machine that has not recorded one, which is every
    fresh checkout and most CI runners. What must hold everywhere is that each
    entry is well-formed and says where its binary came from.
    """

    def test_it_parses(self) -> None:
        assert isinstance(ChecksumManifest.load(), ChecksumManifest)

    def test_every_entry_is_loadable(self) -> None:
        # A malformed entry raises during load, so reaching here is the
        # assertion; the count is reported for the reader's benefit.
        manifest = ChecksumManifest.load()

        assert len(manifest) >= 0

    def test_every_entry_records_its_provenance(self) -> None:
        # A digest without a source is a checksum nobody can justify: it proves
        # the file has not changed, and nothing about whether it should be
        # trusted (ADR-047).
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        for entry in document["entries"]:
            assert entry.get("source"), f"{entry.get('platform')} entry has no source"
            assert entry.get("recorded"), f"{entry.get('platform')} entry has no date"

    def test_every_digest_is_a_sha256(self) -> None:
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        for entry in document["entries"]:
            digest = entry["sha256"]
            assert len(digest) == SHA256_HEX_LENGTH
            assert digest == digest.lower()
            assert all(character in "0123456789abcdef" for character in digest)

    def test_no_platform_has_two_entries_for_one_digest(self) -> None:
        # A duplicate means someone recorded the same binary twice, which makes
        # the provenance ambiguous: which sentence describes it?
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        seen = [(entry["platform"], entry["sha256"]) for entry in document["entries"]]

        assert len(seen) == len(set(seen))
