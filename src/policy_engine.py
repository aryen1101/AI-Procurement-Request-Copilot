"""Deterministic policy engine - the code half of "AI interprets, CODE enforces, HUMAN approves".

Implements data/procurement_policy.md sections 1-10 as explicit rules. The LLM can add
approvals or flags on top of this result but can never remove them (see src/decision.py).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from src import config
from src.tools import RunContext, core_result, ensure_core_evidence

APPROVAL_ORDER = ["Manager", "Department Head", "Finance", "CFO", "Procurement", "Security", "Privacy", "Legal"]

# Recommendation categories, most restrictive first. The final category is the most
# restrictive of (deterministic baseline, LLM suggestion) - the LLM can escalate, not relax.
CATEGORY_ORDER = [
    "request_clarification",
    "manual_review_unverified",
    "budget_exception_review",
    "specialist_review",
    "reuse_existing_tool",
    "standard_approval",
]

KNOWN_FLAGS = {
    "existing_tool_overlap", "budget_insufficient", "budget_unverified", "security_review_required",
    "privacy_review_required", "legal_review_required", "vendor_review_expired", "conflicting_vendor_evidence",
    "vendor_risk_unavailable", "vendor_risk_record_missing", "prompt_injection_detected", "missing_information",
    "new_vendor", "high_vendor_risk", "cross_region_data", "ai_tool_use_case_review",
}

INJECTION_PATTERNS = [
    r"\bignore\b[^.]{0,40}\b(rules?|instructions?|polic(y|ies)|controls?|guidelines?|checks?)\b",
    r"\b(disregard|bypass|skip|override|circumvent)\b[^.]{0,40}\b(rules?|reviews?|approvals?|polic(y|ies)|controls?|security|checks?)\b",
    r"\b(treat|consider|mark|record)\b[^.]{0,40}\b(as|is)\b[^.]{0,20}\b(pre-?)?approved\b",
    r"\b(already|pre-?)\s*approved\s+by\b",
    r"\b(approve|purchase|buy|sign)\b[^.]{0,25}\b(immediately|right away|right now|without (review|approval))\b",
    r"\b(system prompt|you are now|new instructions|developer mode|act as (an?|the) )\b",
    r"\b(reveal|print|show|expose|send)\b[^.]{0,30}\b(api[ _-]?key|secret|password|credential|system prompt)s?\b",
]

UNKNOWN_VALUES = {"", "unknown", "tbd", "n/a", "na", "none specified", "not sure", "?"}


@dataclass
class Finding:
    rule: str
    detail: str
    section: str
    source: str  # tool whose output justifies the finding


@dataclass
class PolicyResult:
    approvals: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    category: str = "standard_approval"
    recommendation: str = ""
    next_step: str = ""
    injection_hits: list[dict] = field(default_factory=list)
    overlap_candidates: list[dict] = field(default_factory=list)
    approval_tier: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- helpers

def _norm(v: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(v or "").strip().lower())


def is_unknown(v: Any) -> bool:
    return v is None or str(v).strip().lower() in UNKNOWN_VALUES


def data_class(level: Any) -> dict[str, bool]:
    lv = _norm(level)
    return {
        "unknown": is_unknown(level),
        "source_code": "source_code" in lv or ("source" in lv and "code" in lv) or lv in {"code", "repository"},
        "confidential": "confidential" in lv,
        "employee_pii": "employee" in lv and ("pii" in lv or "personal" in lv),
        "customer_pii": "customer" in lv and ("pii" in lv or "personal" in lv),
        "pii": "pii" in lv or "personal" in lv,
        "credentials": "credential" in lv or "secret" in lv,
        "production": "production" in lv or "prod_" in lv,
    }


SECURITY_INTEGRATION_KEYWORDS = (
    "production", "cloud account", "aws", "azure", "gcp", "git", "source code", "code repositor",
    "secret", "vault", "credential", "kubernetes",
)


def scan_injection(fields: dict[str, Any]) -> list[dict]:
    hits = []
    for field_name, value in fields.items():
        text = str(value or "")
        for pat in INJECTION_PATTERNS:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                hits.append({"field": field_name, "match": m.group(0)})
                break
    return hits


def threshold_approvals(cost: float) -> tuple[list[str], str]:
    if cost <= 1_000:
        return ["Manager"], "up to $1,000"
    if cost <= 10_000:
        return ["Department Head", "Procurement"], "$1,000.01 - $10,000"
    if cost <= 25_000:
        return ["Department Head", "Finance", "Procurement"], "$10,000.01 - $25,000"
    return ["Department Head", "Finance", "CFO", "Procurement"], "above $25,000"


def _strip_injection_sentences(text: str, hits: list[dict]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", text or "")
    bad = [h["match"].lower() for h in hits]
    return " ".join(s for s in sentences if not any(b in s.lower() for b in bad))


def money(v: float | None) -> str:
    return "unknown" if v is None else f"${float(v):,.0f}" if float(v).is_integer() else f"${float(v):,.2f}"


# --------------------------------------------------------------------------- engine

def evaluate(ctx: RunContext) -> PolicyResult:
    ensure_core_evidence(ctx)
    req = ctx.request
    res = PolicyResult()
    approvals: set[str] = set()
    flags: set[str] = set()

    budget_res = core_result(ctx, "get_requester_budget") or {}
    catalog_res = core_result(ctx, "search_software_catalog") or {}
    registry = core_result(ctx, "get_vendor_registry") or {}
    risk = core_result(ctx, "get_vendor_risk") or {"available": False, "error": "not called"}

    def add(rule: str, detail: str, section: str, source: str) -> None:
        res.findings.append(Finding(rule, detail, section, source))

    cost = req.get("annual_cost_usd")
    dc = data_class(req.get("data_access_level"))
    integrations = req.get("requested_integrations")
    integ_text = " ".join(str(i) for i in (integrations or [])).lower()

    # ---- 9. Prompt injection (scan all untrusted free text: request + vendor notes) ----
    res.injection_hits = scan_injection({
        "request.product_name": req.get("product_name"),
        "request.business_justification": req.get("business_justification"),
        "request.category": req.get("category"),
        "vendor_registry.notes": registry.get("notes"),
        "vendor_risk.notes": risk.get("notes"),
    })
    if res.injection_hits:
        flags.add("prompt_injection_detected")
        add("prompt_injection", "Embedded instructions ignored as untrusted business data: "
            + "; ".join(f"{h['field']}: \"{h['match']}\"" for h in res.injection_hits), "Policy section 9", "check_policy_rules")

    # ---- 1. Required information ----
    missing = []
    if not budget_res.get("found"):
        missing.append("requester and department (requester not found in employee directory)")
    if is_unknown(req.get("product_name")) or is_unknown(req.get("vendor_name")):
        missing.append("product/vendor")
    if cost is None:
        missing.append("annual cost (or a reasonable annual estimate)")
    if req.get("user_count") is None:
        missing.append("number of users/licenses")
    purpose = _strip_injection_sentences(str(req.get("business_justification") or ""), res.injection_hits)
    if len(re.findall(r"[A-Za-z]{2,}", purpose)) < 5:
        missing.append("business purpose (justification missing or too vague)")
    if dc["unknown"]:
        missing.append("intended data-access level")
    if integrations is None:
        missing.append("required integrations")
    res.missing_information = missing
    if missing:
        flags.add("missing_information")
        add("missing_information", "Material request fields missing: " + "; ".join(missing), "Policy section 1", "check_policy_rules")

    # ---- 4. Financial approval thresholds ----
    if cost is not None:
        tier, label = threshold_approvals(float(cost))
        res.approval_tier = label
        if "Manager" in tier and budget_res.get("found") and not budget_res.get("manager"):
            tier = ["Department Head"]
            add("approval_threshold", "Requester has no manager on record; Manager approval escalated to Department Head.",
                "Policy section 4", "get_requester_budget")
        approvals.update(tier)
        add("approval_threshold", f"Annual amount {money(cost)} falls in tier '{label}' -> {', '.join(tier)}.",
            "Policy section 4", "check_policy_rules")
    else:
        approvals.add("Procurement")
        add("approval_threshold", "Approval tier cannot be determined until an annual cost is provided; Procurement triages.",
            "Policy section 4", "check_policy_rules")

    # ---- 2. Budget ----
    if budget_res.get("found") and not budget_res.get("budget_found"):
        flags.add("budget_unverified")
        approvals.add("Finance")
        add("budget", f"No software budget record for department '{budget_res.get('department')}'; Finance must confirm funding.",
            "Policy section 2", "get_requester_budget")
    elif budget_res.get("within_available_budget") is False:
        flags.add("budget_insufficient")
        approvals.add("Finance")
        b = budget_res["budget"]
        add("budget", f"Cost {money(cost)} exceeds {b['department']} available budget {money(b['available_usd'])} "
            f"(shortfall {money(budget_res['shortfall_usd'])}); Finance budget-exception review.", "Policy section 2", "get_requester_budget")
    elif budget_res.get("within_available_budget") is True:
        b = budget_res["budget"]
        add("budget", f"Cost {money(cost)} is within {b['department']} available budget {money(b['available_usd'])} "
            "(within budget does not imply approval).", "Policy section 2", "get_requester_budget")

    # ---- 3. Existing software / overlap ----
    matches = catalog_res.get("matches") or []
    extension = bool(catalog_res.get("request_looks_like_extension"))
    vendor_n = _norm(req.get("vendor_name"))
    for m in matches:
        reasons = m.get("match_reasons", [])
        strong = "same_product" in reasons or (
            m.get("accessible_to_requester_department") and ("same_category" in reasons or "same_vendor" in reasons))
        is_extension_of_this = extension and _norm(m.get("vendor_name")) == vendor_n
        if strong and not is_extension_of_this:
            res.overlap_candidates.append(m)
        elif strong and is_extension_of_this:
            add("existing_contract", f"Request extends existing {m['product_name']} contract ({m['software_id']}, "
                f"{m['licensed_seats']} seats, scope {m['scope']}).", "Policy section 3", "search_software_catalog")
    if res.overlap_candidates:
        flags.add("existing_tool_overlap")
        desc = "; ".join(f"{m['product_name']} ({m['software_id']}, {m['category']}, {m['licensed_seats']} seats, "
                         f"scope {m['scope']}, status {m['status']})" for m in res.overlap_candidates)
        add("existing_tool_overlap", f"Approved catalog already has: {desc}. Confirm a credible gap before buying.",
            "Policy section 3", "search_software_catalog")

    # ---- vendor status (registry + external risk) ----
    is_new_vendor = not registry.get("found") or str(registry.get("procurement_status", "")).lower() != "approved"
    if is_new_vendor:
        flags.add("new_vendor")
        add("new_vendor", f"Vendor '{req.get('vendor_name')}' is not an approved vendor "
            f"(registry: {registry.get('procurement_status', 'not found')}).", "Policy section 7", "get_vendor_registry")

    reg_approved = str(registry.get("security_status", "")).lower() == "approved"
    reg_current = reg_approved and not registry.get("review_expired") and registry.get("review_age_days") is not None
    security_assessment_ok = reg_current
    if registry.get("found") and registry.get("review_expired"):
        flags.add("vendor_review_expired")
        add("vendor_review_expired", f"Registry security review dated {registry.get('security_review_date')} is "
            f"{registry.get('review_age_days')} days old at {config.REFERENCE_DATE} (> 365).", "Policy section 5", "get_vendor_registry")

    if risk.get("available") is False:
        flags.add("vendor_risk_unavailable")
        security_assessment_ok = False
        add("vendor_risk_unavailable", f"Vendor-risk service could not be reached ({risk.get('error')}); "
            "security/privacy status NOT verified - no favourable status assumed.", "Policy section 10", "get_vendor_risk")
    elif risk.get("found") is False:
        flags.add("vendor_risk_record_missing")
        security_assessment_ok = False
        add("vendor_risk_record_missing", f"Vendor-risk service has no assessment for '{req.get('vendor_name')}'.",
            "Policy section 5", "get_vendor_risk")
    elif risk.get("found"):
        api_status = str(risk.get("security_review_status", "")).lower()
        api_approved = api_status == "approved" and not risk.get("review_expired")
        if api_status == "expired" or risk.get("review_expired"):
            flags.add("vendor_review_expired")
        if not api_approved:
            security_assessment_ok = False
        add("vendor_risk", f"Vendor-risk service: risk={risk.get('risk_level')}, security_review_status={api_status}, "
            f"last_review_date={risk.get('last_review_date')}, processes_personal_data={risk.get('processes_personal_data')}, "
            f"stores_data_outside_region={risk.get('stores_data_outside_region')}.", "Policy section 5", "get_vendor_risk")
        if str(risk.get("risk_level", "")).lower() == "high":
            flags.add("high_vendor_risk")
        # Conflict between registry and external service (section 5: do not silently choose one)
        if registry.get("found"):
            conflicts = []
            if reg_approved != (api_status == "approved"):
                conflicts.append(f"registry security_status={registry.get('security_status')} vs service={api_status}")
            rd, ad = registry.get("security_review_date"), risk.get("last_review_date")
            if rd and ad and str(rd) != str(ad):
                conflicts.append(f"registry review date {rd} vs service {ad}")
            if conflicts:
                flags.add("conflicting_vendor_evidence")
                add("conflicting_vendor_evidence", "Registry and vendor-risk service disagree: " + "; ".join(conflicts)
                    + ". Not resolved automatically.", "Policy section 5", "get_vendor_risk")

    # ---- 5. Security review ----
    security_reasons = []
    if dc["source_code"]:
        security_reasons.append("source code access")
    if dc["confidential"]:
        security_reasons.append("confidential documents")
    if dc["pii"]:
        security_reasons.append("personal data (PII)")
    if dc["credentials"]:
        security_reasons.append("credentials/secrets")
    if dc["production"]:
        security_reasons.append("production data")
    if any(k in integ_text for k in SECURITY_INTEGRATION_KEYWORDS):
        security_reasons.append(f"sensitive integration ({', '.join(integrations or [])})")
    if dc["unknown"]:
        security_reasons.append("data-access level unknown (cannot confirm low sensitivity)")
    if not security_assessment_ok:
        security_reasons.append("vendor security assessment missing, expired, not completed or unverified")
    if "conflicting_vendor_evidence" in flags:
        security_reasons.append("conflicting vendor evidence")
    if security_reasons:
        flags.add("security_review_required")
        approvals.add("Security")
        add("security_review", "Security review required: " + "; ".join(security_reasons) + ".", "Policy section 5", "check_policy_rules")

    # ---- 6. Privacy review ----
    sensitive = dc["pii"] or dc["confidential"] or dc["source_code"] or dc["credentials"]
    outside_region = risk.get("stores_data_outside_region") is True
    privacy_reasons = []
    if dc["pii"]:
        privacy_reasons.append("tool will process employee/customer PII")
    if outside_region and sensitive:
        flags.add("cross_region_data")
        privacy_reasons.append("vendor stores data outside the operating region")
    if privacy_reasons:
        flags.add("privacy_review_required")
        approvals.add("Privacy")
        add("privacy_review", "Privacy review required: " + "; ".join(privacy_reasons) + ".", "Policy section 6", "check_policy_rules")

    # ---- 7. Legal review ----
    legal_reasons = []
    if is_new_vendor and cost is not None and float(cost) >= 10_000:
        legal_reasons.append(f"new vendor with annual spend {money(cost)} (>= $10,000)")
    legal_status = str(registry.get("legal_terms_status", "")).lower()
    if legal_status != "approved":
        legal_reasons.append(f"legal terms not approved/standard (registry: {registry.get('legal_terms_status', 'not found')})")
    if outside_region and sensitive:
        legal_reasons.append("material cross-region data-processing issue")
    if legal_reasons:
        flags.add("legal_review_required")
        approvals.add("Legal")
        add("legal_review", "Legal review required: " + "; ".join(legal_reasons) + ".", "Policy section 7", "check_policy_rules")

    # ---- 8. AI tools ----
    is_ai = "ai" in re.findall(r"[a-z]+", str(req.get("category", "")).lower()) or any(
        "limited" in str(m.get("status", "")).lower() for m in matches)
    if is_ai:
        limited = [m for m in matches if "limited" in str(m.get("status", "")).lower()]
        note = f" Existing approval is limited: {limited[0]['product_name']} ({limited[0]['notes']})." if limited else ""
        add("ai_tool", "AI tool: prior approvals do not extend to new use cases or data classes; normal rules apply." + note,
            "Policy section 8", "search_software_catalog" if limited else "check_policy_rules")
        if limited and (sensitive or dc["unknown"]):
            flags.add("ai_tool_use_case_review")

    # ---- category + recommendation ----
    res.approvals = [a for a in APPROVAL_ORDER if a in approvals]
    res.risk_flags = sorted(flags)
    res.category = baseline_category(flags, missing)
    res.recommendation, res.next_step = render_recommendation(res.category, res.approvals, flags, res.missing_information)
    return res


def final_policy(ctx: RunContext) -> PolicyResult:
    """Authoritative policy result for the decision. Records a guarded `check_policy_rules`
    call if the agent never invoked it, so every evidence source is a real tool call."""
    from src.tools import run_tool

    if "check_policy_rules" not in ctx.tool_names:
        run_tool(ctx, "check_policy_rules", {}, caller="guard")
    return evaluate(ctx)


def baseline_category(flags: set[str], missing: list[str]) -> str:
    if missing:
        return "request_clarification"
    if flags & {"vendor_risk_unavailable", "vendor_risk_record_missing", "conflicting_vendor_evidence"}:
        return "manual_review_unverified"
    if flags & {"budget_insufficient", "budget_unverified"}:
        return "budget_exception_review"
    if flags & {"security_review_required", "privacy_review_required", "legal_review_required"}:
        return "specialist_review"
    if "existing_tool_overlap" in flags:
        return "reuse_existing_tool"
    return "standard_approval"


def render_recommendation(category: str, approvals: list[str], flags: set[str] | list[str], missing: list[str]) -> tuple[str, str]:
    flags = set(flags)
    specialists = [a for a in ("Security", "Privacy", "Legal") if a in approvals]
    business = [a for a in approvals if a not in specialists]
    spec_txt = "/".join(specialists)
    labels = {
        "request_clarification": "Request clarification before review",
        "manual_review_unverified": "Hold for manual review - vendor evidence could not be verified",
        "budget_exception_review": "Route to Finance for budget exception review",
        "specialist_review": f"Route for {spec_txt or 'specialist'} review before business approval",
        "reuse_existing_tool": "Review existing approved tool before buying",
        "standard_approval": f"Proceed to {' + '.join(approvals) or 'business'} approval",
    }
    steps = {
        "request_clarification": "Ask the requester to provide: " + "; ".join(missing) + ". Re-run the review once complete.",
        "manual_review_unverified": f"Security to verify vendor status manually; then {', '.join(approvals)} review the evidence pack.",
        "budget_exception_review": f"Finance to decide on a budget exception or re-scope; in parallel {', '.join(approvals)} review.",
        "specialist_review": f"Send the evidence pack to {spec_txt or 'specialist reviewers'}; after sign-off route to {', '.join(business) or 'business approvers'}.",
        "reuse_existing_tool": "Ask the requester to confirm whether the existing tool meets the need (or document the gap) before Procurement proceeds.",
        "standard_approval": f"Send to {', '.join(approvals)} for approval. No purchase is made by the copilot.",
    }
    step = steps[category]
    if "prompt_injection_detected" in flags:
        step += " Note: embedded instructions in the request were ignored; reviewer should be aware."
    return labels[category], step


def most_restrictive(a: str, b: str | None) -> str:
    if b not in CATEGORY_ORDER:
        return a
    return min(a, b, key=CATEGORY_ORDER.index)
