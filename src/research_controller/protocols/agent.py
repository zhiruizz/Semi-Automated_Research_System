from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_controller.domain.enums import AgentRunStatus, ProjectStage


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionPolicy(StrEnum):
    NEW = "new"
    RESUME_ROLE = "resume_role"
    FORK_ROLE = "fork_role"
    EPHEMERAL = "ephemeral"


class ContextMode(StrEnum):
    FULL = "FULL"
    SUMMARY = "SUMMARY"
    EXCERPT = "EXCERPT"
    METADATA = "METADATA"
    OMIT = "OMIT"


class AgentOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class ContextReference(StrictModel):
    artifact_id: str
    purpose: str
    required: bool = True
    mode: ContextMode = ContextMode.FULL


class DeliverableSpec(StrictModel):
    logical_name: str
    artifact_kind: str
    required: bool = True
    evidence_candidate: bool = False


class AgentPermissions(StrictModel):
    filesystem_write: bool = False
    local_command: bool = False
    network: bool = False
    compute_submit: bool = False
    request_tasks: bool = False
    request_transition: bool = False
    request_protocol_amendment: bool = False


class AgentExecutionPolicy(StrictModel):
    session_policy: SessionPolicy = SessionPolicy.NEW
    timeout_sec: int = Field(default=3600, ge=1)


class AgentTaskSpec(StrictModel):
    schema_version: Literal["agent-task/v0.1"]
    project_id: str
    task_id: str
    role: str
    objective: str
    instructions: list[str] = Field(default_factory=list)
    context: list[ContextReference] = Field(default_factory=list)
    deliverables: list[DeliverableSpec] = Field(default_factory=list)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    execution_policy: AgentExecutionPolicy = Field(default_factory=AgentExecutionPolicy)

    @model_validator(mode="after")
    def unique_deliverables(self) -> AgentTaskSpec:
        names = [item.logical_name for item in self.deliverables]
        if len(names) != len(set(names)):
            raise ValueError("deliverable logical_name values must be unique")
        return self


class ProducedArtifact(StrictModel):
    logical_name: str
    path: Path
    artifact_kind: str
    evidence_candidate: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class RequestedTask(StrictModel):
    action: str
    role: str
    objective: str
    reason: str
    inputs: list[str] = Field(default_factory=list)


class ProtocolAmendmentRequest(StrictModel):
    reason: str
    proposed_changes: dict[str, Any] = Field(default_factory=dict)


class TransitionRequest(StrictModel):
    schema_version: Literal["transition-request/v0.1"] = "transition-request/v0.1"
    project_id: str
    from_stage: ProjectStage
    to_stage: ProjectStage
    reason: str
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    asserted_preconditions: list[str] = Field(default_factory=list)


class AgentResult(StrictModel):
    schema_version: Literal["agent-result/v0.1"]
    task_id: str
    outcome: AgentOutcome
    summary: str
    artifacts: list[ProducedArtifact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    requested_tasks: list[RequestedTask] = Field(default_factory=list)
    transition_request: TransitionRequest | None = None
    protocol_amendment_request: ProtocolAmendmentRequest | None = None
    needs_escalation: bool = False
    escalation: str | None = None


class DecisionRequest(StrictModel):
    schema_version: Literal["decision-request/v0.1"] = "decision-request/v0.1"
    project_id: str
    decision_type: str
    question: str
    criteria: list[str] = Field(default_factory=list)
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    allowed_decisions: list[str]

    @model_validator(mode="after")
    def decisions_are_unique_and_nonempty(self) -> DecisionRequest:
        if not self.allowed_decisions:
            raise ValueError("allowed_decisions must not be empty")
        if len(self.allowed_decisions) != len(set(self.allowed_decisions)):
            raise ValueError("allowed_decisions must be unique")
        return self


class DecisionResult(StrictModel):
    schema_version: Literal["decision-result/v0.1"] = "decision-result/v0.1"
    decision: str
    confidence: float = Field(ge=0, le=1)
    rationale: str
    evidence_used: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    requested_tasks: list[RequestedTask] = Field(default_factory=list)
    transition_request: TransitionRequest | None = None

    def validate_for(self, request: DecisionRequest) -> None:
        if self.decision not in request.allowed_decisions:
            raise ValueError(
                f"decision {self.decision!r} is not one of {request.allowed_decisions!r}"
            )


class ContextItem(StrictModel):
    artifact_id: str
    purpose: str
    mode: ContextMode
    logical_name: str
    artifact_kind: str
    uri: str
    sha256: str
    content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextPack(StrictModel):
    schema_version: Literal["context-pack/v0.1"] = "context-pack/v0.1"
    project_id: str
    task_id: str
    items: list[ContextItem] = Field(default_factory=list)


class RouteDecision(StrictModel):
    schema_version: Literal["agent-route/v0.1"] = "agent-route/v0.1"
    adapter_id: str
    logical_executor: str
    role: str
    model_tier: str
    session_policy: SessionPolicy
    context_profile: str
    reason: str


class AgentExecutionRequest(StrictModel):
    schema_version: Literal["agent-execution-request/v0.1"] = (
        "agent-execution-request/v0.1"
    )
    run_key: str
    task_spec: AgentTaskSpec
    context_pack: ContextPack
    route: RouteDecision
    session_id: str | None = None
    workdir: Path
    adapter_config: dict[str, Any] = Field(default_factory=dict)


class ExternalAgentRun(StrictModel):
    schema_version: Literal["external-agent-run/v0.1"] = "external-agent-run/v0.1"
    run_key: str
    external_run_id: str
    session_id: str | None = None
    status: AgentRunStatus
    workdir: Path
    launched_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRunView(StrictModel):
    id: str
    project_id: str
    task_id: str
    backend: str
    role: str
    session_id: str | None
    external_run_id: str | None
    status: AgentRunStatus
    started_at: datetime | None
    config: dict[str, Any] = Field(default_factory=dict)


class AgentObservation(StrictModel):
    schema_version: Literal["agent-observation/v0.1"] = "agent-observation/v0.1"
    run_key: str
    external_run_id: str | None
    observed_at: datetime
    status: AgentRunStatus
    raw_state: str
    session_id: str | None = None
    result_available: bool = False
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
