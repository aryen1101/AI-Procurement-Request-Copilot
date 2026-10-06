from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    source: str = Field(description="Tool/data source name")
    finding: str = Field(description="Concise factual finding")
    reference: str | None = Field(default=None, description="Optional record ID / policy section / endpoint")


class RunTelemetry(BaseModel):
    llm_calls: int | None = None
    tool_calls: int | None = None
    tool_names: list[str] = Field(default_factory=list)
    # Extensions (optional, backwards compatible with the starter contract)
    architecture: str | None = None
    mode: str | None = Field(default=None, description="'llm' or 'deterministic_fallback'")
    models_used: list[str] = Field(default_factory=list)
    guard_tool_calls: int = Field(default=0, description="Tool calls the orchestrator made because the agent skipped them")
    ungrounded_evidence_dropped: int = Field(default=0, description="LLM evidence items rejected by the grounding check")
    llm_overrides_blocked: int = Field(default=0, description="Times the LLM tried to de-escalate below the deterministic baseline")
    llm_errors: list[str] = Field(default_factory=list)
    latency_ms: float | None = None


class ProcurementDecision(BaseModel):
    request_id: str
    recommendation: str = Field(description="Short recommendation label or sentence")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    next_step: str
    human_review_required: bool = True
    telemetry: RunTelemetry | None = None
    # Extensions (optional)
    recommendation_category: str | None = None
    rationale: str | None = None
    clarifying_questions: list[str] = Field(default_factory=list)


Architecture = Literal["single", "staged"]

