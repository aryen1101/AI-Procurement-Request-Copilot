"""Groq client (free tier).

Uses the OpenAI-compatible /chat/completions endpoint over plain HTTP, so no provider SDK
is required. Tool use is implemented with a JSON protocol in the prompt (see src/agent_loop.py)
so any model in the fallback chain works, whatever its native function-calling support.
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
MAX_ROUNDS = 5  # passes over the model chain before giving up
MAX_RETRY_WAIT_SECONDS = 60.0


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


class GroqChat:
    def __init__(self, api_key: str | None = None, models: list[str] | None = None, temperature: float = 0.0):
        self.api_key = api_key or config.groq_api_key()
        self.models = models or config.model_chain()
        self.temperature = temperature
        self.stats = LLMStats()
        if not self.api_key:
            raise LLMUnavailable("GROQ_API_KEY is not set")
        if not self.models:
            raise LLMUnavailable("No Groq model configured")

    def _cache_path(self, model: str, messages: list[dict]) -> Path:
        key = hashlib.sha256(json.dumps([model, messages], sort_keys=True).encode()).hexdigest()[:32]
        return CACHE_DIR / f"{key}.json"

    def chat(self, messages: list[dict]) -> str:
        if config.llm_cache_enabled():
            for model in self.models:
                path = self._cache_path(model, messages)
                if path.exists():
                    self.stats.calls += 1
                    self.stats.cache_hits += 1
                    self.stats.models_used.append(model)
                    return json.loads(path.read_text(encoding="utf-8"))["content"]
        # Groq's free tier limits tokens per minute *per model*, so on a 429 move straight to the next
        # model and only sleep (for the Retry-After Groq asks for) once every model is rate limited.
        last_error = "no attempt"
        models = list(self.models)
        for round_no in range(MAX_ROUNDS):
            waits: list[float] = []
            for model in list(models):
                try:
                    resp = requests.post(
                        f"{config.GROQ_BASE_URL}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json={"model": model, "messages": messages, "temperature": self.temperature},
                        timeout=config.llm_timeout_seconds(),
                    )
                except requests.RequestException as exc:
                    last_error = f"{model}: {type(exc).__name__}"
                    self.stats.errors.append(last_error)
                    waits.append(2.0 * (round_no + 1))
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"{model}: HTTP {resp.status_code}"
                    self.stats.errors.append(last_error)
                    retry_after = resp.headers.get("Retry-After", "")
                    waits.append(float(retry_after) if retry_after.replace(".", "").isdigit() else 2.0 * (round_no + 1))
                    continue
                recovered = _recover_tool_call(resp) if resp.status_code == 400 else None
                if recovered:
                    self.stats.calls += 1
                    self.stats.models_used.append(model)
                    return recovered
                if resp.status_code >= 400:
                    last_error = f"{model}: HTTP {resp.status_code} {resp.text[:160]}"
                    self.stats.errors.append(last_error)
                    if resp.status_code in (404, 413):
                        models.remove(model)  # model not available to this key / prompt too large for it
                    continue
                body = resp.json()
                if "error" in body:
                    last_error = f"{model}: {str(body['error'])[:160]}"
                    self.stats.errors.append(last_error)
                    continue
                message = (body.get("choices") or [{}])[0].get("message") or {}
                content = message.get("content") or ""
                if not content.strip() and message.get("tool_calls"):
                    # gpt-oss sometimes emits a native tool call instead of the JSON protocol; translate it.
                    content = json.dumps({"tool_calls": [
                        {"name": (c.get("function") or {}).get("name"), "args": (c.get("function") or {}).get("arguments") or "{}"}
                        for c in message["tool_calls"]]})
                if not content.strip():
                    last_error = f"{model}: empty response"
                    self.stats.errors.append(last_error)
                    continue
                self.stats.calls += 1
                self.stats.models_used.append(body.get("model", model))
                if config.llm_cache_enabled():
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    self._cache_path(model, messages).write_text(json.dumps({"content": content}), encoding="utf-8")
                return content
            if not waits or not models or round_no == MAX_ROUNDS - 1:
                break
            time.sleep(min(min(waits), MAX_RETRY_WAIT_SECONDS))
        raise LLMUnavailable(f"All Groq models failed; last error: {last_error}")


def _recover_tool_call(resp: requests.Response) -> str | None:
    """gpt-oss sometimes answers with a native tool call, which Groq rejects with code 'tool_use_failed'
    but echoes back in 'failed_generation'. Translate it into our JSON tool protocol instead of failing."""
    try:
        err = resp.json().get("error") or {}
        if err.get("code") != "tool_use_failed":
            return None
        call = json.loads(err.get("failed_generation") or "")
    except (ValueError, AttributeError):
        return None
    if not isinstance(call, dict) or not call.get("name"):
        return None
    name = str(call["name"]).split(".")[-1]  # e.g. "functions.get_vendor_registry"
    return json.dumps({"tool_calls": [{"name": name, "args": call.get("arguments", call.get("parameters", {}))}]})


def get_chat_model() -> ChatModel:
    if config.llm_mode() == "off":
        raise LLMUnavailable("LLM_MODE=off")
    return GroqChat()


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
