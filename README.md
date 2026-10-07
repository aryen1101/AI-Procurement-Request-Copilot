# AI Procurement Request Copilot

An internal copilot that takes a software or service purchase request, gathers evidence with tools (budget, software
catalog, vendor registry, external vendor-risk API, policy), applies procurement policy with deterministic code, and
recommends the next action. **Every final decision stays with a human.**

Two architectures are built and compared on the same test set:
**A - single agent** and **B - staged analyst -> policy/risk reviewer**.
LLM: **Groq free tier** (`openai/gpt-oss-120b`, falling back to `qwen/qwen3.8-27b` and `openai/gpt-oss-20b`).

- Diagrams, tools, guardrails, assumptions: **[docs/architecture.md](docs/architecture.md)**
- Decision memo (which architecture to ship): **[docs/architecture_decision.md](docs/architecture_decision.md)**

## Setup and run

Windows PowerShell (macOS/Linux in brackets):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1            # [source .venv/bin/activate]
python -m pip install -r requirements.txt
copy .env.example .env                  # [cp .env.example .env]  then set GROQ_API_KEY (free key: https://console.groq.com/keys)
python verify_setup.py                  # expect PRE-FLIGHT PASSED
python run_local.py                     # one command: mock vendor-risk API on :8001 + UI on http://127.0.0.1:8501
```

Without a `GROQ_API_KEY` everything still runs: the agents fall back to the deterministic policy engine and the
telemetry says so (`mode: deterministic_fallback`).

## Product workflow

1. **Request** - pick one of the 10 dataset requests, or enter a new one in the sidebar form.
2. **Understand + gather evidence** - the agent calls tools. Request text and tool output are treated as untrusted
   data, never as instructions.
3. **Deterministic checks** - approval thresholds, budget, Security/Privacy/Legal triggers, review expiry, conflicting
   vendor evidence, missing fields.
4. **Recommendation** - category, recommendation, next step, required approvals, risk flags, missing information,
   evidence (each item cites the tool it came from), rationale, clarifying questions.
5. **Human review** - the reviewer records forward / request info / reject / escalate (saved to
   `runs/human_decisions.jsonl`). The copilot never acts on its own.

Output contract: `src/contracts.py::ProcurementDecision`. The required fields are unchanged; optional fields added:
`recommendation_category`, `rationale`, `clarifying_questions`, and extended telemetry.

## Architecture (summary)

| | A: single agent | B: staged 2-agent |
|---|---|---|
| LLM roles | one agent: calls tools and decides | Analyst (calls tools, builds an evidence pack) -> Reviewer (no tools, decides) |
| LLM calls per request (design) | ~2 | ~3 |
| Shared by both | 6 tools, policy engine, guardrails, merge step | same |

**Tools (6, of which 5 are deterministic):** `get_requester_budget`, `search_software_catalog`,
`get_vendor_registry`, `check_policy_rules`, `get_policy_section`, plus `get_vendor_risk` (external mock API).

**Who decides what:** code owns thresholds, approvals and risk checks; the LLM interprets (is an overlap real? is
text manipulative?) and explains; a human makes every final decision. The LLM can add approvals and risk flags, but
it can never remove or relax what the policy engine decided, and a stricter category only counts when a risk flag
backs it. LLM evidence must pass a grounding check against real tool output.

## Edge cases covered

| Edge case (from the brief) | Cases | Behaviour |
|---|---|---|
| Incomplete / ambiguous request | PUB-05 | `request_clarification` with specific questions |
| Existing tool already solves the need | PUB-02, EXT-03 | overlap flagged; add-ons to an existing contract are not overlap (PUB-01) |
| Conflicting or expired vendor information | EXT-02 | `conflicting_vendor_evidence` + `vendor_review_expired` -> manual review |
| Security-sensitive request / approval threshold | PUB-03, EXT-01, EXT-05, EXT-09 | specialist reviewers; exact $10,000 boundary; CFO tier |
| Prompt injection inside business data | PUB-05, EXT-08 | flagged and ignored; cannot change approvals |
| Tool / API unavailable | PUB-06, EXT-07 | outage (503) and no record (404) both block a favourable status |

## Evaluation

```powershell
python -m unittest discover -s tests                       # rules on all gold cases + live A/B tests (skipped without a key)
python evals/run_public_evals.py --architecture single     # starter harness, 6 public cases
python evals/run_public_evals.py --architecture staged
python evals/run_comparison.py --delay 20                  # 16 gold cases x {single, staged, rules}
```

The 16 gold-labelled cases are the 6 public cases, the 4 remaining dataset requests, and 6 synthetic edge cases
(see table above). `run_comparison.py` scores every case on correct next action, policy followed (approvals, flags,
missing info), human escalation, grounded evidence and exact approvals (over-escalation), and records latency, LLM
calls and tool calls. Outputs: `evals/results/comparison_summary.md`, `comparison_cases.csv`, `decisions_<arch>.json`.

`--delay 20` spaces the cases out: Groq's free tier allows 8,000 tokens per minute per model, and one case uses
several thousand tokens per LLM call. Latency figures exclude the delay.

### Results

Final run: `python evals/run_comparison.py --delay 20`, 16 cases, Groq free tier (`openai/gpt-oss-120b`,
`qwen/qwen3.8-27b`, `openai/gpt-oss-20b`), reference date 2026-09-30. Full output in
[evals/results/comparison_summary.md](evals/results/comparison_summary.md).

| Metric | A: single agent | B: staged 2-agent | Rules only (no LLM) |
|---|---:|---:|---:|
| Cases passing all quality criteria | **14/16** | 12/16 | 16/16 |
| Correct next action (category) | **15/16** | 13/16 | 16/16 |
| Policy followed (approvals + flags + missing info) | 16/16 | 16/16 | 16/16 |
| Human escalation correct | 16/16 | 16/16 | 16/16 |
| Evidence grounded | **15/16** | 12/16 | 16/16 |
| Approvals exactly match gold | 15/16 | 15/16 | 16/16 |
| Public minimum checks (starter runner) | 6/6 | 6/6 | 6/6 |
| Median latency | **5.6 s** | 182 s | 0.03 s |
| Avg latency | 6.6 s | 178 s | 0.03 s |
| Avg LLM calls | 2.6 | 2.4 | 0 |
| Avg tool calls | 6.0 | 6.0 | 5.0 |
| Ungrounded LLM evidence items dropped | 2 | 18 | 0 |
| Runs that fell back to rules (all models rate limited) | **0** | 9 | - |

**Reading the table**

- **B's numbers are flattered by its fallbacks.** 9 of its 16 runs hit Groq's tokens-per-minute limit on all three
  models and were answered by the rules engine, which is always correct on these cases. On the 7 runs where B's LLM
  did answer, it passed **3/7** and averaged **3.7 LLM calls** (A: 14/16 with 2.6 calls). B's latency is mostly
  rate-limit waiting caused by its extra calls.
- **Safety held everywhere.** Policy followed and human escalation are 16/16 for both: no run removed a required
  approval or approved something it should not.
- **Every failure has the same cause.** The LLM added a factual risk flag that the deterministic tools had already
  checked and found false, which then justified a stricter category:

  | Arch | Case | LLM-added flag | Result |
  |---|---|---|---|
  | A | PUB-05 | `budget_unverified`, `privacy_review_required`, `vendor_review_expired` | extra Finance + Privacy approvals |
  | A | EXT-05 | `vendor_risk_unavailable` (the API had answered) | `manual_review_unverified` instead of `specialist_review` |
  | B | PUB-02, PUB-03, PUB-04 | `conflicting_vendor_evidence` | `manual_review_unverified` instead of specialist / budget review |
  | B | PUB-05 | `conflicting_vendor_evidence`, `privacy_review_required` | extra Privacy approval |

  These failures over-escalate (more human review than needed) rather than under-escalate. The next fix is to let
  the LLM add only judgment flags (security/privacy review, overlap, prompt injection) and not facts that the
  deterministic tools establish.
- The rules-only column is a reference, not an architecture: it shows the floor the merge step guarantees when the
  LLM is unavailable.

## Ship decision

**Architecture A (single agent).** It passed 14/16 against B's 12/16 (and 3/7 when B's LLM actually answered),
with no fallbacks, a 5.6 s median latency, fewer LLM calls and far fewer ungrounded claims. B's extra stage added
cost and latency and made decisions worse. Full reasoning: [docs/architecture_decision.md](docs/architecture_decision.md).

## Starter-pack issues found and fixed

| Issue | Fix |
|---|---|
| Eval runner never started the mock API, so every vendor looked "unavailable" | `src/mock_server.ensure_mock_api()`, used by the runners and the UI |
| `vendor_client` raised the same error for 404 (no record) and 503 (outage) | separate `VendorRiskNotFound` / `VendorRiskUnavailable` |
| Mock API double URL-decoded vendor names (`unquote` after FastAPI decoding) | removed |
| Employee E007's department "Go To Market" has no budget row (latent crash) | handled: `budget_unverified` -> Finance |
| Policy boundary: Legal at >= $10,000, Finance at > $10,000 | encoded exactly and unit-tested |
| `app.py` used deprecated `use_container_width` | UI rewritten |

## Known limitations

- Groq free-tier models are rate limited (tokens per minute per model) and can be deprecated; when every model is
  rate limited the request falls back to the rules engine. Models are configurable in `.env`.
- Free models are not deterministic, so LLM results vary a little between runs.
- The LLM can still add factual risk flags that the tools contradict, causing over-escalation (see Results).
- Gold labels encode one reading of ambiguous policy points and need validation with Procurement, Security and Legal.
- Overlap detection in code is category/vendor based; semantic overlap relies on the LLM.
- 16 evaluation cases is a small set.

No secrets are in the repo: `.env` is git-ignored and `.env.example` is provided.
