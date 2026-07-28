"""GoLogs configuration.

All runtime configuration is sourced from environment variables (optionally via
a local `.env` file). See `.env.example` at the repo root for the authoritative,
documented list. Every value here has a safe local-only default so the app runs
out of the box with zero configuration, per FR-12 (fully offline) and the
[HW-RULE] resource budget in PRD §0.

No secrets are required for core functionality — GoLogs never calls a cloud
LLM and has no external API dependency in the core investigation loop.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelProfile(StrEnum):
    """Selects which local Ollama model profile to use. See PRD §11.1."""

    DEFAULT = "default"  # llama3.1:8b-instruct-q4_K_M (~4.9 GB)
    LIGHT = "light"  # phi3.5:3.8b-mini-instruct-q4_K_M (~2.2 GB)


class InjectionScanStrictness(StrEnum):
    """Controls pattern-match sensitivity of the InjectionScanner. See PRD §11.4."""

    STANDARD = "standard"
    STRICT = "strict"


class Settings(BaseSettings):
    """Central settings object. Instantiate once via `get_settings()`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Product identity (GoLogs rebrand — authoritative) ---
    product_name: str = "GoLogs"
    product_tagline: str = "Interrogate Your Evidence"

    # --- Ollama / LLM runtime ---
    ollama_host: str = Field(default="http://127.0.0.1:11434")
    model_profile: ModelProfile = Field(default=ModelProfile.DEFAULT)

    # --- Context / tool-result budget ---
    max_tool_result_tokens: int = Field(default=4000, gt=0)

    # --- Storage ---
    case_storage_dir: Path = Field(default=Path("./data/cases"))
    db_url: str = Field(default="sqlite:///./data/copilot.db")

    # --- Guardrails ---
    auto_approve_risk_classes: str = Field(default="AUTO_APPROVE")
    injection_scan_strictness: InjectionScanStrictness = Field(
        default=InjectionScanStrictness.STANDARD
    )

    # --- Observability ---
    log_level: str = Field(default="INFO")

    # --- API binding (local-only, single-user; PRD §14, §20) ---
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)

    @property
    def ollama_model_name(self) -> str:
        if self.model_profile is ModelProfile.DEFAULT:
            return "llama3.1:8b-instruct-q4_K_M"
        if self.model_profile is ModelProfile.LIGHT:
            return "phi3.5:3.8b-mini-instruct-q4_K_M"
        raise ValueError(f"Unsupported MODEL_PROFILE: {self.model_profile}")

    @property
    def auto_approve_risk_class_set(self) -> frozenset[str]:
        return frozenset(c.strip() for c in self.auto_approve_risk_classes.split(",") if c.strip())


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings singleton, constructing it on first use."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
