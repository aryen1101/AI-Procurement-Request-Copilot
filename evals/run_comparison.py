"""Run the SAME gold-labelled case set through each architecture and compare.

    python evals/run_comparison.py                         # single + staged + rules on all 16 cases
    python evals/run_comparison.py --cases public          # only the 6 public cases
    python evals/run_comparison.py --architectures single staged --delay 4

Outputs (evals/results/):
    comparison_cases.csv    one row per (case, architecture) with every metric
    comparison_summary.md   aggregate table used in the README / decision memo
    decisions_<arch>.json   full decision objects for manual review
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run_public_evals import evaluate as public_minimum_checks  # noqa: E402  (same folder)

from src import config, data_access  # noqa: E402
from src.contracts import ProcurementDecision  # noqa: E402
from src.mock_server import ensure_mock_api  # noqa: E402
from src.solution import process_request  # noqa: E402

RESULTS = ROOT / "evals" / "results"


def score(decision: ProcurementDecision, gold: dict) -> dict:
    approvals = set(decision.required_approvals)
    flags = set(decision.risk_flags)
    gold_appr = set(gold["approvals"])
    missing_text = " | ".join(decision.missing_information).lower()

    next_action_correct = decision.recommendation_category == gold["category"]
    approvals_complete = gold_appr <= approvals
    approvals_exact = gold_appr == approvals
    flags_ok = set(gold["flags_required"]) <= flags and not (set(gold["flags_forbidden"]) & flags)
    missing_ok = all(any(tok in missing_text for tok in group) for group in gold["missing_required"]) and \
        len(decision.missing_information) <= gold["max_missing"]
    tel = decision.telemetry
    called = set(tel.tool_names if tel else [])
    grounded = all(e.source in called or e.source == "request" for e in decision.evidence) and \
        (tel.ungrounded_evidence_dropped == 0 if tel else True)
    specialists_needed = gold_appr & {"Finance", "Security", "Privacy", "Legal"}
    escalation_ok = decision.human_review_required and specialists_needed <= approvals
    policy_followed = approvals_complete and flags_ok and missing_ok
    return {
        "correct_next_action": next_action_correct,
        "policy_followed": policy_followed,
        "human_escalation_correct": escalation_ok,
        "grounded_evidence": grounded,
        "approvals_exact": approvals_exact,
        "over_escalation": sorted(approvals - gold_appr),
        "under_escalation": sorted(gold_appr - approvals),
        "flag_problems": sorted((set(gold["flags_required"]) - flags) | (set(gold["flags_forbidden"]) & flags)),
        "passed": next_action_correct and policy_followed and escalation_ok and grounded,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--architectures", nargs="+", default=["single", "staged", "rules"],
                        choices=["single", "staged", "rules"])
    parser.add_argument("--cases", choices=["all", "public", "extended"], default="all")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to sleep between LLM runs (free-tier rate limits)")
    args = parser.parse_args()

    ensure_mock_api()
    cases = json.loads((ROOT / "evals" / "gold_cases.json").read_text(encoding="utf-8"))
    if args.cases != "all":
        cases = [c for c in cases if c["set"] == args.cases]
    public = {c["request_id"]: c for c in json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))}

    RESULTS.mkdir(exist_ok=True)
    rows: list[dict] = []
    print(f"LLM mode={config.llm_mode()}  key set={bool(config.groq_api_key())}  models={config.model_chain()}")
    for arch in args.architectures:
        print(f"\n=== architecture: {arch} ===")
        decisions = []
        for case in cases:
            request = case.get("request") or data_access.get_request(case["request_id"])
            start = time.perf_counter()
            try:
                decision = process_request(request, arch)
                error = ""
            except Exception as exc:  # should not happen - process_request degrades gracefully
                print(f"ERROR {case['case_id']}: {type(exc).__name__}: {exc}")
                continue
            latency = (time.perf_counter() - start) * 1000
            s = score(decision, case["gold"])
            pub = public.get(case.get("request_id", ""))
            pub_fail = public_minimum_checks(decision, pub["expectations"]) if pub else []
            tel = decision.telemetry
            rows.append({
                "case_id": case["case_id"], "set": case["set"], "architecture": arch,
                "mode": tel.mode, "passed": s["passed"],
                "correct_next_action": s["correct_next_action"], "grounded_evidence": s["grounded_evidence"],
                "policy_followed": s["policy_followed"], "human_escalation_correct": s["human_escalation_correct"],
                "approvals_exact": s["approvals_exact"],
                "public_minimum_checks": ("" if not pub else not pub_fail),
                "latency_ms": round(latency, 1), "llm_calls": tel.llm_calls, "tool_calls": tel.tool_calls,
                "guard_tool_calls": tel.guard_tool_calls, "ungrounded_dropped": tel.ungrounded_evidence_dropped,
                "overrides_blocked": tel.llm_overrides_blocked,
                "category": decision.recommendation_category,
                "over_escalation": ";".join(s["over_escalation"]), "under_escalation": ";".join(s["under_escalation"]),
                "flag_problems": ";".join(s["flag_problems"]),
                "llm_errors": " | ".join(tel.llm_errors)[:300], "error": error,
            })
            decisions.append({"case_id": case["case_id"], **decision.model_dump()})
            print(f"{'PASS' if s['passed'] else 'FAIL'}  {case['case_id']:7s} {decision.recommendation_category:26s} "
                  f"{latency:7.0f} ms  llm={tel.llm_calls} tools={tel.tool_calls} mode={tel.mode}"
                  + (f"  over={s['over_escalation']}" if s["over_escalation"] else "")
                  + (f"  under={s['under_escalation']}" if s["under_escalation"] else "")
                  + (f"  flags={s['flag_problems']}" if s["flag_problems"] else ""))
            if arch != "rules" and args.delay:
                time.sleep(args.delay)
        (RESULTS / f"decisions_{arch}.json").write_text(json.dumps(decisions, indent=2), encoding="utf-8")

    if not rows:
        return
    with (RESULTS / "comparison_cases.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    summary = summarise(rows, args.architectures)
    (RESULTS / "comparison_summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    print(f"Wrote {RESULTS.relative_to(ROOT)}/comparison_cases.csv, comparison_summary.md, decisions_*.json")


def summarise(rows: list[dict], archs: list[str]) -> str:
    def pct(rs, key):
        return f"{sum(1 for r in rs if r[key] is True)}/{len(rs)}"

    def avg(rs, key):
        vals = [float(r[key]) for r in rs if r[key] not in ("", None)]
        return f"{statistics.mean(vals):.1f}" if vals else "-"

    names = {"single": "A: single agent", "staged": "B: staged 2-agent", "rules": "Rules only (no LLM, reference)"}
    by = {a: [r for r in rows if r["architecture"] == a] for a in archs}
    metrics = [
        ("Cases passing all quality criteria", lambda rs: pct(rs, "passed")),
        ("Correct next action (category)", lambda rs: pct(rs, "correct_next_action")),
        ("Policy followed (approvals + flags + missing info)", lambda rs: pct(rs, "policy_followed")),
        ("Human escalation correct", lambda rs: pct(rs, "human_escalation_correct")),
        ("Evidence grounded (no ungrounded LLM claims)", lambda rs: pct(rs, "grounded_evidence")),
        ("Approvals exactly match gold (no over-escalation)", lambda rs: pct(rs, "approvals_exact")),
        ("Public minimum checks (starter runner)", lambda rs: f"{sum(1 for r in rs if r['public_minimum_checks'] is True)}/{sum(1 for r in rs if r['public_minimum_checks'] != '')}"),
        ("Avg latency (ms)", lambda rs: avg(rs, "latency_ms")),
        ("Max latency (ms)", lambda rs: f"{max(float(r['latency_ms']) for r in rs):.0f}"),
        ("Avg LLM calls", lambda rs: avg(rs, "llm_calls")),
        ("Avg tool calls", lambda rs: avg(rs, "tool_calls")),
        ("Avg guard tool calls (agent skipped a tool)", lambda rs: avg(rs, "guard_tool_calls")),
        ("Ungrounded LLM evidence items dropped (total)", lambda rs: str(sum(int(r["ungrounded_dropped"]) for r in rs))),
        ("LLM de-escalations blocked (total)", lambda rs: str(sum(int(r["overrides_blocked"]) for r in rs))),
        ("Runs that fell back to rules (LLM failed)", lambda rs: str(sum(1 for r in rs if r["mode"] == "deterministic_fallback"))),
    ]
    out = ["| Metric | " + " | ".join(names[a] for a in archs) + " |",
           "|---|" + "---:|" * len(archs)]
    for label, fn in metrics:
        out.append(f"| {label} | " + " | ".join(fn(by[a]) if by[a] else "-" for a in archs) + " |")
    header = (f"# Architecture comparison\n\nCases: {len(by[archs[0]])} (same set for every architecture). "
              f"Models: {', '.join(config.model_chain())}. Reference date {config.REFERENCE_DATE}.\n\n")
    failures = [f"- {r['architecture']} {r['case_id']}: category={r['category']} over={r['over_escalation'] or '-'} "
                f"under={r['under_escalation'] or '-'} flags={r['flag_problems'] or '-'}"
                for r in rows if not r["passed"]]
    return header + "\n".join(out) + "\n\n## Failing cases\n" + ("\n".join(failures) if failures else "None") + "\n"


if __name__ == "__main__":
    main()
