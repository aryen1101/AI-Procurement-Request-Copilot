"""Make sure the mock vendor-risk API is running.

Starter-pack fix: the public eval runner called the copilot without starting the API, so every
vendor looked "unavailable" unless run_local.py happened to be running in another terminal.
"""
from __future__ import annotations

import atexit
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from src import config

ROOT = Path(__file__).resolve().parents[1]


def is_up(timeout: float = 0.5) -> bool:
    try:
        return requests.get(f"{config.VENDOR_RISK_BASE_URL}/health", timeout=timeout).ok
    except requests.RequestException:
        return False


def ensure_mock_api(timeout_seconds: float = 15.0) -> subprocess.Popen | None:
    """Start the mock API in the background if it is not reachable. Returns the process it started."""
    if is_up():
        return None
    parsed = urlparse(config.VENDOR_RISK_BASE_URL)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return None  # remote service configured; don't try to start anything
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_api.app:app", "--host", "127.0.0.1",
         "--port", str(parsed.port or 8001), "--log-level", "warning"],
        cwd=ROOT,
    )
    atexit.register(lambda: proc.poll() is None and proc.terminate())
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Mock vendor-risk API exited with code {proc.returncode} (port in use?)")
        if is_up():
            return proc
        time.sleep(0.25)
    raise RuntimeError("Mock vendor-risk API did not become ready")
