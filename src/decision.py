"""Turn (deterministic policy result + optional LLM output) into a ProcurementDecision.

Merge rules - the LLM can only make the outcome *safer*:
  * approvals / risk flags  = policy engine  UNION  valid LLM additions
  * category                = most restrictive of policy baseline and LLM suggestion
  * missing information     = policy engine only (prevents invented gaps on complete requests)
  * evidence                = deterministic findings + LLM items that pass the grounding check
  * human_review_required   = always True
"""
from __future__ import annotations

import json
import re

from src.contracts import EvidenceItem, ProcurementDecision, RunTelemetry
from src.policy_engine import (
    APPROVAL_ORDER, CATEGORY_ORDER, KNOWN_FLAGS, PolicyResult, baseline_category, most_restrictive,
    render_recommendation,
)
from src.tools import TOOLS, RunContext, core_result

FLAG_TO_APPROVAL = {
    "security_review_required": "Security",
    "privacy_review_required": "Privacy",
    "legal_review_required": "Legal",
    "budget_insufficient": "Finance",
    "budget_unverified": "Finance",
}
APPROVAL_TO_FLAG = {"Security": "security_review_required", "Privacy": "privacy_review_required", "Legal": "legal_review_required"}

AUTONOMY_CLAIM_RE = re.compile(
    r"\b(has been|have been|is|was|i have|i've|we have)\s+(approved|purchased|signed|bought)\b|\bapproval (is )?granted\b|\bno review (is )?(needed|required)\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


# --------------------------------------------------------------------------- evidence

def deterministic_evidence(ctx: RunContext, policy: PolicyResult) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    b = core_result(ctx, "get_requester_budget") or {}
    if b.get("found"):
        r = b["requester"]
        mgr = b.get("manager")
        items.append(EvidenceItem(
            source="get_requester_budget",
            finding=f"Requester {r['name']} ({r['employee_id']}), {r['department']}, {r['level']}; "
                    f"manager: {mgr['name'] + ' (' + mgr['employee_id'] + ')' if mgr else 'none on record'}.",
            reference=f"employees.csv:{r['employee_id']}"))
    reg = core_result(ctx, "get_vendor_registry") or {}
    if reg.get("found"):
        items.append(EvidenceItem(
            source="get_vendor_registry",
            finding=f"Registry {reg['vendor_id']} {reg['vendor_name']}: procurement={reg['procurement_status']}, "
                    f"security={reg['security_status']} (review {reg.get('security_review_date') or 'none'}), "
                    f"legal terms={reg['legal_terms_status']}. Note: {reg.get('notes')}",
            reference=f"vendors.csv:{reg['vendor_id']}"))
    for f in policy.findings:
        items.append(EvidenceItem(source=f.source, finding=f.detail, reference=f.section))
    return items


def _resolve_source(source: str, ctx: RunContext) -> str | None:
    s = (source or "").lower().replace(" ", "_")
    called = set(ctx.tool_names)
    for name in TOOLS:
        if name in called and (name in s or s in name) and len(s) >= 4:
            return name
    if s.startswith("request"):
        return "request"
    return None


def is_grounded(item: dict, ctx: RunContext) -> EvidenceItem | None:
    """Accept an LLM evidence item only if it cites a tool actually called in this run and every
    significant number (>= 3 digits: costs, dates, seats) appears in that tool's output."""
    if not isinstance(item, dict) or not item.get("finding"):
        return None
    source = _resolve_source(str(item.get("source", "")), ctx)
    if source is None:
        return None
    if source == "request":
        haystack = json.dumps(ctx.request, default=str)
    else:
        haystack = json.dumps([c.result for c in ctx.calls if c.name == source], default=str)
    hay_numbers = {n.replace(",", "").rstrip("0").rstrip(".") for n in NUMBER_RE.findall(haystack)}
    for num in NUMBER_RE.findall(str(item["finding"])):
        clean = num.replace(",", "")
        if len(clean.split(".")[0]) < 3:
            continue
        if clean.rstrip("0").rstrip(".") not in hay_numbers and clean not in hay_numbers:
            return None
    ref = item.get("reference")
    return EvidenceItem(source=source, finding=str(item["finding"])[:400],
                        reference=f"agent: {ref}" if ref else "agent interpretation")


# --------------------------------------------------------------------------- merge

def build_decision(
    ctx: RunContext,
    policy: PolicyResult,
    llm_final: dict | None,
    telemetry: RunTelemetry,
    extra_llm_evidence: list[dict] | None = None,
) -> ProcurementDecision:
    req = ctx.request
    approvals = set(policy.approvals)
    flags = set(policy.risk_flags)
    evidence = deterministic_evidence(ctx, policy)
    llm_category = None
    rationale = None
    questions: list[str] = []
    llm_next = llm_rec = None

    if llm_final:
        llm_category = llm_final.get("recommendation_category")
        for a in llm_final.get("additional_approvals") or []:
            canon = next((x for x in APPROVAL_ORDER if x.lower() == str(a).strip().lower()), None)
            if canon:
                approvals.add(canon)
        # The policy engine already decided whether a same-vendor catalog match is an extension of the
        # existing contract (e.g. extra seats) rather than a competing tool; the LLM may not re-add it.
        vendor = str(req.get("vendor_name") or "").strip().lower()
        overlap = llm_final.get("overlap_assessment") or {}
        existing = str(overlap.get("existing_tool") or "").lower() if isinstance(overlap, dict) else ""
        same_vendor_extension = "existing_tool_overlap" not in policy.risk_flags and (
            any(f.source == "search_software_catalog" and "extends existing" in f.detail for f in policy.findings)
            or (vendor and vendor in existing))
        for fl in llm_final.get("additional_risk_flags") or []:
            if str(fl) in KNOWN_FLAGS and not (fl == "existing_tool_overlap" and same_vendor_extension):
                flags.add(str(fl))
        if llm_final.get("prompt_injection_suspected") is True:
            flags.add("prompt_injection_detected")
        if isinstance(overlap, dict) and overlap.get("overlaps") is True and overlap.get("existing_tool") \
                and not same_vendor_extension:
            flags.add("existing_tool_overlap")
        rationale = llm_final.get("rationale") or None
        questions = [str(q) for q in (llm_final.get("clarifying_questions") or []) if str(q).strip()][:5]
        llm_next, llm_rec = llm_final.get("next_step"), llm_final.get("recommendation")
        llm_items = list(llm_final.get("evidence") or []) + list(extra_llm_evidence or [])
        for item in llm_items[:10]:
            ok = is_grounded(item, ctx)
            if ok is None:
                telemetry.ungrounded_evidence_dropped += 1
            elif not any(ok.finding == e.finding for e in evidence):
                evidence.append(ok)

    # keep flags and approvals consistent with each other
    for fl, appr in FLAG_TO_APPROVAL.items():
        if fl in flags:
            approvals.add(appr)
    for appr, fl in APPROVAL_TO_FLAG.items():
        if appr in approvals:
            flags.add(fl)

    ordered_approvals = [a for a in APPROVAL_ORDER if a in approvals]
    base = baseline_category(flags, policy.missing_information)
    category = most_restrictive(base, llm_category)
    if llm_category in CATEGORY_ORDER and CATEGORY_ORDER.index(llm_category) > CATEGORY_ORDER.index(base):
        telemetry.llm_overrides_blocked += 1

    recommendation, next_step = render_recommendation(category, ordered_approvals, flags, policy.missing_information)
    # Use the LLM's wording only when it agrees with the final category, names every specialist
    # reviewer, and does not claim autonomous action.
    specialists = [a for a in ("Finance", "Security", "Privacy", "Legal") if a in ordered_approvals]
    if llm_category == category and isinstance(llm_next, str) and llm_next.strip():
        if all(s.lower() in llm_next.lower() for s in specialists) and not AUTONOMY_CLAIM_RE.search(llm_next):
            next_step = llm_next.strip()
    if llm_category == category and isinstance(llm_rec, str) and llm_rec.strip() and not AUTONOMY_CLAIM_RE.search(llm_rec):
        recommendation = llm_rec.strip()
    if "prompt_injection_detected" in flags and "ignored" not in next_step.lower():
        next_step += " Embedded instructions in the request were ignored."
    if not questions and policy.missing_information:
        questions = [f"Please provide: {m}" for m in policy.missing_information]
    if rationale is None:
        rationale = " ".join(f.detail for f in policy.findings[:4])

    telemetry.tool_calls = len(ctx.calls)
    telemetry.tool_names = ctx.tool_names
    telemetry.guard_tool_calls = sum(1 for c in ctx.calls if c.caller == "guard")

    return ProcurementDecision(
        request_id=str(req.get("request_id", "REQ-NEW")),
        recommendation=recommendation,
        recommendation_category=category,
        rationale=rationale,
        evidence=evidence,
        required_approvals=ordered_approvals,
        missing_information=list(policy.missing_information),
        risk_flags=sorted(flags),
        clarifying_questions=questions,
        next_step=next_step,
        human_review_required=True,
        telemetry=telemetry,
    )
