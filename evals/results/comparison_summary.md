# Architecture comparison

Cases: 16 (same set for every architecture). Models: openai/gpt-oss-120b, qwen/qwen3.8-27b, openai/gpt-oss-20b. Reference date 2026-09-30.

| Metric | A: single agent | B: staged 2-agent | Rules only (no LLM, reference) |
|---|---:|---:|---:|
| Cases passing all quality criteria | 14/16 | 12/16 | 16/16 |
| Correct next action (category) | 15/16 | 13/16 | 16/16 |
| Policy followed (approvals + flags + missing info) | 16/16 | 16/16 | 16/16 |
| Human escalation correct | 16/16 | 16/16 | 16/16 |
| Evidence grounded (no ungrounded LLM claims) | 15/16 | 12/16 | 16/16 |
| Approvals exactly match gold (no over-escalation) | 15/16 | 15/16 | 16/16 |
| Public minimum checks (starter runner) | 6/6 | 6/6 | 6/6 |
| Avg latency (ms) | 6631.3 | 178060.5 | 25.8 |
| Max latency (ms) | 23861 | 482250 | 31 |
| Avg LLM calls | 2.6 | 2.4 | 0.0 |
| Avg tool calls | 6.0 | 6.0 | 5.0 |
| Avg guard tool calls (agent skipped a tool) | 1.5 | 2.9 | 5.0 |
| Ungrounded LLM evidence items dropped (total) | 2 | 18 | 0 |
| LLM de-escalations blocked (total) | 0 | 1 | 0 |
| Runs that fell back to rules (LLM failed) | 0 | 9 | 0 |

## Failing cases
- single PUB-05: category=request_clarification over=Finance;Privacy under=- flags=-
- single EXT-05: category=manual_review_unverified over=- under=- flags=-
- staged PUB-02: category=manual_review_unverified over=- under=- flags=-
- staged PUB-03: category=manual_review_unverified over=- under=- flags=-
- staged PUB-04: category=manual_review_unverified over=- under=- flags=-
- staged PUB-05: category=request_clarification over=Privacy under=- flags=-
