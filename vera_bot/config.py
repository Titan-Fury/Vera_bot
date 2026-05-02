"""Typed configuration for the Vera bot.

All runtime behaviour is driven from environment variables so the same artifact
can run against Gemini in dev, Groq in staging, and Ollama on-prem without code
changes. Values are read once at import time; see `.env.example` for the full
matrix.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    from dotenv import load_dotenv  # type: ignore

    # override=True so .env wins over stale OS env vars (common on Windows,
    # where `setx LLM_PROVIDER ollama` leaks into every new shell).
    load_dotenv(override=True)
except Exception:  # pragma: no cover — dotenv is optional
    pass


COMPOSER_VERSION = "composer_v1"
"""Stamped into every composed message's rationale. Bump on prompt changes."""

APP_VERSION = "0.2.1"
MAX_BODY_CHARS = 320
TICK_ACTION_CAP = 20
CONTEXT_PAYLOAD_CAP_BYTES = 500 * 1024
DEFAULT_COMPOSE_TIMEOUT_S = 9.0
JUDGE_CALL_TIMEOUT_S = 10.0
# Per-tick concurrency cap. 6 keeps OpenRouter free tier happy while
# letting precompute drain before the judge's first /v1/tick arrives.
TICK_CONCURRENCY = int(os.environ.get("TICK_CONCURRENCY") or "6")


@dataclass(frozen=True)
class Settings:
    """Process-level configuration snapshot."""

    # LLM selection
    llm_provider: Optional[str] = None  # explicit override
    llm_model: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    perplexity_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    together_api_key: Optional[str] = None
    ollama_host: Optional[str] = None
    openrouter_referer: str = "https://magicpin.in"

    # Submission metadata
    team_name: str = "Team Beta"
    team_members: tuple[str, ...] = ("Agent",)
    contact_email: str = "team@example.com"
    approach: str = (
        "Per-trigger-kind prompt routing with post-LLM validation (length, URL, "
        "repetition) over a pluggable multi-provider composer."
    )

    # Ops
    log_level: str = "INFO"
    submitted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


def load_settings() -> Settings:
    team_members_raw = _env("TEAM_MEMBERS", "Agent") or "Agent"
    team_members = tuple(m.strip() for m in team_members_raw.split(",") if m.strip())

    return Settings(
        llm_provider=(_env("LLM_PROVIDER") or "").strip().lower() or None,
        llm_model=_env("LLM_MODEL"),
        gemini_api_key=_env("GEMINI_API_KEY"),
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        perplexity_api_key=_env("PERPLEXITY_API_KEY"),
        groq_api_key=_env("GROQ_API_KEY"),
        together_api_key=_env("TOGETHER_API_KEY"),
        ollama_host=_env("OLLAMA_HOST"),
        openrouter_referer=_env("OPENROUTER_REFERER", "https://magicpin.in") or "https://magicpin.in",
        team_name=_env("TEAM_NAME", "Team Beta") or "Team Beta",
        team_members=team_members,
        contact_email=_env("CONTACT_EMAIL", "team@example.com") or "team@example.com",
        approach=_env("APPROACH", Settings.__dataclass_fields__["approach"].default) or "",
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
    )


# Singleton settings instance. Do NOT mutate; re-import to pick up env changes.
SETTINGS: Settings = load_settings()
