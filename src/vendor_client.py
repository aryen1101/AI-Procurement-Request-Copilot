from __future__ import annotations

from urllib.parse import quote
import requests

from src import config


class VendorRiskNotFound(Exception):
    """The service is up but holds no assessment for this vendor (HTTP 404)."""


class VendorRiskUnavailable(Exception):
    """The service could not answer (timeout, connection error, 5xx)."""


def get_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Low-level API client.

    Starter-pack fix: the original client raised the same HTTPError for "no record" (404)
    and "outage" (503). Those mean different things for policy, so they are now separate
    exception types. Neither may be interpreted as a favourable status (policy section 10).
    """
    url = f"{config.VENDOR_RISK_BASE_URL}/vendor-risk/{quote(vendor_name, safe='')}"
    try:
        response = requests.get(url, timeout=timeout_seconds)
    except requests.RequestException as exc:
        raise VendorRiskUnavailable(f"{type(exc).__name__}: could not reach vendor-risk service") from exc
    if response.status_code == 404:
        raise VendorRiskNotFound(f"No vendor-risk record for '{vendor_name}'")
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail", "")
        except ValueError:
            pass
        raise VendorRiskUnavailable(f"HTTP {response.status_code}: {detail}".strip())
    return response.json()
