"""Agent tools.

Every tool is a plain Python function that:
  * reads only from the data layer / mock API (never from the LLM),
  * never raises - failures come back as structured results (`available: false`),
  * is recorded in the run telemetry.

Five of the six tools are fully deterministic. `get_vendor_risk` calls the external
mock service and can fail. The LLM decides *which* tools to call and with what
arguments; the orchestrator guarantees the mandatory ones run (see `ensure_core_evidence`).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd

from src import config, data_access
from src.vendor_client import VendorRiskNotFound, VendorRiskUnavailable, get_vendor_risk as _api_get_vendor_risk


# --------------------------------------------------------------------------- run context

@dataclass
class ToolCallRecord:
    name: str
    args: dict
    result: dict
    caller: str  # "agent" | "guard"
    duration_ms: float


@dataclass
class RunContext:
    request: dict
    calls: list[ToolCallRecord] = field(default_factory=list)

    @property
    def tool_names(self) -> list[str]:
        return [c.name for c in self.calls]

    def latest(self, name: str, predicate: Callable[[dict, dict], bool] | None = None) -> dict | None:
        for call in reversed(self.calls):
            if call.name == name and (predicate is None or predicate(call.args, call.result)):
                return call.result
        return None


# --------------------------------------------------------------------------- helpers

def _clean(value: Any) -> Any:
    """Convert pandas NaN / numpy scalars to JSON-friendly values."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _row(df_row: pd.Series) -> dict:
    return {k: _clean(v) for k, v in df_row.to_dict().items()}


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def review_age_days(review_date: str | None) -> int | None:
    if not review_date:
        return None
    try:
        return (config.REFERENCE_DATE - date.fromisoformat(str(review_date))).days
    except ValueError:
        return None


EXTENSION_KEYWORDS = ("add-on", "addon", "add on", "expansion", "expand", "additional", "extra seats", "training", "renewal")
STOPWORDS = {
    "the", "and", "for", "with", "our", "team", "teams", "new", "pro", "plus", "enterprise", "business",
    "tool", "tools", "software", "platform", "suite", "needs", "need", "wants", "want", "use", "using",
}


def _keywords(*texts: Any) -> set[str]:
    words: set[str] = set()
    for text in texts:
        for w in re.findall(r"[a-z][a-z0-9&+-]{2,}", _norm(text)):
            if w not in STOPWORDS:
                words.add(w)
    return words


# --------------------------------------------------------------------------- tools

def get_requester_budget(ctx: RunContext, employee_id: str | None = None) -> dict:
    """Deterministic: requester, reporting line and department budget vs. requested cost."""
    req = ctx.request
    employee_id = employee_id or req.get("requester_id")
    employees = data_access.load_employees()
    match = employees[employees["employee_id"] == employee_id]
    if match.empty:
        return {"available": True, "found": False, "employee_id": employee_id,
                "detail": "Requester not found in employee directory."}
    emp = _row(match.iloc[0])
    manager = None
    if emp.get("manager_id"):
        mrow = employees[employees["employee_id"] == emp["manager_id"]]
        if not mrow.empty:
            m = _row(mrow.iloc[0])
            manager = {"employee_id": m["employee_id"], "name": m["name"], "level": m["level"]}

    budgets = data_access.load_budgets()
    brow = budgets[budgets["department"] == emp["department"]]
    budget = _row(brow.iloc[0]) if not brow.empty else None

    cost = req.get("annual_cost_usd")
    within = shortfall = None
    if budget is not None and cost is not None:
        within = float(cost) <= float(budget["available_usd"])
        shortfall = max(0.0, float(cost) - float(budget["available_usd"]))
    return {
        "available": True,
        "found": True,
        "requester": {k: emp[k] for k in ("employee_id", "name", "department", "level", "country")},
        "manager": manager,
        "department": emp["department"],
        "budget": budget,
        "budget_found": budget is not None,
        "requested_annual_cost_usd": cost,
        "within_available_budget": within,
        "shortfall_usd": shortfall,
    }


