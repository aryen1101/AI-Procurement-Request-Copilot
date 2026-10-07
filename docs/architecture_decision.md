# Architecture Decision Memo

## Decision
Ship **Architecture A, the single agent**, backed by the deterministic policy engine.

## Evidence
Both architectures ran on the same 16 gold-labelled cases: 6 public, 4 other dataset requests, and 6 synthetic edge
cases ($10,000 boundary, missing budget record, unknown vendor, subtle injection, CFO tier, VP without a manager).
Source: `python evals/run_comparison.py --delay 20` on the Groq free tier; full rows in `evals/results/`.

| Metric | A: single | B: staged | Rules only |
|---|---:|---:|---:|
| Cases passing all criteria | **14/16** | 12/16 | 16/16 |
| Correct next action | **15/16** | 13/16 | 16/16 |
| Policy followed / human escalation | 16/16 | 16/16 | 16/16 |
| Evidence grounded | **15/16** | 12/16 | 16/16 |
| Fell back to rules (429) | **0** | 9 | - |
| Median latency | **5.6 s** | 182 s | 0.03 s |
| *Runs where the LLM answered* | *16* | *7* | - |
| *... passing all criteria* | *14/16* | *3/7* | - |
| *... avg LLM calls* | *2.6* | *3.7* | 0 |
| Ungrounded evidence dropped | 2 | 18 | 0 |

B's 12/16 flatters it: 9 of its passes are the rules fallback. When B's LLM actually answered, it passed 3 of 7.

## Trade-offs
- **B's intended gain:** the reviewer sees a compact evidence pack and the handoff is auditable. In practice the
  reviewer, without tool output in context, made more unsupported claims (18 items dropped by the grounding check vs 2).
- **B's cost:** ~40% more LLM calls and tokens. On an 8,000 tokens-per-minute limit that alone caused 9 fallbacks
  and minutes of latency.
- **A:** one prompt, one loop, one place to debug, with the evidence in context when it decides.

Every failure in both architectures has one cause: the LLM added a factual flag that code had already checked and
found false (`conflicting_vendor_evidence` 4x, `vendor_risk_unavailable`, `budget_unverified`), which justified a
stricter category. No failure approved anything it should not: policy and human escalation were 16/16 for both.

## Risks
- Over-escalation wastes reviewer time. **Next fix:** let the LLM add only judgment flags (security/privacy review,
  overlap, injection), not facts the deterministic tools establish.
- Free models vary in JSON discipline and availability; the rules fallback keeps decisions safe but loses the LLM's
  interpretation.
- Gold labels encode my reading of ambiguous policy points and should be validated with Procurement, Security and
  Legal. 16 cases is small; production needs a larger labelled set and shadow mode against human decisions.

## Why this is the right MVP
The client needs evidence gathering and policy compliance with humans in control. The hard guarantees (thresholds,
review triggers, expired or conflicting evidence, tool outages) are code and unit-tested. The LLM adds judgment on
overlap, manipulative text and explanation. One agent does that better, faster and cheaper than two; B adds cost and
latency and made decisions worse.
