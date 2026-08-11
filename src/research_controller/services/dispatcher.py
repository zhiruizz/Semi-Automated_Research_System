from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from research_controller.compute.registry import ProviderRegistry
from research_controller.compute.router import (
    ComputeRouter,
    NoFeasibleComputeRoute,
    RouteFailureReason,
)
from research_controller.db.models import Project, Task
from research_controller.domain.enums import (
    ProjectLifecycle,
    TaskExecutor,
    TaskStatus,
)
from research_controller.domain.ids import new_id
from research_controller.protocols.compute import ComputeTaskSpec
from research_controller.services.project_state import ProjectStateService
from research_controller.services.resource_allocation import LocalGpuAllocationService
from research_controller.services.transitions import TransitionService


LOGGER = logging.getLogger("research_controller.dispatcher")


class ComputeDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: ProviderRegistry,
        *,
        controller_id: str,
        lease_seconds: int = 60,
        retry_seconds: float = 5.0,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.router = ComputeRouter(registry)
        self.controller_id = controller_id
        self.lease_seconds = lease_seconds
        self.retry_seconds = retry_seconds
        self.transitions = TransitionService()
        self.projects = ProjectStateService(self.transitions.events)
        self.allocations = LocalGpuAllocationService(session_factory)

    async def dispatch_ready(self) -> list[str]:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            task_ids = session.scalars(
                select(Task.id)
                .join(Project, Project.id == Task.project_id)
                .where(
                    Task.status == TaskStatus.READY,
                    Task.executor == TaskExecutor.COMPUTE,
                    Project.lifecycle == ProjectLifecycle.ACTIVE,
                    or_(Task.not_before.is_(None), Task.not_before <= now),
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
            try:
                route = await self.router.route(
                    spec,
                    snapshot_overlay=self.allocations.overlay,
                )
            except NoFeasibleComputeRoute as failure:
                retry_at = datetime.now(timezone.utc) + timedelta(
                    seconds=self.retry_seconds
                )
                with self.session_factory.begin() as session:
                    task = session.get(Task, task_id)
                    if task is None or task.status is not TaskStatus.READY:
                        continue
                    if failure.reason is RouteFailureReason.TEMPORARILY_BUSY:
                        task.not_before = retry_at
                        LOGGER.info(
                            "compute dispatch deferred",
                            extra={
                                "project_id": task.project_id,
                                "task_id": task.id,
                                "event_type": "COMPUTE_DISPATCH_DEFERRED",
                                "reason": failure.reason.value,
                            },
                        )
                    else:
                        task.block_reason = "RESOURCE_UNAVAILABLE"
                        self.transitions.transition_task(
                            session,
                            task,
                            TaskStatus.BLOCKED,
                            expected_status=TaskStatus.READY,
                            payload={
                                "route_failure": failure.reason.value,
                                "retryable": failure.retryable,
                                "details": failure.details,
                            },
                        )
                continue
            provider = self.registry.get(route.provider_id)
            prepared = await provider.prepare(spec, route.resource_class)
            correlation_id = new_id("corr")

            with self.session_factory.begin() as session:
                task = session.get(Task, task_id)
                if task is None or task.status is not TaskStatus.READY:
                    continue
                if (
                    self.allocations.is_gpu_resource(route.resource_class)
                    and self.allocations.is_reserved(
                        session, route.provider_id, route.resource_class
                    )
                ):
                    task.not_before = datetime.now(timezone.utc) + timedelta(
                        seconds=self.retry_seconds
                    )
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
                    provider_metadata=route.provider_metadata,
                    correlation_id=correlation_id,
                )
                task.not_before = None
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