def search_software_catalog(ctx: RunContext, query: str | None = None, category: str | None = None) -> dict:
    """Deterministic search of the approved catalog + purchase history.

    With no arguments it searches for the requested product, vendor and category.
    The agent may pass its own `query` keywords to look for semantic overlaps.
    """
    req = ctx.request
    catalog = data_access.load_software_catalog()
    history = data_access.load_purchase_history()
    dept = None
    budget_res = ctx.latest("get_requester_budget")
    if budget_res and budget_res.get("found"):
        dept = budget_res["department"]
    else:
        emp = data_access.load_employees()
        m = emp[emp["employee_id"] == req.get("requester_id")]
        dept = None if m.empty else str(m.iloc[0]["department"])

    product = _norm(req.get("product_name"))
    vendor = _norm(req.get("vendor_name"))
    req_category = _norm(category or req.get("category"))
    query_words = _keywords(query) if query else _keywords(req.get("product_name"), req.get("category"))

    matches = []
    for _, r in catalog.iterrows():
        item = _row(r)
        reasons = []
        if product and _norm(item["product_name"]) == product:
            reasons.append("same_product")
        if vendor and _norm(item["vendor_name"]) == vendor:
            reasons.append("same_vendor")
        if req_category and _norm(item["category"]) == req_category:
            reasons.append("same_category")
        overlap_words = query_words & _keywords(item["product_name"], item["category"], item["notes"])
        if overlap_words and not reasons:
            reasons.append("keyword:" + ",".join(sorted(overlap_words)))
        if not reasons:
            continue
        scope = str(item["scope"])
        item["match_reasons"] = reasons
        item["accessible_to_requester_department"] = scope == "Company-wide" or (dept is not None and scope == dept)
        matches.append(item)

    vendor_history = [
        _row(r) for _, r in history.iterrows()
        if vendor and _norm(r["vendor_name"]) == vendor
    ]
    text = _norm(f"{req.get('product_name')} {req.get('business_justification')}")
    return {
        "available": True,
        "requester_department": dept,
        "query": query,
        "matches": matches,
        "purchase_history_for_vendor": vendor_history,
        "request_looks_like_extension": any(k in text for k in EXTENSION_KEYWORDS),
    }


def get_vendor_registry(ctx: RunContext, vendor_name: str | None = None) -> dict:
    """Deterministic: internal vendor registry record with review-age calculation."""
    vendor_name = vendor_name or ctx.request.get("vendor_name")
    vendors = data_access.load_vendors()
    match = vendors[vendors["vendor_name"].str.lower() == _norm(vendor_name)]
    if match.empty:
        return {"available": True, "found": False, "vendor_name": vendor_name,
                "detail": "Vendor is not in the internal registry (treat as new vendor)."}
    rec = _row(match.iloc[0])
    age = review_age_days(rec.get("security_review_date"))
    rec.update({
        "available": True,
        "found": True,
        "reference_date": config.REFERENCE_DATE.isoformat(),
        "review_age_days": age,
        "review_expired": age is not None and age > config.SECURITY_REVIEW_VALID_DAYS,
    })
    return rec


def get_vendor_risk(ctx: RunContext, vendor_name: str | None = None) -> dict:
    """External mock API: vendor security/privacy status. Failures are returned, not raised."""
    vendor_name = vendor_name or ctx.request.get("vendor_name")
    try:
        data = _api_get_vendor_risk(vendor_name)
    except VendorRiskNotFound as exc:
        return {"available": True, "found": False, "vendor_name": vendor_name, "error": str(exc)}
    except VendorRiskUnavailable as exc:
        return {"available": False, "found": None, "vendor_name": vendor_name, "error": str(exc)}
    age = review_age_days(data.get("last_review_date"))
    data.update({
        "available": True,
        "found": True,
        "reference_date": config.REFERENCE_DATE.isoformat(),
        "review_age_days": age,
        "review_expired": age is not None and age > config.SECURITY_REVIEW_VALID_DAYS,
    })
    return data


def check_policy_rules(ctx: RunContext) -> dict:
    """Deterministic policy engine (thresholds, review triggers, missing fields, injection scan).

    Runs any prerequisite evidence tools the agent has not called yet (as guard calls).
    """
    from src.policy_engine import evaluate  # local import avoids a cycle

    ensure_core_evidence(ctx)
    return evaluate(ctx).to_dict()


POLICY_SECTION_RE = re.compile(r"^## (\d+)\. (.+)$", re.MULTILINE)


