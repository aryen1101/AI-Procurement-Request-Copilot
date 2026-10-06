"""Test helper: serve the mock vendor-risk API in-process so tests don't need port 8001."""
from __future__ import annotations

from urllib.parse import quote

from fastapi.testclient import TestClient

from mock_api.app import app
from src.vendor_client import VendorRiskNotFound, VendorRiskUnavailable

_client = TestClient(app)


def in_process_vendor_api(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    r = _client.get(f"/vendor-risk/{quote(vendor_name, safe='')}")
    if r.status_code == 404:
        raise VendorRiskNotFound(r.json().get("detail"))
    if r.status_code >= 400:
        raise VendorRiskUnavailable(f"HTTP {r.status_code}: {r.json().get('detail')}")
    return r.json()
