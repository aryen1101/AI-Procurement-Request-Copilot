"""Central configuration. Values come from environment variables (.env is loaded by src/__init__.py)."""
from __future__ import annotations

import os
from datetime import date

# Policy section header: "Data snapshot / evaluation reference date: 2026-09-30".
# All date-based checks use this date, never the machine clock.
REFERENCE_DATE = date.fromisoformat(os.getenv("POLICY_REFERENCE_DATE", "2026-09-30"))
SECURITY_REVIEW_VALID_DAYS = 365

VENDOR_RISK_BASE_URL = os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
DEFAULT_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_FALLBACK_MODELS = "nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-26b-a4b-it:free"


def openrouter_api_key() -> str | None:
    return os.getenv("OPENROUTER_API_KEY") or None


def model_chain() -> list[str]:
    """Primary model first, then fallbacks. Only ':free' models are accepted (assignment constraint)."""
    primary = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip()
    fallbacks = os.getenv("OPENROUTER_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
    chain: list[str] = []
    for name in [primary, *fallbacks.split(",")]:
        name = name.strip()
        if name and name.endswith(":free") and name not in chain:
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