def get_policy_section(ctx: RunContext, section: str | int | None = None) -> dict:
    """Deterministic retrieval of the policy text (one section, or the index)."""
    text = data_access.load_policy_text()
    heads = list(POLICY_SECTION_RE.finditer(text))
    index = {m.group(1): m.group(2) for m in heads}
    if section is None or str(section).strip() == "":
        return {"available": True, "sections": index}
    wanted = re.sub(r"\D", "", str(section))
    for i, m in enumerate(heads):
        if m.group(1) == wanted:
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return {"available": True, "section": wanted, "title": m.group(2), "text": text[m.start():end].strip()}
    return {"available": True, "error": f"No policy section '{section}'", "sections": index}


# --------------------------------------------------------------------------- registry

TOOLS: dict[str, dict] = {
    "get_requester_budget": {
        "fn": get_requester_budget, "deterministic": True,
        "description": "Requester profile, manager and the department's available software budget compared with the request cost.",
        "args": {"employee_id": "optional; defaults to the request's requester_id"},
    },
    "search_software_catalog": {
        "fn": search_software_catalog, "deterministic": True,
        "description": "Search approved software catalog and purchase history for the same product, vendor, category or keywords.",
        "args": {"query": "optional keywords describing the need", "category": "optional category override"},
    },
    "get_vendor_registry": {
        "fn": get_vendor_registry, "deterministic": True,
        "description": "Internal vendor registry: procurement, security and legal status plus review age at the reference date.",
        "args": {"vendor_name": "optional; defaults to the request's vendor"},
    },
    "get_vendor_risk": {
        "fn": get_vendor_risk, "deterministic": False,
        "description": "External vendor-risk service: risk level, security review status, personal data and data residency. May be unavailable.",
        "args": {"vendor_name": "optional; defaults to the request's vendor"},
    },
    "check_policy_rules": {
        "fn": check_policy_rules, "deterministic": True,
        "description": "Deterministic policy engine: approval thresholds, Security/Privacy/Legal triggers, budget, missing fields, conflicts, injection scan. Authoritative.",
        "args": {},
    },
    "get_policy_section": {
        "fn": get_policy_section, "deterministic": True,
        "description": "Read procurement policy text. Pass a section number (1-11) or nothing for the index.",
        "args": {"section": "optional section number"},
    },
}

CORE_TOOLS = ("get_requester_budget", "search_software_catalog", "get_vendor_registry", "get_vendor_risk")


def run_tool(ctx: RunContext, name: str, args: dict | None = None, caller: str = "agent") -> dict:
    """Execute a tool by name with validated args. Unknown tools/args return an error result."""
    args = dict(args or {})
    spec = TOOLS.get(name)
    start = time.perf_counter()
    if spec is None:
        result = {"available": False, "error": f"Unknown tool '{name}'. Valid tools: {sorted(TOOLS)}"}
    else:
        allowed = set(spec["args"])
        clean_args = {k: v for k, v in args.items() if k in allowed and v not in (None, "")}
        args = clean_args
        try:
            result = spec["fn"](ctx, **clean_args)
        except Exception as exc:  # a tool must never crash the run
            result = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    ctx.calls.append(ToolCallRecord(name, args, result, caller, (time.perf_counter() - start) * 1000))
    return result


def ensure_core_evidence(ctx: RunContext) -> int:
    """Guard: run any core evidence tool the agent skipped. Returns number of guard calls.

    "Skipped" includes calling the tool for a different subject (e.g. another vendor name
    suggested by untrusted request text) - policy is always evaluated on the request's own vendor.
    """
    n = 0
    for name in CORE_TOOLS:
        if core_result(ctx, name) is None:
            run_tool(ctx, name, {}, caller="guard")
            n += 1
    return n


def core_result(ctx: RunContext, name: str) -> dict | None:
    """The result of a core tool for the request's own subject."""
    req = ctx.request
    for c in reversed(ctx.calls):
        if c.name != name:
            continue
        if name in ("get_vendor_registry", "get_vendor_risk"):
            if _norm(c.args.get("vendor_name") or req.get("vendor_name")) == _norm(req.get("vendor_name")):
                return c.result
        elif name == "get_requester_budget":
            if (c.args.get("employee_id") or req.get("requester_id")) == req.get("requester_id"):
                return c.result
        elif name == "search_software_catalog":
            if not c.args:
                return c.result
        else:
            return c.result
    return None
