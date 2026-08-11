from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_controller.domain.enums import (
    AccessMode,
    AccessStatus,
    ComputeExecutionStatus,
    FailureClass,
    HealthLevel,
    ObservationStatus,
    ResourceState,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionSpec(StrictModel):
    command: list[str] = Field(default_factory=list)
    entrypoint_artifact_id: str | None = None
    argv: list[str] = Field(default_factory=list)
    mode: Literal["native"] = "native"
    environment_ref: str = "local-default"
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_entrypoint(self) -> ExecutionSpec:
        if not self.command and not self.entrypoint_artifact_id:
            raise ValueError("execution requires command or entrypoint_artifact_id")
        return self


class ResourceRequirements(StrictModel):
    gpu_count: int = Field(default=0, ge=0)
    min_gpu_memory_gb: float = Field(default=0, ge=0)
    cpu_cores: int = Field(default=1, ge=1)
    memory_gb: float = Field(default=1, gt=0)
    walltime_sec: int = Field(default=3600, ge=1)
    required_capabilities: list[str] = Field(default_factory=list)


class OutputSpec(StrictModel):
    logical_name: str
    glob: str
    required: bool = True
    artifact_kind: str
    evidence_candidate: bool = False


class RoutingSpec(StrictModel):
    strategy: Literal["first_feasible"] = "first_feasible"
    allowed_providers: list[str] = Field(default_factory=lambda: ["local"])
    allow_manual_provider: bool = False


class SuccessSpec(StrictModel):
    require_zero_exit_code: bool = True
    required_validators: list[str] = Field(default_factory=lambda: ["exit_code_zero"])


class ComputeTaskSpec(StrictModel):
    schema_version: Literal["compute-task-spec/v0.1"]
    project_id: str
    task_id: str
    submission_key: str
    execution: ExecutionSpec
    resources: ResourceRequirements
    outputs: list[OutputSpec] = Field(default_factory=list)
    routing: RoutingSpec = Field(default_factory=RoutingSpec)
    success: SuccessSpec = Field(default_factory=SuccessSpec)


class ProviderHealth(StrictModel):
    schema_version: Literal["provider-health/v0.1"] = "provider-health/v0.1"
    provider_id: str
    checked_at: datetime
    valid_until: datetime
    level: HealthLevel
    access: AccessStatus
    transport: str
    scheduler: str
    storage: str
    can_submit: bool
    can_poll: bool
    can_collect: bool
    human_action_required: bool = False
    human_action_code: str | None = None
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceOffer(StrictModel):
    resource_class: str
    resource_state: ResourceState
    operational: bool
    schedulable: bool
    gpu_model: str | None = None
    gpu_memory_gb: float | None = None
    gpu_per_node: int = 0
    idle_nodes: int = 0
    allocated_nodes: int = 0
    total_nodes: int = 1
    queue_depth: int = 0
    access_mode: AccessMode = AccessMode.AUTOMATIC
    capabilities: list[str] = Field(default_factory=list)
    provider_score_bias: float = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceSnapshot(StrictModel):
    schema_version: Literal["resource-snapshot/v0.1"] = "resource-snapshot/v0.1"
    provider_id: str
    observed_at: datetime
    valid_until: datetime
    offers: list[ResourceOffer]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactCandidate(StrictModel):
    logical_name: str
    path: Path
    artifact_kind: str
    required: bool = False
    evidence_candidate: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreparedJob(StrictModel):
    provider_id: str
    submission_key: str
    resource_class: str
    workdir: Path
    command: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    outputs: list[OutputSpec] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ComputeJobView(StrictModel):
    id: str
    project_id: str
    task_id: str
    provider_id: str
    resource_class: str
    submission_key: str
    external_job_id: str | None
    remote_workdir: Path
    execution_status: ComputeExecutionStatus
    observation_status: ObservationStatus
    log_cursor: dict[str, Any] = Field(default_factory=dict)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class JobObservation(StrictModel):
    schema_version: Literal["job-observation/v0.1"] = "job-observation/v0.1"
    provider_id: str
    compute_job_id: str
    external_job_id: str | None
    observed_at: datetime
    observation_status: ObservationStatus
    execution_status: ComputeExecutionStatus
    raw_scheduler_state: str | None = None
    exit_code: int | None = None
    failure_class: FailureClass = FailureClass.NONE
    retryable: bool = False
    progress: dict[str, Any] = Field(default_factory=dict)
    log_deltas: dict[str, str] = Field(default_factory=dict)
    log_cursor: dict[str, Any] = Field(default_factory=dict)
    anomalies: list[str] = Field(default_factory=list)
    artifact_candidates: list[ArtifactCandidate] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
