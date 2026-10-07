"""Central configuration. Values come from environment variables (.env is loaded by src/__init__.py)."""
from __future__ import annotations

import os
from datetime import date

# Policy section header: "Data snapshot / evaluation reference date: 2026-09-30".
# All date-based checks use this date, never the machine clock.
REFERENCE_DATE = date.fromisoformat(os.getenv("POLICY_REFERENCE_DATE", "2026-09-30"))
SECURITY_REVIEW_VALID_DAYS = 365

VENDOR_RISK_BASE_URL = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")

GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_FALLBACK_MODELS = "qwen/qwen3.8-27b,openai/gpt-oss-20b"


def groq_api_key() -> str | None:
    return os.getenv("GROQ_API_KEY") or None


def model_chain() -> list[str]:
    """Primary model first, then fallbacks (all available on the Groq free tier)."""
    primary = os.getenv("GROQ_MODEL", DEFAULT_MODEL).strip()
    fallbacks = os.getenv("GROQ_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
    chain: list[str] = []
    for name in [primary, *fallbacks.split(",")]:
        name = name.strip()
        if name and name not in chain:
            chain.append(name)
    return chain


def llm_mode() -> str:
    """'auto' = use the LLM when a key is configured, otherwise deterministic fallback.
    'off'  = never call the LLM (rules-only run, useful offline and in CI)."""
    return os.getenv("LLM_MODE", "auto").strip().lower()


def llm_timeout_seconds() -> float:
    return float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))


def llm_cache_enabled() -> bool:
    return os.getenv("LLM_CACHE", "0").strip() in {"1", "true", "yes"}
