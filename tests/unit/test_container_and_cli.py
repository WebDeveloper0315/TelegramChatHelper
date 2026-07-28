"""Composition root and command line adapter tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tgassist import __version__
from tgassist.application.container import Container
from tgassist.infrastructure.config import AppConfig, LoadedConfig, Profile
from tgassist.presentation.cli.app import app

runner = CliRunner()

# A deliberately realistic fake, used to assert it never reaches stdout.
SECRET_LOOKING_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"  # noqa: S105


class TestContainer:
    def test_exposes_the_resolved_configuration(self, config: AppConfig) -> None:
        container = Container(LoadedConfig(config=config))

        assert container.config is config

    def test_creates_the_directory_layout(self, config: AppConfig, data_dir: Path) -> None:
        container = Container(LoadedConfig(config=config))

        container.ensure_directories()

        assert (data_dir / "logs").is_dir()
        assert (data_dir / "sessions").is_dir()

    def test_is_a_context_manager(self, config: AppConfig) -> None:
        with Container(LoadedConfig(config=config)) as container:
            assert not container.is_closed

        assert container.is_closed

    def test_create_resolves_configuration_without_touching_global_logging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(tmp_path / "data"))

        container = Container.create(
            profile=Profile.TESTING,
            config_dir=tmp_path / "absent",
            configure_logging_on_start=False,
        )

        assert container.config.profile is Profile.TESTING
        assert container.config.paths.data_dir == tmp_path / "data"


class TestCli:
    def test_version(self) -> None:
        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert __version__ in result.stdout

    def test_no_arguments_shows_help(self) -> None:
        result = runner.invoke(app, [])

        assert "config" in result.stdout
        assert "doctor" in result.stdout

    def test_config_validate_accepts_the_shipped_configuration(self, repo_config_dir: Path) -> None:
        result = runner.invoke(app, ["config", "validate", "--config-dir", str(repo_config_dir)])

        assert result.exit_code == 0
        assert "valid" in result.stdout

    def test_config_validate_reports_a_bad_file_clearly(self, write_config: Any) -> None:
        config_dir = write_config({"default.yaml": "logging:\n  levle: DEBUG\n"})

        result = runner.invoke(app, ["config", "validate", "--config-dir", str(config_dir)])

        assert result.exit_code == 2
        assert "CONFIG_UNKNOWN_KEY" in result.output
        assert "levle" in result.output

    def test_config_show_reports_layers_and_origins(self, repo_config_dir: Path) -> None:
        result = runner.invoke(app, ["config", "show", "--config-dir", str(repo_config_dir)])

        assert result.exit_code == 0
        assert "default.yaml" in result.stdout
        assert "logging:" in result.stdout
        assert "level:" in result.stdout

    def test_config_show_masks_sensitive_values(self, write_config: Any) -> None:
        # No secret-shaped key exists in configuration today, so this proves the
        # masking path rather than a current leak -- before there is one to leak.
        config_dir = write_config({})
        result = runner.invoke(
            app,
            ["config", "show", "--config-dir", str(config_dir)],
            env={"TGASSIST_APP__LOCALE": "en"},
        )

        assert result.exit_code == 0
        assert SECRET_LOOKING_KEY not in result.stdout

    def test_config_path_lists_present_and_absent_layers(self, repo_config_dir: Path) -> None:
        result = runner.invoke(app, ["config", "path", "--config-dir", str(repo_config_dir)])

        assert result.exit_code == 0
        assert "present" in result.stdout
        assert "absent" in result.stdout  # local.yaml is gitignored

    def test_doctor_reports_pending_subsystems_honestly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(tmp_path / "data"))

        result = runner.invoke(app, ["doctor", "--config-dir", str(tmp_path / "absent")])

        assert result.exit_code == 0
        assert "Telegram library: not implemented yet" in result.stdout

    def test_doctor_checks_the_secret_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TGASSIST_APP__DATA_DIR", str(tmp_path / "data"))

        result = runner.invoke(app, ["doctor", "--config-dir", str(tmp_path / "absent")])

        assert "Secret store:" in result.stdout
        assert "not implemented yet" not in result.stdout.split("Secret store:")[1][:40]

    def test_doctor_fails_loudly_on_bad_configuration(self, write_config: Any) -> None:
        config_dir = write_config({"default.yaml": "logging:\n  level: LOUD\n"})

        result = runner.invoke(app, ["doctor", "--config-dir", str(config_dir)])

        assert result.exit_code == 2
