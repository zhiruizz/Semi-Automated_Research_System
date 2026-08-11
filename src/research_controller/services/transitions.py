from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from research_controller.db.base import utc_now
from research_controller.db.models import AgentRun, ComputeJob, Task
from research_controller.domain.enums import (
    AgentRunStatus,
    ComputeExecutionStatus,
    ObservationStatus,
    TaskStatus,
)
from research_controller.services.event_log import EventLog


class InvalidTransition(ValueError):
    pass


TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
    ),
    TaskStatus.READY: frozenset({TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.VERIFYING: frozenset(
        {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.BLOCKED}
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.SKIPPED: frozenset(),
}


COMPUTE_TRANSITIONS: dict[ComputeExecutionStatus, frozenset[ComputeExecutionStatus]] = {
    ComputeExecutionStatus.CREATED: frozenset(
        {
            ComputeExecutionStatus.SUBMITTED,
            ComputeExecutionStatus.PENDING,
            ComputeExecutionStatus.RUNNING,
            ComputeExecutionStatus.FAILED,
            ComputeExecutionStatus.CANCELLED,
        }
    ),
    ComputeExecutionStatus.SUBMITTED: frozenset(
        {
            ComputeExecutionStatus.PENDING,
            ComputeExecutionStatus.RUNNING,
            ComputeExecutionStatus.COLLECTING,
            ComputeExecutionStatus.SUCCEEDED,
            ComputeExecutionStatus.FAILED,
            ComputeExecutionStatus.OOM,
            ComputeExecutionStatus.TIMEOUT,
            ComputeExecutionStatus.CANCELLED,
        }
    ),
    ComputeExecutionStatus.PENDING: frozenset(
        {
            ComputeExecutionStatus.RUNNING,
            ComputeExecutionStatus.COLLECTING,
            ComputeExecutionStatus.SUCCEEDED,
            ComputeExecutionStatus.FAILED,
            ComputeExecutionStatus.OOM,
            ComputeExecutionStatus.TIMEOUT,
            ComputeExecutionStatus.CANCELLED,
        }
    ),
    ComputeExecutionStatus.RUNNING: frozenset(
        {
            ComputeExecutionStatus.COLLECTING,
            ComputeExecutionStatus.SUCCEEDED,
            ComputeExecutionStatus.FAILED,
            ComputeExecutionStatus.OOM,
            ComputeExecutionStatus.TIMEOUT,
            ComputeExecutionStatus.CANCELLED,
        }
    ),
    ComputeExecutionStatus.COLLECTING: frozenset(
        {
            ComputeExecutionStatus.SUCCEEDED,
            ComputeExecutionStatus.FAILED,
            ComputeExecutionStatus.OOM,
            ComputeExecutionStatus.TIMEOUT,
            ComputeExecutionStatus.CANCELLED,
        }
    ),
    ComputeExecutionStatus.SUCCEEDED: frozenset(),
    ComputeExecutionStatus.FAILED: frozenset(),
    ComputeExecutionStatus.OOM: frozenset(),
    ComputeExecutionStatus.TIMEOUT: frozenset(),
    ComputeExecutionStatus.CANCELLED: frozenset(),
}


AGENT_RUN_TRANSITIONS: dict[AgentRunStatus, frozenset[AgentRunStatus]] = {
    AgentRunStatus.QUEUED: frozenset(
        {AgentRunStatus.STARTING, AgentRunStatus.CANCELLED}
    ),
    AgentRunStatus.STARTING: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.FAILED,
            AgentRunStatus.TIMEOUT,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.RUNNING: frozenset(
        {
            AgentRunStatus.WAITING_APPROVAL,
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.FAILED,
            AgentRunStatus.TIMEOUT,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.WAITING_APPROVAL: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.FAILED,
            AgentRunStatus.TIMEOUT,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.SUCCEEDED: frozenset(),
    AgentRunStatus.FAILED: frozenset(),
    AgentRunStatus.TIMEOUT: frozenset(),
    AgentRunStatus.CANCELLED: frozenset(),
}


TASK_EVENT_TYPES = {
    TaskStatus.READY: "TASK_READY",
    TaskStatus.RUNNING: "TASK_STARTED",
    TaskStatus.VERIFYING: "TASK_VERIFYING",
    TaskStatus.SUCCEEDED: "TASK_SUCCEEDED",
    TaskStatus.FAILED: "TASK_FAILED",
    TaskStatus.BLOCKED: "TASK_BLOCKED",
    TaskStatus.CANCELLED: "TASK_CANCELLED",
    TaskStatus.SKIPPED: "TASK_SKIPPED",
}


COMPUTE_EVENT_TYPES = {
    ComputeExecutionStatus.SUBMITTED: "COMPUTE_JOB_SUBMITTED",
    ComputeExecutionStatus.PENDING: "COMPUTE_JOB_PENDING",
    ComputeExecutionStatus.RUNNING: "COMPUTE_JOB_STARTED",
    ComputeExecutionStatus.COLLECTING: "COMPUTE_JOB_COLLECTING",
    ComputeExecutionStatus.SUCCEEDED: "COMPUTE_JOB_SUCCEEDED",
    ComputeExecutionStatus.FAILED: "COMPUTE_JOB_FAILED",
    ComputeExecutionStatus.OOM: "COMPUTE_JOB_OOM",
    ComputeExecutionStatus.TIMEOUT: "COMPUTE_JOB_TIMEOUT",
    ComputeExecutionStatus.CANCELLED: "COMPUTE_JOB_CANCELLED",
}


AGENT_RUN_EVENT_TYPES = {
    AgentRunStatus.STARTING: "AGENT_RUN_STARTING",
    AgentRunStatus.RUNNING: "AGENT_RUN_STARTED",
    AgentRunStatus.WAITING_APPROVAL: "AGENT_RUN_WAITING_APPROVAL",
    AgentRunStatus.SUCCEEDED: "AGENT_RUN_SUCCEEDED",
    AgentRunStatus.FAILED: "AGENT_RUN_FAILED",
    AgentRunStatus.TIMEOUT: "AGENT_RUN_TIMEOUT",
    AgentRunStatus.CANCELLED: "AGENT_RUN_CANCELLED",
}


class TransitionService:
    def __init__(self, event_log: EventLog | None = None) -> None:
        self.events = event_log or EventLog()

    def transition_task(
        self,
        session: Session,
        task: Task,
        to_status: TaskStatus,
        *,
        expected_status: TaskStatus | None = None,
        event_type: str | None = None,
        correlation_id: str | None = None,
        actor_id: str | None = None,
        payload: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        old_status = task.status
        if expected_status is not None and old_status != expected_status:
            raise InvalidTransition(
                f"expected Task {task.id} in {expected_status}, found {old_status}"
            )
        if to_status not in TASK_TRANSITIONS[old_status]:
            raise InvalidTransition(f"illegal Task transition: {old_status} -> {to_status}")

        changed_at = now or utc_now()
        task.status = to_status
        task.lock_version += 1
        if to_status is TaskStatus.RUNNING:
            task.started_at = task.started_at or changed_at
            task.attempt_count += 1
        if to_status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
        }:
            task.finished_at = changed_at
            task.lease_owner = None
            task.lease_expires_at = None
        self.events.append(
            session,
            project_id=task.project_id,
            event_type=event_type or TASK_EVENT_TYPES[to_status],
            entity_type="TASK",
            entity_id=task.id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            old_state=old_status.value,
            new_state=to_status.value,
            payload=payload,
        )

    def transition_compute_job(
        self,
        session: Session,
        job: ComputeJob,
        to_status: ComputeExecutionStatus,
        *,
        expected_status: ComputeExecutionStatus | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        old_status = job.execution_status
        if expected_status is not None and old_status != expected_status:
            raise InvalidTransition(
                f"expected ComputeJob {job.id} in {expected_status}, found {old_status}"
            )
        if to_status == old_status:
            return
        if to_status not in COMPUTE_TRANSITIONS[old_status]:
            raise InvalidTransition(f"illegal ComputeJob transition: {old_status} -> {to_status}")

        changed_at = now or utc_now()
        job.execution_status = to_status
        job.lock_version += 1
        if to_status is ComputeExecutionStatus.SUBMITTED:
            job.submitted_at = job.submitted_at or changed_at
        if to_status is ComputeExecutionStatus.RUNNING:
            job.started_at = job.started_at or changed_at
        if to_status in {
            ComputeExecutionStatus.SUCCEEDED,
            ComputeExecutionStatus.FAILED,
            ComputeExecutionStatus.OOM,
            ComputeExecutionStatus.TIMEOUT,
            ComputeExecutionStatus.CANCELLED,
        }:
            job.finished_at = changed_at
        self.events.append(
            session,
            project_id=job.project_id,
            event_type=COMPUTE_EVENT_TYPES[to_status],
            entity_type="COMPUTE_JOB",
            entity_id=job.id,
            correlation_id=correlation_id,
            old_state=old_status.value,
            new_state=to_status.value,
            payload=payload,
        )

    def transition_agent_run(
        self,
        session: Session,
        run: AgentRun,
        to_status: AgentRunStatus,
        *,
        expected_status: AgentRunStatus | None = None,
        event_type: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        old_status = run.status
        if expected_status is not None and old_status is not expected_status:
            raise InvalidTransition(
                f"expected AgentRun {run.id} in {expected_status}, found {old_status}"
            )
        if to_status is old_status:
            return
        if to_status not in AGENT_RUN_TRANSITIONS[old_status]:
            raise InvalidTransition(
                f"illegal AgentRun transition: {old_status} -> {to_status}"
            )

        changed_at = now or utc_now()
        run.status = to_status
        if to_status is AgentRunStatus.RUNNING:
            run.started_at = run.started_at or changed_at
            run.heartbeat_at = changed_at
        if to_status in {
            AgentRunStatus.SUCCEEDED,
            AgentRunStatus.FAILED,
            AgentRunStatus.TIMEOUT,
            AgentRunStatus.CANCELLED,
        }:
            run.finished_at = changed_at
        self.events.append(
            session,
            project_id=run.project_id,
            event_type=event_type or AGENT_RUN_EVENT_TYPES[to_status],
            entity_type="AGENT_RUN",
            entity_id=run.id,
            correlation_id=correlation_id,
            old_state=old_status.value,
            new_state=to_status.value,
            payload=payload,
        )

    def update_observation_status(
        self,
        session: Session,
        job: ComputeJob,
        status: ObservationStatus,
        *,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        old = job.observation_status
        if old == status:
            return
        job.observation_status = status
        job.lock_version += 1
        if status is ObservationStatus.FRESH:
            event_type = "COMPUTE_OBSERVABILITY_RESTORED"
        elif status in {ObservationStatus.UNREACHABLE, ObservationStatus.AUTH_REQUIRED}:
            event_type = "COMPUTE_OBSERVABILITY_LOST"
        else:
            event_type = "COMPUTE_OBSERVATION_CHANGED"
        self.events.append(
            session,
            project_id=job.project_id,
            event_type=event_type,
            entity_type="COMPUTE_JOB",
            entity_id=job.id,
            correlation_id=correlation_id,
            old_state=old.value,
            new_state=status.value,
            payload=payload,
        )
