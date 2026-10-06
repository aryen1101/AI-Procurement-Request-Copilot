"""Minimal JSON-protocol tool loop shared by the single agent and the staged analyst."""
from __future__ import annotations

import json

from src.llm import ChatModel, extract_json
from src.prompts import tool_results_message
from src.tools import RunContext, run_tool

MAX_TOOL_RESULT_CHARS = 6000
MAX_CALLS_PER_TURN = 8


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _truncate(result: dict) -> dict | str:
    text = json.dumps(result, default=str)
    return result if len(text) <= MAX_TOOL_RESULT_CHARS else text[:MAX_TOOL_RESULT_CHARS] + "...[truncated]"


def run_tool_loop(llm: ChatModel, ctx: RunContext, system: str, user: str, max_turns: int = 4) -> dict | None:
    """Run the agent until it returns {"final": ...}. Returns the final object or None.

    Safety valves: max turns, one JSON-repair retry, and one nudge if the model tries to
    answer without calling any tool.
    """
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    repaired = nudged = False
    for turn in range(max_turns):
        reply = llm.chat(messages)
        messages.append({"role": "assistant", "content": reply})
        try:
            obj = extract_json(reply)
        except ValueError:
            if repaired:
                return None
            repaired = True
            messages.append({"role": "user", "content": "Your reply was not a valid JSON object. Reply with ONE JSON object only."})
            continue

        calls = obj.get("tool_calls")
        if isinstance(calls, list) and calls:
            results = []
            for call in calls[:MAX_CALLS_PER_TURN]:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or call.get("tool") or "")
                args = _parse_args(call.get("args", call.get("arguments", {})))
                result = run_tool(ctx, name, args, caller="agent")
                results.append({"tool": name, "args": args, "result": _truncate(result)})
            last = turn >= max_turns - 2
            messages.append({"role": "user", "content": tool_results_message(results, last_turn=last)})
            continue

        final = obj.get("final", obj)
        if not isinstance(final, dict):
            return None
        if not ctx.calls and not nudged:
            nudged = True
            messages.append({"role": "user", "content": "You have not called any tools. Call the evidence tools first."})
            continue
        return final
    return None
