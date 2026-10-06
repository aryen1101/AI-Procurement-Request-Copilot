"""Procurement Request Copilot - Streamlit product UI.

Panels: request details | recommendation + human action | evidence | tool trace & telemetry.
The copilot only recommends; the human decision recorded here is the reviewer's own.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from src import config, data_access
from src.mock_server import ensure_mock_api
from src.solution import run_with_trace

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "runs" / "human_decisions.jsonl"

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")


@st.cache_resource
def _start_api() -> bool:
    ensure_mock_api()
    return True


try:
    _start_api()
except RuntimeError as exc:
    st.warning(f"Vendor-risk API could not be started ({exc}). Vendor checks will show as unavailable.")

REQUESTS = data_access.load_requests()
BY_ID = {r["request_id"]: r for r in REQUESTS}
EMPLOYEES = data_access.load_employees()
VENDORS = data_access.load_vendors()

CATEGORY_STYLE = {
    "standard_approval": ("success", "Standard approval"),
    "reuse_existing_tool": ("info", "Reuse existing tool?"),
    "specialist_review": ("warning", "Specialist review"),
    "budget_exception_review": ("warning", "Budget exception"),
    "manual_review_unverified": ("error", "Manual review - unverified"),
    "request_clarification": ("error", "Clarification needed"),
}

# ----------------------------------------------------------------------------- sidebar
st.sidebar.title("Procurement Copilot")
source = st.sidebar.radio("Request source", ["Existing request", "New request"], horizontal=True)
architecture = st.sidebar.radio(
    "Architecture", ["single", "staged", "rules"], horizontal=True,
    format_func={"single": "A: single", "staged": "B: staged", "rules": "Rules only"}.get,
)
key_set = bool(config.openrouter_api_key()) and config.llm_mode() != "off"
st.sidebar.caption(
    f"LLM: {'OpenRouter ' + config.model_chain()[0] if key_set else 'not configured - deterministic fallback'}\n\n"
    f"Policy reference date: {config.REFERENCE_DATE}"
)

if source == "Existing request":
    rid = st.sidebar.selectbox("Request", list(BY_ID), format_func=lambda r: f"{r} - {BY_ID[r]['product_name']}")
    request = BY_ID[rid]
else:
    with st.sidebar.form("new_request"):
        emp = st.selectbox("Requester", EMPLOYEES["employee_id"].tolist(),
                           format_func=lambda e: f"{e} - {EMPLOYEES.set_index('employee_id').loc[e, 'name']}")
        product = st.text_input("Product", "")
        vendor = st.selectbox("Vendor", VENDORS["vendor_name"].tolist() + ["(other)"])
        other_vendor = st.text_input("Other vendor name (if not listed)", "")
        category = st.text_input("Category", "")
        cost = st.number_input("Annual cost USD (0 = unknown)", min_value=0.0, step=100.0)
        users = st.number_input("Users / licenses (0 = unknown)", min_value=0, step=1)
        justification = st.text_area("Business justification", "")
        data_level = st.selectbox("Data access level", ["none", "internal_documents", "internal_marketing", "confidential_documents",
                                                        "employee_pii", "customer_pii", "source_code", "production_telemetry", "unknown"])
        integrations = st.text_input("Integrations (comma separated)", "")
        submitted = st.form_submit_button("Use this request")
    if submitted or "custom_request" not in st.session_state:
        st.session_state.custom_request = {
            "request_id": "REQ-NEW", "requester_id": emp, "product_name": product,
            "vendor_name": other_vendor.strip() if vendor == "(other)" else vendor, "category": category,
            "annual_cost_usd": cost or None, "user_count": int(users) or None,
            "business_justification": justification, "data_access_level": data_level,
            "requested_integrations": [i.strip() for i in integrations.split(",") if i.strip()], "urgency": "normal",
        }
    request = st.session_state.custom_request

# ----------------------------------------------------------------------------- header
st.title("AI Procurement Request Copilot")
st.caption("Gathers evidence, applies policy rules and recommends the next action. "
           "It never purchases, approves spend or changes budgets - a human decides.")

run_key = f"{request['request_id']}::{architecture}::{json.dumps(request, sort_keys=True)}"
c1, c2 = st.columns([1, 1])
run_clicked = c1.button("Run analysis", type="primary")
compare_clicked = c2.button("Compare A vs B on this request")

if run_clicked:
    with st.spinner(f"Running architecture '{architecture}' ..."):
        st.session_state[run_key] = run_with_trace(request, architecture)

left, right = st.columns([0.9, 1.1], gap="large")

# ----------------------------------------------------------------------------- request details
with left:
    st.subheader("1. Request details")
    emp_row = EMPLOYEES[EMPLOYEES["employee_id"] == request.get("requester_id")]
    who = f"{emp_row.iloc[0]['name']} ({emp_row.iloc[0]['department']})" if not emp_row.empty else "unknown requester"
    cost_txt = "missing" if request.get("annual_cost_usd") is None else f"${request['annual_cost_usd']:,.0f}"
    st.markdown(f"**Request** `{request['request_id']}`  \n**Requester:** {who}")
    m1, m2, m3 = st.columns(3)
    m1.metric("Annual cost", cost_txt)
    m2.metric("Users", request.get("user_count") or "missing")
    m3.metric("Urgency", request.get("urgency") or "-")
    st.markdown("**Product / vendor / category** (untrusted input, shown as plain text)")
    st.text(f"{request.get('product_name')}  |  {request.get('vendor_name')}  |  {request.get('category')}")
    st.markdown("**Business justification** (untrusted input)")
    st.text(request.get("business_justification") or "(empty)")
    st.markdown("**Data access / integrations**")
    st.text(f"{request.get('data_access_level')}  |  {', '.join(request.get('requested_integrations') or []) or 'none'}")

# ----------------------------------------------------------------------------- recommendation
result = st.session_state.get(run_key)
with right:
    st.subheader("2. Recommendation")
    if result is None:
        st.info("Click **Run analysis** to generate a recommendation.")
    else:
        d, ctx = result
        kind, label = CATEGORY_STYLE.get(d.recommendation_category, ("info", d.recommendation_category))
        getattr(st, kind)(f"**{label}** - {d.recommendation}")
        st.markdown(f"**Next step:** {d.next_step}")
        if d.rationale:
            with st.expander("Rationale", expanded=True):
                st.text(d.rationale)
        a, b = st.columns(2)
        a.markdown("**Approvals required**")
        a.markdown("\n".join(f"- {x}" for x in d.required_approvals) or "-")
        b.markdown("**Risk flags**")
        b.markdown("\n".join(f"- `{x}`" for x in d.risk_flags) or "- none")
        if d.missing_information:
            st.markdown("**Missing information**")
            st.markdown("\n".join(f"- {x}" for x in d.missing_information))
        if d.clarifying_questions:
            st.markdown("**Questions for the requester**")
            for q in d.clarifying_questions:
                st.text(f"- {q}")
        t = d.telemetry
        st.caption(f"Architecture {t.architecture} | mode {t.mode} | {t.llm_calls} LLM calls | {t.tool_calls} tool calls "
                   f"({t.guard_tool_calls} guard) | {t.latency_ms:.0f} ms | models {', '.join(t.models_used) or '-'}")
        if t.llm_errors:
            st.caption("LLM notes: " + " | ".join(t.llm_errors)[:300])

        st.subheader("3. Human review")
        st.caption("Human review is always required. The copilot does not act on this decision.")
        with st.form("human_review"):
            choice = st.radio("Reviewer decision", ["Forward to listed approvers", "Request more information",
                                                    "Reject request", "Escalate as exception"])
            reviewer = st.text_input("Reviewer name")
            comment = st.text_area("Comment")
            if st.form_submit_button("Record decision"):
                LOG.parent.mkdir(exist_ok=True)
                with LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "timestamp": datetime.now(timezone.utc).isoformat(), "request_id": d.request_id,
                        "reviewer": reviewer, "decision": choice, "comment": comment,
                        "copilot_category": d.recommendation_category, "copilot_approvals": d.required_approvals,
                        "architecture": t.architecture,
                    }) + "\n")
                st.success(f"Recorded: {choice}")

# ----------------------------------------------------------------------------- evidence + trace
if result is not None:
    d, ctx = result
    st.subheader("4. Evidence")
    ev = pd.DataFrame([e.model_dump() for e in d.evidence])
    st.dataframe(ev, hide_index=True)
    with st.expander(f"Tool trace ({len(ctx.calls)} calls)"):
        for i, c in enumerate(ctx.calls, 1):
            st.markdown(f"**{i}. {c.name}** `{json.dumps(c.args)}` - called by *{c.caller}*, {c.duration_ms:.0f} ms")
            st.json(c.result, expanded=False)
    with st.expander("Raw ProcurementDecision JSON"):
        st.json(d.model_dump())

if compare_clicked:
    st.subheader("Architecture comparison on this request")
    cols = st.columns(2)
    for col, arch in zip(cols, ["single", "staged"]):
        with st.spinner(f"Running {arch} ..."):
            dd, _ = run_with_trace(request, arch)
        t = dd.telemetry
        col.markdown(f"### {'A: single agent' if arch == 'single' else 'B: staged 2-agent'}")
        col.markdown(f"**{dd.recommendation_category}** - {dd.recommendation}")
        col.markdown(f"Approvals: {', '.join(dd.required_approvals)}  \nFlags: {', '.join(dd.risk_flags) or 'none'}")
        col.caption(f"mode {t.mode} | {t.llm_calls} LLM | {t.tool_calls} tools | {t.latency_ms:.0f} ms | "
                    f"{t.ungrounded_evidence_dropped} ungrounded dropped")

st.divider()
st.caption("Recommendations are advisory. Human approval remains required for all purchasing decisions.")
