"""The tdlib diagnostic commands.

These run end to end against a real configuration and a real filesystem, with
only the native library itself replaced. The point of the commands is that they
perform real work rather than printing configuration back, so the tests assert
on what the work discovered.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fakes.tdjson import TDLIB_VERSION, FakeTdjson, opener_for, write_library
from tgassist.infrastructure.telegram import loader as loader_module
from tgassist.infrastructure.telegram import manifest as manifest_module
from tgassist.presentation.cli.app import app

runner = CliRunner()

LIBRARY_BYTES = b"pretend this is a shared library"
LIBRARY_DIGEST = hashlib.sha256(LIBRARY_BYTES).hexdigest()


@pytest.fixture
def cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_logging: None,  # noqa: ARG001 - a command configures logging process-wide
) -> Path:
    """Point the CLI at an isolated data directory with logging silenced.

    The system library search is disabled so a machine that happens to have
    tdjson installed produces the same result as one that does not.
    """
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(data_dir))
    monkeypatch.setenv("TGASSIST_LOGGING__CONSOLE_ENABLED", "false")
    monkeypatch.setenv("TGASSIST_LOGGING__FILE_ENABLED", "false")
    monkeypatch.setenv("TGASSIST_TELEGRAM__SEARCH_SYSTEM_LIBRARY_PATH", "false")
    return data_dir


def install_library(data_dir: Path, *, content: bytes = LIBRARY_BYTES) -> Path:
    """Put a stand-in library where the loader will find it."""
    filename = loader_module.detect_platform().library_filename
    return write_library(data_dir / "tdlib" / filename, content)


@pytest.fixture
def trusted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the loader at a manifest that trusts the stand-in library."""
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "platform": loader_module.detect_platform().key,
                        "sha256": LIBRARY_DIGEST,
                        "version": None,
                        "source": "test fixture",
                        "recorded": "2026-07-28",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(manifest_module, "MANIFEST_PATH", path)
    # The one thing that cannot be real in a deterministic test.
    monkeypatch.setattr(loader_module, "open_with_ctypes", opener_for(FakeTdjson()), raising=True)
    return path


@pytest.mark.usefixtures("cli_env")
class TestDoctorWithoutALibrary:
    def test_fails_and_says_nothing_was_found(self) -> None:
        result = runner.invoke(app, ["tdlib", "doctor"])

        assert result.exit_code != 0
        assert "No tdjson library was found" in result.output
        assert "Traceback" not in result.output

    def test_shows_every_location_it_searched(self) -> None:
        # So the user knows where to put the file, not merely that it is absent.
        result = runner.invoke(app, ["tdlib", "doctor"])

        assert "search:" in result.stdout
        assert "vendored" in result.stdout

    def test_reports_the_platform_and_the_filename_it_wants(self) -> None:
        result = runner.invoke(app, ["tdlib", "doctor"])

        assert "platform" in result.stdout
        assert "looking for" in result.stdout

    def test_later_stages_are_not_checked_rather_than_failed(self) -> None:
        # Not checked and failed are different things; conflating them sends
        # people to fix the wrong stage.
        result = runner.invoke(app, ["tdlib", "doctor"])

        assert "Checksum verified: not checked" in result.stdout
        assert "Version: not checked" in result.stdout

    def test_offers_concrete_next_steps(self) -> None:
        result = runner.invoke(app, ["tdlib", "doctor"])

        assert "To fix:" in result.stdout
        assert "DEVELOPMENT_WORKFLOW.md" in result.stdout


@pytest.mark.usefixtures("cli_env")
class TestDoctorWithAnUntrustedLibrary:
    def test_fails_because_the_digest_is_unknown(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "doctor"])

        assert result.exit_code != 0
        assert "not in the pinned manifest" in result.output

    def test_the_library_was_found_but_not_verified(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "doctor"])

        assert "ok   Library found" in result.stdout
        assert "FAIL Checksum verified" in result.stdout

    def test_it_is_never_loaded(self, cli_env: Path) -> None:
        # Verification precedes loading, so an untrusted binary is never mapped
        # into the process.
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "doctor"])

        assert "Loaded: not checked" in result.stdout


