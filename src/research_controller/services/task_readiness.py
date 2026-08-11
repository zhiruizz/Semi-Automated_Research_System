from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from research_controller.db.models import AgentRun, ComputeJob, Task, TaskDependency
from research_controller.domain.enums import AgentRunStatus, ComputeExecutionStatus, TaskStatus
from research_controller.services.transitions import TransitionService


NONTERMINAL_COMPUTE = {
    ComputeExecutionStatus.CREATED,
    ComputeExecutionStatus.SUBMITTED,
    ComputeExecutionStatus.PENDING,
    ComputeExecutionStatus.RUNNING,
    ComputeExecutionStatus.COLLECTING,
}

NONTERMINAL_AGENT = {
    AgentRunStatus.QUEUED,
    AgentRunStatus.STARTING,
    AgentRunStatus.RUNNING,
    AgentRunStatus.WAITING_APPROVAL,
}


class TaskReadinessService:
    def __init__(self, transitions: TransitionService | None = None) -> None:
        self.transitions = transitions or TransitionService()

    def reconcile(self, session: Session, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(timezone.utc)
        candidates = session.scalars(
            select(Task).where(
                Task.status == TaskStatus.PENDING,
                or_(Task.not_before.is_(None), Task.not_before <= current),
            )
        ).all()
        readied: list[str] = []
        for task in candidates:
            dependency_states = session.execute(
                select(Task.status)
                .join(TaskDependency, Task.id == TaskDependency.depends_on_task_id)
                .where(TaskDependency.task_id == task.id)
            ).scalars().all()
            if all(status is TaskStatus.SUCCEEDED for status in dependency_states):
                self.transitions.transition_task(
                    session,
                    task,
                    TaskStatus.READY,
                    expected_status=TaskStatus.PENDING,
                )
                readied.append(task.id)
        return readied

    def recover_expired_leases(
        self, session: Session, *, now: datetime | None = None
    ) -> list[str]:
        current = now or datetime.now(timezone.utc)
        tasks = session.scalars(
            select(Task).where(
                Task.lease_expires_at.is_not(None),
                Task.lease_expires_at <= current,
                Task.status.in_([TaskStatus.READY, TaskStatus.RUNNING]),
            )
        ).all()
        recovered: list[str] = []
        for task in tasks:
            has_compute_work = session.scalar(
                select(
                    exists().where(
                        ComputeJob.task_id == task.id,
                        ComputeJob.execution_status.in_(NONTERMINAL_COMPUTE),
                    )
                )
            )
            has_agent_work = session.scalar(
                select(
                    exists().where(
                        AgentRun.task_id == task.id,
                        AgentRun.status.in_(NONTERMINAL_AGENT),
                    )
                )
            )
            task.lease_owner = None
            task.lease_expires_at = None
            if task.status is TaskStatus.RUNNING and not (has_compute_work or has_agent_work):
                self.transitions.transition_task(
                    session,
                    task,
                    TaskStatus.FAILED,
                    expected_status=TaskStatus.RUNNING,
                    event_type="TASK_LEASE_EXPIRED",
                    payload={"recovery": "retry_without_external_work"},
                )
                if task.attempt_count < task.max_attempts:
                    self.transitions.transition_task(
                        session,
                        task,
                        TaskStatus.READY,
                        expected_status=TaskStatus.FAILED,
                        event_type="TASK_RETRIED",
                    )
            recovered.append(task.id)
        return recovered
