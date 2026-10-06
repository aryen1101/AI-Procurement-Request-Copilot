"""OpenRouter client restricted to free models (model ids ending in ':free').

Uses the OpenAI-compatible /chat/completions endpoint over plain HTTP, so no provider SDK
is required. Tool use is implemented with a JSON protocol in the prompt (see src/agent_loop.py)
because free models differ in native function-calling support.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import requests

from src import config

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / ".cache" / "llm"


class LLMUnavailable(Exception):
    """No usable LLM (no key, all models failed, or rate limited)."""


@dataclass
class LLMStats:
    calls: int = 0
    cache_hits: int = 0
    models_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ChatModel(Protocol):
    stats: LLMStats

    def chat(self, messages: list[dict]) -> str: ...


class OpenRouterChat:
    def __init__(self, api_key: str | None = None, models: list[str] | None = None, temperature: float = 0.0):
        self.api_key = api_key or config.openrouter_api_key()
        self.models = models or config.model_chain()
        self.temperature = temperature
        self.stats = LLMStats()
        if not self.api_key:
            raise LLMUnavailable("OPENROUTER_API_KEY is not set")
        if not self.models:
            raise LLMUnavailable("No ':free' OpenRouter model configured")

    def _cache_path(self, model: str, messages: list[dict]) -> Path:
        key = hashlib.sha256(json.dumps([model, messages], sort_keys=True).encode()).hexdigest()[:32]
        return CACHE_DIR / f"{key}.json"

    def chat(self, messages: list[dict]) -> str:
        last_error = "no attempt"
        for model in self.models:
            if config.llm_cache_enabled():
                path = self._cache_path(model, messages)
                if path.exists():
                    self.stats.calls += 1
                    self.stats.cache_hits += 1
                    self.stats.models_used.append(model)
                    return json.loads(path.read_text(encoding="utf-8"))["content"]
            for attempt in range(3):
                try:
                    resp = requests.post(
                        f"{config.OPENROUTER_BASE_URL}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "http://localhost:8501",
                            "X-Title": "AI Procurement Request Copilot",
                        },
                        json={"model": model, "messages": messages, "temperature": self.temperature},
                        timeout=config.llm_timeout_seconds(),
                    )
                except requests.RequestException as exc:
                    last_error = f"{model}: {type(exc).__name__}"
                    self.stats.errors.append(last_error)
                    break  # network problem -> try next model
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"{model}: HTTP {resp.status_code}"
                    self.stats.errors.append(last_error)
                    if attempt < 2:
                        retry_after = resp.headers.get("Retry-After")
                        wait = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else 2.0 * (attempt + 1)
                        time.sleep(min(wait, 10.0))
                        continue
                    break
                if resp.status_code >= 400:
                    last_error = f"{model}: HTTP {resp.status_code} {resp.text[:160]}"
                    self.stats.errors.append(last_error)
                    break  # bad request / model gone -> next model
                body = resp.json()
                if "error" in body:
                    last_error = f"{model}: {str(body['error'])[:160]}"
                    self.stats.errors.append(last_error)
                    break
                content = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
                if not content.strip():
                    last_error = f"{model}: empty response"
                    self.stats.errors.append(last_error)
                    break
                self.stats.calls += 1
                self.stats.models_used.append(body.get("model", model))
                if config.llm_cache_enabled():
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    self._cache_path(model, messages).write_text(json.dumps({"content": content}), encoding="utf-8")
                return content
        raise LLMUnavailable(f"All free models failed; last error: {last_error}")


def get_chat_model() -> ChatModel:
    if config.llm_mode() == "off":
        raise LLMUnavailable("LLM_MODE=off")
    return OpenRouterChat()


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict:
    """Return the first JSON object in a model reply (tolerates code fences, prose and <think> blocks)."""
    text = _THINK_RE.sub("", text or "")
    text = re.sub(r"```(?:json)?", "", text)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("No JSON object found in model reply")
