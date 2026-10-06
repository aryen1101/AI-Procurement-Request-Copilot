from __future__ import annotations

import time

from src import data_access
from src.architectures import run_single_agent, run_staged
from src.contracts import Architecture, ProcurementDecision, RunTelemetry
from src.decision import build_decision
from src.llm import ChatModel, LLMUnavailable, get_chat_model
from src.policy_engine import final_policy
from src.tools import RunContext


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter (called by the public/hidden evaluation harness)."""
    return process_request(data_access.get_request(request_id), architecture)


def process_request(request: dict, architecture: str = "single", llm: ChatModel | None = None) -> ProcurementDecision:
    return run_with_trace(request, architecture, llm)[0]


def run_with_trace(
    request: dict, architecture: str = "single", llm: ChatModel | None = None
) -> tuple[ProcurementDecision, RunContext]:
    """Run one request through an architecture; also returns the tool-call trace (used by the UI).

    architecture: "single" (A), "staged" (B) or "rules" (deterministic-only reference, no LLM).
    If the LLM is unavailable or fails, the run degrades to the deterministic decision and says so
    in telemetry - it never crashes and never assumes a favourable outcome.
    """
    start = time.perf_counter()
    ctx = RunContext(request=dict(request))
    telemetry = RunTelemetry(architecture=architecture, llm_calls=0)
    llm_final = None
    extra_evidence: list[dict] = []
    policy = None

    if architecture != "rules":
        try:
            llm = llm or get_chat_model()
            runner = run_staged if architecture == "staged" else run_single_agent
            llm_final, extra_evidence, policy = runner(llm, ctx)
            if llm_final is None:
                telemetry.llm_errors.append("LLM returned no usable final object")
        except LLMUnavailable as exc:
            telemetry.llm_errors.append(str(exc))
        except Exception as exc:  # malformed model output, unexpected provider response, ...
            telemetry.llm_errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            if llm is not None and hasattr(llm, "stats"):
                telemetry.llm_calls = llm.stats.calls
                telemetry.models_used = sorted(set(llm.stats.models_used))
                telemetry.llm_errors += llm.stats.errors

    if policy is None:
        policy = final_policy(ctx)
    telemetry.mode = "rules_only" if architecture == "rules" else ("llm" if llm_final else "deterministic_fallback")
    decision = build_decision(ctx, policy, llm_final, telemetry, extra_evidence)
    telemetry.latency_ms = round((time.perf_counter() - start) * 1000, 1)
    return decision, ctx
