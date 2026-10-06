"""Architecture A (single agent) and Architecture B (staged analyst -> reviewer).

Both share the same tools, policy engine and merge step, so the comparison isolates the
effect of the orchestration itself.
"""
from __future__ import annotations

from src import prompts
from src.agent_loop import run_tool_loop
from src.llm import ChatModel, extract_json
from src.policy_engine import PolicyResult, final_policy
from src.tools import RunContext


def run_single_agent(llm: ChatModel, ctx: RunContext) -> tuple[dict | None, list[dict], PolicyResult]:
    """A: one agent gathers evidence with tools and returns the decision object."""
    final = run_tool_loop(llm, ctx, prompts.single_agent_system(), prompts.request_message(ctx.request), max_turns=4)
    return final, [], final_policy(ctx)


def run_staged(llm: ChatModel, ctx: RunContext) -> tuple[dict | None, list[dict], PolicyResult]:
    """B: Analyst (tools) builds an evidence pack; Reviewer (no tools) decides from the pack."""
    pack = run_tool_loop(llm, ctx, prompts.analyst_system(), prompts.request_message(ctx.request), max_turns=4) or {}
    policy = final_policy(ctx)

    # Structured handoff: the reviewer sees the analyst's interpretation plus the authoritative
    # deterministic result - never raw instructions from the request.
    handoff = {
        "analyst": {k: pack.get(k) for k in
                    ("request_summary", "key_facts", "overlap_assessment", "prompt_injection_suspected", "concerns", "open_questions")},
        "policy_engine": {
            "required_approvals": policy.approvals,
            "risk_flags": policy.risk_flags,
            "missing_information": policy.missing_information,
            "baseline_category": policy.category,
            "approval_tier": policy.approval_tier,
            "findings": [f"[{f.section} | {f.source}] {f.detail}" for f in policy.findings],
        },
        "tools_called": ctx.tool_names,
    }
    messages = [
        {"role": "system", "content": prompts.reviewer_system()},
        {"role": "user", "content": prompts.reviewer_message(ctx.request, handoff)},
    ]
    reply = llm.chat(messages)
    try:
        obj = extract_json(reply)
    except ValueError:
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": "Reply with ONE valid JSON object only."}]
        try:
            obj = extract_json(llm.chat(messages))
        except ValueError:
            obj = {}
    final = obj.get("final", obj) if isinstance(obj, dict) else None
    if final and pack.get("prompt_injection_suspected") is True:
        final.setdefault("prompt_injection_suspected", True)
    analyst_facts = pack.get("key_facts") if isinstance(pack.get("key_facts"), list) else []
    return (final or None), analyst_facts, policy