@pytest.mark.usefixtures("cli_env", "trusted")
class TestDoctorWithATrustedLibrary:
    def test_succeeds(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "doctor"])

        assert result.exit_code == 0, result.output
        assert "The Telegram library is ready." in result.stdout

    def test_every_stage_passes(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "doctor"])

        for stage in ("Library found", "Checksum verified", "Loaded", "Client API", "Version"):
            assert f"ok   {stage}" in result.stdout

    def test_reports_the_manifest_entry_count(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "doctor"])

        assert "1 trusted entry" in result.stdout


@pytest.mark.usefixtures("cli_env")
class TestVerify:
    def test_fails_when_there_is_nothing_to_verify(self) -> None:
        result = runner.invoke(app, ["tdlib", "verify"])

        assert result.exit_code != 0
        assert "No library to verify" in result.output

    def test_prints_the_digest_of_an_untrusted_library(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "verify"])

        assert result.exit_code != 0
        assert LIBRARY_DIGEST in result.output

    def test_prints_a_pasteable_manifest_entry(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "verify"])

        assert '"sha256"' in result.output
        assert '"platform"' in result.output

    def test_asks_for_provenance_before_trust(self, cli_env: Path) -> None:
        # Verification is a claim about where a file came from, and only a
        # person can make it (ADR-047).
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "verify"])

        assert "came from" in result.output

    def test_a_changed_binary_stops_verifying(self, cli_env: Path, trusted: Path) -> None:
        assert trusted
        install_library(cli_env, content=b"swapped for something else")

        result = runner.invoke(app, ["tdlib", "verify"])

        assert result.exit_code != 0
        assert "NOT VERIFIED" in result.output


@pytest.mark.usefixtures("cli_env", "trusted")
class TestVerifyWhenTrusted:
    def test_succeeds(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "verify"])

        assert result.exit_code == 0, result.output
        assert "Verified" in result.stdout

    def test_reports_the_digest_it_checked(self, cli_env: Path) -> None:
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "verify"])

        assert LIBRARY_DIGEST in result.stdout


@pytest.mark.usefixtures("cli_env")
class TestVersion:
    def test_fails_without_a_usable_library(self) -> None:
        result = runner.invoke(app, ["tdlib", "version"])

        assert result.exit_code != 0
        assert "No version available" in result.output
        assert "Traceback" not in result.output


@pytest.mark.usefixtures("cli_env", "trusted")
class TestVersionWhenAvailable:
    def test_reports_what_the_library_said(self, cli_env: Path) -> None:
        # From the library, not from configuration.
        install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "version"])

        assert result.exit_code == 0, result.output
        assert TDLIB_VERSION in result.stdout

    def test_reports_the_minimum_and_the_path(self, cli_env: Path) -> None:
        path = install_library(cli_env)

        result = runner.invoke(app, ["tdlib", "version"])

        assert "minimum supported" in result.stdout
        assert str(path) in result.stdout


@pytest.mark.usefixtures("cli_env")
class TestConfiguredPath:
    def test_an_explicit_path_is_searched_first(
        self, cli_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_library(cli_env)
        explicit = write_library(tmp_path / "explicit" / "tdjson.bin", b"explicitly named")
        monkeypatch.setenv("TGASSIST_TELEGRAM__TDJSON_PATH", str(explicit))

        result = runner.invoke(app, ["tdlib", "verify"])

        assert str(explicit) in result.output

    def test_naming_a_file_does_not_trust_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The configured path is the highest precedence candidate, not an
        # exemption from verification.
        explicit = write_library(tmp_path / "explicit" / "tdjson.bin", b"explicitly named")
        monkeypatch.setenv("TGASSIST_TELEGRAM__TDJSON_PATH", str(explicit))

        result = runner.invoke(app, ["tdlib", "verify"])

        assert result.exit_code != 0
        assert "NOT VERIFIED" in result.output
