from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from research_controller.db.base import Base, utc_now
from research_controller.domain.enums import (
    AgentRunStatus,
    ArtifactIntegrityStatus,
    ComputeExecutionStatus,
    FailureClass,
    ObservationStatus,
    ProjectLifecycle,
    ProjectPhase,
    ProjectStage,
    TaskExecutor,
    TaskKind,
    TaskStatus,
)
from research_controller.domain.ids import new_id


def enum_column(enum_type: type, **kwargs: Any) -> Any:
    return mapped_column(SqlEnum(enum_type, native_enum=False, validate_strings=True), **kwargs)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("prj"))
    slug: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    brief: Mapped[str] = mapped_column(Text, default="", nullable=False)
    workspace_uri: Mapped[str] = mapped_column(Text, nullable=False)

    lifecycle: Mapped[ProjectLifecycle] = enum_column(
        ProjectLifecycle, default=ProjectLifecycle.QUEUED, nullable=False, index=True
    )
    phase: Mapped[ProjectPhase] = enum_column(
        ProjectPhase, default=ProjectPhase.IDEATION, nullable=False
    )
    stage: Mapped[ProjectStage] = enum_column(
        ProjectStage, default=ProjectStage.LITERATURE_REVIEW, nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    active_proposal_artifact_id: Mapped[str | None] = mapped_column(String(80))
    active_contract_artifact_id: Mapped[str | None] = mapped_column(String(80))
    active_paper_artifact_id: Mapped[str | None] = mapped_column(String(80))
    protocol_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    protocol_frozen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paper_review_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    budget_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    event_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    tasks: Mapped[list[Task]] = relationship(back_populates="project")


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TaskArtifact(Base):
    __tablename__ = "task_artifacts"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), primary_key=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        Index("ix_tasks_dispatch", "status", "not_before", "priority"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("tsk"))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    contract_item_key: Mapped[str | None] = mapped_column(String(300))

    stage: Mapped[ProjectStage] = enum_column(ProjectStage, nullable=False)
    kind: Mapped[TaskKind] = enum_column(TaskKind, nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    executor: Mapped[TaskExecutor] = enum_column(TaskExecutor, nullable=False)
    status: Mapped[TaskStatus] = enum_column(
        TaskStatus, default=TaskStatus.PENDING, nullable=False, index=True
    )
    block_reason: Mapped[str | None] = mapped_column(String(300))

    required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    acceptance_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retry_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    escalation_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    routing_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(500), nullable=False)

    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    result_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(200), default="controller", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    project: Mapped[Project] = relationship(back_populates="tasks")
    compute_jobs: Mapped[list[ComputeJob]] = relationship(back_populates="task")
    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="task")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("project_id", "seq"),
        UniqueConstraint("project_id", "dedupe_key"),
        Index("ix_events_entity", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("evt"))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(200))
    correlation_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    causation_event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id"))
    dedupe_key: Mapped[str | None] = mapped_column(String(500))
    old_state: Mapped[str | None] = mapped_column(String(120))
    new_state: Mapped[str | None] = mapped_column(String(120))
    severity: Mapped[str] = mapped_column(String(30), default="INFO", nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("task_id", "attempt_no"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("arun"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    backend: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str | None] = mapped_column(String(200))
    session_id: Mapped[str | None] = mapped_column(String(300))
    external_run_id: Mapped[str | None] = mapped_column(String(300))
    mode: Mapped[str | None] = mapped_column(String(50))
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AgentRunStatus] = enum_column(
        AgentRunStatus, default=AgentRunStatus.QUEUED, nullable=False
    )
    request_artifact_id: Mapped[str | None] = mapped_column(String(80))
    response_artifact_id: Mapped[str | None] = mapped_column(String(80))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    task: Mapped[Task] = relationship(back_populates="agent_runs")


class ComputeJob(Base):
    __tablename__ = "compute_jobs"
    __table_args__ = (
        UniqueConstraint("provider_id", "submission_key"),
        UniqueConstraint("task_id", "attempt_no"),
        Index("ix_compute_jobs_poll", "execution_status", "next_poll_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("job"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    provider_id: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_class: Mapped[str] = mapped_column(String(100), nullable=False)
    submission_key: Mapped[str] = mapped_column(String(500), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    external_job_id: Mapped[str | None] = mapped_column(String(300))
    remote_workdir: Mapped[str] = mapped_column(Text, nullable=False)
    execution_status: Mapped[ComputeExecutionStatus] = enum_column(
        ComputeExecutionStatus, default=ComputeExecutionStatus.CREATED, nullable=False, index=True
    )
    observation_status: Mapped[ObservationStatus] = enum_column(
        ObservationStatus, default=ObservationStatus.FRESH, nullable=False
    )
    resource_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    provider_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    failure_class: Mapped[FailureClass] = enum_column(
        FailureClass, default=FailureClass.NONE, nullable=False
    )
    log_paths_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    log_cursor_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    runtime_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    submission_script_artifact_id: Mapped[str | None] = mapped_column(String(80))
    stdout_artifact_id: Mapped[str | None] = mapped_column(String(80))
    stderr_artifact_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    task: Mapped[Task] = relationship(back_populates="compute_jobs")


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("project_id", "logical_name", "version"),
        UniqueConstraint(
            "project_id", "producer_type", "producer_ref_id", "logical_name", "sha256"
        ),
        Index("ix_artifacts_evidence", "project_id", "evidence_eligible"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("art"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    logical_name: Mapped[str] = mapped_column(String(500), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(80), default="local_cas", nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    producer_type: Mapped[str] = mapped_column(String(80), nullable=False)
    producer_ref_id: Mapped[str] = mapped_column(String(80), nullable=False)
    parent_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    integrity_status: Mapped[ArtifactIntegrityStatus] = enum_column(
        ArtifactIntegrityStatus, default=ArtifactIntegrityStatus.PENDING, nullable=False
    )
    schema_name: Mapped[str | None] = mapped_column(String(200))
    evidence_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(Artifact, "before_update")
def prevent_artifact_identity_mutation(_mapper: Any, _connection: Any, target: Artifact) -> None:
    immutable_fields = {
        "project_id",
        "task_id",
        "kind",
        "logical_name",
        "version",
        "storage_backend",
        "uri",
        "mime_type",
        "size_bytes",
        "sha256",
        "producer_type",
        "producer_ref_id",
        "parent_artifact_id",
        "created_at",
    }
    state = inspect(target)
    changed = sorted(
        field for field in immutable_fields if state.attrs[field].history.has_changes()
    )
    if changed:
        raise ValueError(f"Artifact is immutable; attempted to change: {', '.join(changed)}")
