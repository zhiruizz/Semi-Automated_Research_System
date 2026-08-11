from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from research_controller.db.models import ComputeJob, Project, Task, TaskDependency
from research_controller.domain.enums import (
    ComputeExecutionStatus,
    ProjectLifecycle,
    ProjectPhase,
    ProjectStage,
    TaskExecutor,
    TaskKind,
)
from research_controller.domain.ids import new_id
from research_controller.services.event_log import EventLog


class ProjectStateService:
    def __init__(self, event_log: EventLog | None = None) -> None:
        self.events = event_log or EventLog()

    def create_project(
        self,
        session: Session,
        *,
        slug: str,
        title: str,
        workspace_uri: str,
        brief: str = "",
        lifecycle: ProjectLifecycle = ProjectLifecycle.ACTIVE,
        phase: ProjectPhase = ProjectPhase.EXPERIMENT,
        stage: ProjectStage = ProjectStage.TOY_RUN,
        priority: int = 0,
        budget: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
    ) -> Project:
        project = Project(
            slug=slug,
            title=title,
            workspace_uri=workspace_uri,
            brief=brief,
            lifecycle=lifecycle,
            phase=phase,
            stage=stage,
            priority=priority,
            budget_json=budget or {},
            policy_json=policy or {},
        )
        session.add(project)
        session.flush()
        self.events.append(
            session,
            project_id=project.id,
            event_type="PROJECT_CREATED",
            entity_type="PROJECT",
            entity_id=project.id,
            old_state=None,
            new_state=lifecycle.value,
            dedupe_key=f"project-created:{project.id}",
        )
        return project

    def create_task(
        self,
        session: Session,
        *,
        task_id: str | None = None,
        project_id: str,
        stage: ProjectStage,
        kind: TaskKind,
        action: str,
        executor: TaskExecutor,
        idempotency_key: str,
        spec: dict[str, Any],
        output_spec: dict[str, Any] | None = None,
        acceptance_policy: dict[str, Any] | None = None,
        dependency_ids: Iterable[str] = (),
        priority: int = 0,
        required: bool = True,
        max_attempts: int = 1,
        created_by: str = "controller",
    ) -> Task:
        existing = session.scalar(
            select(Task).where(
                Task.project_id == project_id, Task.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing

        task = Task(
            id=task_id or new_id("tsk"),
            project_id=project_id,
            stage=stage,
            kind=kind,
            action=action,
            executor=executor,
            idempotency_key=idempotency_key,
            spec_json=spec,
            output_spec_json=output_spec or {},
            acceptance_policy_json=acceptance_policy or {},
            priority=priority,
            required=required,
            max_attempts=max_attempts,
            created_by=created_by,
        )
        session.add(task)
        session.flush()
        for dependency_id in dependency_ids:
            dependency = session.get(Task, dependency_id)
            if dependency is None or dependency.project_id != project_id:
                raise ValueError(f"invalid dependency for {task.id}: {dependency_id}")
            session.add(TaskDependency(task_id=task.id, depends_on_task_id=dependency_id))
        self.events.append(
            session,
            project_id=project_id,
            event_type="TASK_CREATED",
            entity_type="TASK",
            entity_id=task.id,
            dedupe_key=f"task-created:{task.id}",
            old_state=None,
            new_state=task.status.value,
            payload={"action": action, "idempotency_key": idempotency_key},
        )
        return task

    def create_compute_job(
        self,
        session: Session,
        *,
        task: Task,
        provider_id: str,
        resource_class: str,
        submission_key: str,
        remote_workdir: str,
        spec: dict[str, Any],
        resource_snapshot: dict[str, Any],
        correlation_id: str,
    ) -> ComputeJob:
        existing = session.scalar(
            select(ComputeJob).where(
                ComputeJob.provider_id == provider_id,
                ComputeJob.submission_key == submission_key,
            )
        )
        if existing is not None:
            return existing
        job = ComputeJob(
            project_id=task.project_id,
            task_id=task.id,
            provider_id=provider_id,
            resource_class=resource_class,
            submission_key=submission_key,
            attempt_no=task.attempt_count + 1,
            remote_workdir=remote_workdir,
            spec_json=spec,
            resource_snapshot_json=resource_snapshot,
        )
        session.add(job)
        session.flush()
        self.events.append(
            session,
            project_id=task.project_id,
            event_type="COMPUTE_JOB_CREATED",
            entity_type="COMPUTE_JOB",
            entity_id=job.id,
            correlation_id=correlation_id,
            dedupe_key=f"compute-created:{provider_id}:{submission_key}",
            old_state=None,
            new_state=job.execution_status.value,
        )
        return job
