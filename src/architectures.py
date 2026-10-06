"""
Architecture A (single agent).
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


