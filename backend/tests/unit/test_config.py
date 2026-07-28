"""Unit tests for `app.config.Settings`."""

from __future__ import annotations

from pathlib import Path

from app.config import InjectionScanStrictness, ModelProfile, Settings


class TestSettingsDefaults:
    def test_product_identity_is_gologs(self) -> None:
        s = Settings(_env_file=None)
        assert s.product_name == "GoLogs"
        assert s.product_tagline == "Interrogate Your Evidence"

    def test_default_model_profile_is_default_not_light(self) -> None:
        s = Settings(_env_file=None)
        assert s.model_profile == ModelProfile.DEFAULT

    def test_default_ollama_host_is_localhost(self) -> None:
        s = Settings(_env_file=None)
        assert s.ollama_host == "http://127.0.0.1:11434"

    def test_default_profile_maps_to_expected_ollama_model(self) -> None:
        s = Settings(_env_file=None)
        assert s.ollama_model_name == "llama3.1:8b-instruct-q4_K_M"

    def test_default_db_is_local_sqlite_file(self) -> None:
        s = Settings(_env_file=None)
        assert s.db_url.startswith("sqlite:///")

    def test_default_injection_scan_strictness_is_standard(self) -> None:
        s = Settings(_env_file=None)
        assert s.injection_scan_strictness == InjectionScanStrictness.STANDARD


class TestSettingsEnvOverrides:
    def test_env_vars_override_defaults(self, monkeypatch) -> None:
        monkeypatch.setenv("MODEL_PROFILE", "light")
        monkeypatch.setenv("MAX_TOOL_RESULT_TOKENS", "8000")
        s = Settings(_env_file=None)
        assert s.model_profile == ModelProfile.LIGHT
        assert s.max_tool_result_tokens == 8000

    def test_case_storage_dir_is_path_typed(self, monkeypatch) -> None:
        monkeypatch.setenv("CASE_STORAGE_DIR", "/tmp/some/dir")
        s = Settings(_env_file=None)
        assert isinstance(s.case_storage_dir, Path)
        assert s.case_storage_dir == Path("/tmp/some/dir")

    def test_light_profile_maps_to_expected_ollama_model(self, monkeypatch) -> None:
        monkeypatch.setenv("MODEL_PROFILE", "light")
        s = Settings(_env_file=None)
        assert s.ollama_model_name == "phi3.5:3.8b-mini-instruct-q4_K_M"


class TestAutoApproveRiskClassSet:
    def test_single_value_parses_to_frozenset(self) -> None:
        s = Settings(_env_file=None, auto_approve_risk_classes="AUTO_APPROVE")
        assert s.auto_approve_risk_class_set == frozenset({"AUTO_APPROVE"})

    def test_comma_separated_values_parse_correctly(self) -> None:
        s = Settings(_env_file=None, auto_approve_risk_classes="AUTO_APPROVE, LOW_RISK")
        assert s.auto_approve_risk_class_set == frozenset({"AUTO_APPROVE", "LOW_RISK"})

    def test_empty_string_parses_to_empty_frozenset(self) -> None:
        s = Settings(_env_file=None, auto_approve_risk_classes="")
        assert s.auto_approve_risk_class_set == frozenset()
