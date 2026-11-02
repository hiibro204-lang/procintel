"""Runtime configuration, read from the environment with sane defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    app_name: str = "Procurement Intelligence Platform"
    version: str = "0.1.0"
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'procintel.db'}"))
    archive_dir: Path = field(default_factory=lambda: Path(_env("ARCHIVE_DIR", str(BASE_DIR / "archive"))))

    # LLM extraction / assistant. The key is read from the environment and is
    # never written to disk, logged or committed.
    openrouter_api_key: str | None = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    openrouter_base_url: str = field(default_factory=lambda: _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))
    extraction_model: str = field(default_factory=lambda: _env("EXTRACTION_MODEL", "google/gemini-2.0-flash-001"))
    assistant_model: str = field(default_factory=lambda: _env("ASSISTANT_MODEL", "anthropic/claude-3.5-haiku"))
    llm_timeout_seconds: int = int(_env("LLM_TIMEOUT_SECONDS", "90"))

    # Compliance / crawling
    crawler_user_agent: str = field(
        default_factory=lambda: _env(
            "CRAWLER_USER_AGENT",
            "InfiSourceProcurementBot/0.1 (+contact: procurement-ops@infisource.example)",
        )
    )
    crawler_max_requests_per_min: int = int(_env("CRAWLER_MAX_RPM", "20"))
    policy_max_age_days: int = int(_env("POLICY_MAX_AGE_DAYS", "365"))

    # Statistical reporting floors (blueprint Section 10.3)
    min_n_for_stats: int = int(_env("MIN_N_FOR_STATS", "3"))
    min_n_for_distribution: int = int(_env("MIN_N_FOR_DISTRIBUTION", "8"))

    # Tenancy: when false, all requests run as the internal tenant with full access.
    auth_enabled: bool = field(default_factory=lambda: _env_bool("AUTH_ENABLED", False))
    default_tenant_slug: str = field(default_factory=lambda: _env("DEFAULT_TENANT_SLUG", "infisource"))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openrouter_api_key)


settings = Settings()
settings.archive_dir.mkdir(parents=True, exist_ok=True)
