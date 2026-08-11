from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_controller.compute.registry import ProviderRegistry
from research_controller.compute.router import ComputeRouter
from research_controller.db.models import Project, Task
from research_controller.domain.enums import (
    ProjectLifecycle,
    TaskExecutor,
    TaskStatus,
)
from research_controller.domain.ids import new_id
from research_controller.protocols.compute import ComputeTaskSpec
from research_controller.services.project_state import ProjectStateService
from research_controller.services.transitions import TransitionService


class ComputeDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: ProviderRegistry,
        *,
        controller_id: str,
        lease_seconds: int = 60,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.router = ComputeRouter(registry)
        self.controller_id = controller_id
        self.lease_seconds = lease_seconds
        self.transitions = TransitionService()
        self.projects = ProjectStateService(self.transitions.events)

    async def dispatch_ready(self) -> list[str]:
        with self.session_factory() as session:
            task_ids = session.scalars(
                select(Task.id)
                .join(Project, Project.id == Task.project_id)
                .where(
                    Task.status == TaskStatus.READY,
                    Task.executor == TaskExecutor.COMPUTE,
                    Project.lifecycle == ProjectLifecycle.ACTIVE,
                )
                .order_by(Task.priority.desc(), Task.created_at)
            ).all()

        dispatched: list[str] = []
        for task_id in task_ids:
            with self.session_factory() as session:
                task = session.get(Task, task_id)
                if task is None or task.status is not TaskStatus.READY:
                    continue
                spec = ComputeTaskSpec.model_validate(task.spec_json)
            route = await self.router.route(spec)
            provider = self.registry.get(route.provider_id)
            prepared = await provider.prepare(spec, route.resource_class)
            correlation_id = new_id("corr")

            with self.session_factory.begin() as session:
                task = session.get(Task, task_id)
                if task is None or task.status is not TaskStatus.READY:
                    continue
                job = self.projects.create_compute_job(
                    session,
                    task=task,
                    provider_id=route.provider_id,
                    resource_class=route.resource_class,
                    submission_key=spec.submission_key,
                    remote_workdir=str(prepared.workdir),
                    spec=spec.model_dump(mode="json"),
                    resource_snapshot=route.snapshot.model_dump(mode="json"),
                    correlation_id=correlation_id,
                )
                task.lease_owner = self.controller_id
                task.lease_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=self.lease_seconds
                )
                self.transitions.transition_task(
                    session,
                    task,
                    TaskStatus.RUNNING,
                    expected_status=TaskStatus.READY,
                    correlation_id=correlation_id,
                )
                dispatched.append(job.id)
        return dispatched
