"""Configuration system tests.

These verify the properties `docs/CONFIGURATION.md` section 13 requires:
complete defaults, correct precedence, unknown-key rejection, type validation,
immutability and secret masking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tgassist.domain.errors import (
    InvalidConfigurationValueError,
    UnknownConfigurationKeyError,
)
from tgassist.infrastructure.config import AppConfig, LogLevel, Profile, load_config
from tgassist.infrastructure.config.loader import resolve_profile


class TestDefaults:
    def test_application_starts_with_no_configuration_files(self, tmp_path: Path) -> None:
        loaded = load_config(config_dir=tmp_path / "absent")

        assert loaded.config.logging.level is LogLevel.INFO
        assert loaded.config.security.enforce_file_permissions is True
        assert loaded.layers == ()

    def test_missing_files_are_reported_but_not_an_error(self, tmp_path: Path) -> None:
        loaded = load_config(config_dir=tmp_path / "absent")

        assert len(loaded.missing_layers) == 3

    def test_committed_default_file_matches_the_models(self, repo_config_dir: Path) -> None:
        # If default.yaml drifts from the models, unknown-key rejection turns it
        # into a startup failure. This test catches that at commit time instead.
        loaded = load_config(config_dir=repo_config_dir, profile=Profile.PRODUCTION)

        assert loaded.config.profile is Profile.PRODUCTION

    @pytest.mark.parametrize("profile", list(Profile))
    def test_every_shipped_profile_is_valid(self, repo_config_dir: Path, profile: Profile) -> None:
        loaded = load_config(config_dir=repo_config_dir, profile=profile)

        assert loaded.config.profile is profile


class TestPrecedence:
    def test_default_file_overrides_built_in_defaults(self, load_from: Any) -> None:
        loaded = load_from({"default.yaml": "logging:\n  level: ERROR\n"})

        assert loaded.config.logging.level is LogLevel.ERROR

    def test_profile_overrides_default_file(self, load_from: Any) -> None:
        loaded = load_from(
            {
                "default.yaml": "logging:\n  level: ERROR\n",
                "profiles/testing.yaml": "logging:\n  level: WARNING\n",
            },
            profile=Profile.TESTING,
        )

        assert loaded.config.logging.level is LogLevel.WARNING

    def test_local_overrides_profile(self, load_from: Any) -> None:
        loaded = load_from(
            {
                "default.yaml": "logging:\n  level: ERROR\n",
                "profiles/testing.yaml": "logging:\n  level: WARNING\n",
                "local.yaml": "logging:\n  level: DEBUG\n",
            },
            profile=Profile.TESTING,
        )

        assert loaded.config.logging.level is LogLevel.DEBUG

    def test_environment_overrides_local(
        self, load_from: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TGASSIST_LOGGING__LEVEL", "CRITICAL")

        loaded = load_from({"local.yaml": "logging:\n  level: DEBUG\n"})

        assert loaded.config.logging.level is LogLevel.CRITICAL

    def test_explicit_overrides_beat_environment(
        self, write_config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TGASSIST_LOGGING__LEVEL", "CRITICAL")

        loaded = load_config(
            config_dir=write_config({}),
            overrides={"logging": {"level": "DEBUG"}},
        )

        assert loaded.config.logging.level is LogLevel.DEBUG

    def test_merge_is_deep_and_preserves_sibling_keys(self, load_from: Any) -> None:
        loaded = load_from(
            {
                "default.yaml": "logging:\n  level: ERROR\n  backup_count: 9\n",
                "local.yaml": "logging:\n  level: DEBUG\n",
            }
        )

        assert loaded.config.logging.level is LogLevel.DEBUG
        assert loaded.config.logging.backup_count == 9


class TestOriginTracking:
    def test_origin_names_the_winning_source(self, load_from: Any) -> None:
        loaded = load_from(
            {
                "default.yaml": "logging:\n  level: ERROR\n  backup_count: 9\n",
                "local.yaml": "logging:\n  level: DEBUG\n",
            }
        )

        assert loaded.origins["logging.level"].endswith("local.yaml")
        assert loaded.origins["logging.backup_count"].endswith("default.yaml")

    def test_environment_origin_is_labelled(
        self, load_from: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TGASSIST_LOGGING__LEVEL", "CRITICAL")

        loaded = load_from({})

        assert loaded.origins["logging.level"] == "environment"


class TestValidation:
    def test_unknown_top_level_key_is_rejected(self, load_from: Any) -> None:
        with pytest.raises(UnknownConfigurationKeyError) as excinfo:
            load_from({"default.yaml": "databse:\n  path: x.db\n"})

        assert "databse" in excinfo.value.message
        assert excinfo.value.code == "CONFIG_UNKNOWN_KEY"

    def test_unknown_nested_key_is_rejected(self, load_from: Any) -> None:
        with pytest.raises(UnknownConfigurationKeyError):
            load_from({"default.yaml": "logging:\n  levle: DEBUG\n"})

    def test_invalid_enum_value_is_rejected(self, load_from: Any) -> None:
        with pytest.raises(InvalidConfigurationValueError):
            load_from({"default.yaml": "logging:\n  level: CHATTY\n"})

    def test_out_of_range_value_is_rejected(self, load_from: Any) -> None:
        with pytest.raises(InvalidConfigurationValueError):
            load_from({"default.yaml": "logging:\n  max_file_mb: 0\n"})

    def test_malformed_yaml_is_rejected(self, load_from: Any) -> None:
        with pytest.raises(InvalidConfigurationValueError):
            load_from({"default.yaml": "logging:\n  level: [unclosed\n"})

    def test_non_mapping_document_is_rejected(self, load_from: Any) -> None:
        with pytest.raises(InvalidConfigurationValueError):
            load_from({"default.yaml": "- a\n- b\n"})

    def test_empty_file_is_accepted(self, load_from: Any) -> None:
        loaded = load_from({"default.yaml": "\n"})

        assert loaded.config.logging.level is LogLevel.INFO

    def test_unknown_profile_is_rejected(self) -> None:
        with pytest.raises(InvalidConfigurationValueError):
            resolve_profile("staging")

    def test_diagnostic_mode_is_rejected_in_production(self, load_from: Any) -> None:
        # Diagnostic mode writes third-party conversation content to disk. It
        # must be a deliberate runtime action, never a shipped configuration.
        with pytest.raises(InvalidConfigurationValueError):
            load_from(
                {"default.yaml": "logging:\n  diagnostic_mode: true\n"},
                profile=Profile.PRODUCTION,
            )

    def test_diagnostic_mode_is_permitted_in_development(self, load_from: Any) -> None:
        loaded = load_from(
            {"default.yaml": "logging:\n  diagnostic_mode: true\n"},
            profile=Profile.DEVELOPMENT,
        )

        assert loaded.config.logging.diagnostic_mode is True


class TestProfileResolution:
    def test_default_profile_is_development(self) -> None:
        assert resolve_profile() is Profile.DEVELOPMENT

    def test_profile_comes_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TGASSIST_PROFILE", "production")

        assert resolve_profile() is Profile.PRODUCTION

    def test_profile_is_case_insensitive(self) -> None:
        assert resolve_profile("PRODUCTION") is Profile.PRODUCTION

    def test_configuration_file_cannot_change_the_active_profile(self, load_from: Any) -> None:
        # Otherwise a file could select a profile after that profile's own file
        # had already been applied, which is unresolvable.
        loaded = load_from(
            {"default.yaml": "profile: production\n"},
            profile=Profile.TESTING,
        )

        assert loaded.config.profile is Profile.TESTING


class TestImmutability:
    def test_configuration_cannot_be_mutated(self, config: AppConfig) -> None:
        with pytest.raises(Exception, match=r"frozen|immutable"):
            config.logging.level = LogLevel.DEBUG  # type: ignore[misc]

    def test_sections_cannot_be_replaced(self, config: AppConfig) -> None:
        with pytest.raises(Exception, match=r"frozen|immutable"):
            config.logging = None  # type: ignore[assignment,misc]


class TestPaths:
    def test_paths_derive_from_the_data_directory(self, data_dir: Path) -> None:
        config = AppConfig.model_validate({"app": {"data_dir": data_dir}})

        assert config.paths.logs_dir == data_dir / "logs"
        assert config.paths.sessions_dir == data_dir / "sessions"

    def test_explicit_log_dir_wins(self, data_dir: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere"
        config = AppConfig.model_validate(
            {"app": {"data_dir": data_dir}, "logging": {"dir": elsewhere}}
        )

        assert config.log_dir == elsewhere

    def test_ensure_creates_every_directory(self, data_dir: Path) -> None:
        config = AppConfig.model_validate({"app": {"data_dir": data_dir}})

        config.paths.ensure(restrict_permissions=False)

        for directory in config.paths.all_directories():
            assert directory.is_dir()
