from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from research_controller.agents.context import ContextMissingError
from research_controller.agents.gateway import AgentGateway
from research_controller.artifacts.store import ArtifactStore
from research_controller.db.models import Project, Task
from research_controller.domain.enums import (
    AgentRunStatus,
    ProjectLifecycle,
    TaskExecutor,
    TaskKind,
    TaskStatus,
)
from research_controller.domain.ids import new_id
from research_controller.services.project_state import ProjectStateService
from research_controller.services.transitions import TransitionService


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class AgentDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        gateway: AgentGateway,
        artifact_store: ArtifactStore,
        workspace_root: Path | str,
        *,
        controller_id: str,
        lease_seconds: int = 60,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.artifact_store = artifact_store
        self.workspace_root = Path(workspace_root).resolve()
        self.controller_id = controller_id
        self.lease_seconds = lease_seconds
        self.transitions = TransitionService()
        self.projects = ProjectStateService(self.transitions.events)

    async def dispatch_ready(self) -> list[str]:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            task_ids = session.scalars(
                select(Task.id)
                .join(Project, Project.id == Task.project_id)
                .where(
                    Task.kind == TaskKind.AGENT,
                    Task.executor.in_([TaskExecutor.HERMES, TaskExecutor.CODEX]),
                    Task.status == TaskStatus.READY,
                    Project.lifecycle == ProjectLifecycle.ACTIVE,
                    or_(Task.not_before.is_(None), Task.not_before <= now),
                )
                .order_by(Task.priority.desc(), Task.created_at)
            ).all()
        created: list[str] = []
        for task_id in task_ids:
            try:
                with self.session_factory() as session:
                    task = session.get(Task, task_id)
                    if task is None or task.status is not TaskStatus.READY:
                        continue
                    plan = self.gateway.prepare(session, task)
            except (ValidationError, ValueError, ContextMissingError) as exc:
                with self.session_factory.begin() as session:
                    task = session.get(Task, task_id)
                    if task is None or task.status is not TaskStatus.READY:
                        continue
                    task.block_reason = (
                        "CONTEXT_MISSING"
                        if isinstance(exc, ContextMissingError)
                        else "INVALID_AGENT_SPEC"
                    )
                    task.error_summary = str(exc)
                    self.transitions.transition_task(
                        session,
                        task,
                        TaskStatus.BLOCKED,
                        expected_status=TaskStatus.READY,
                        payload={"reason": task.block_reason},
                    )
                continue
            correlation_id = new_id("corr")
            with self.session_factory.begin() as session:
                task = session.get(Task, task_id)
                if task is None or task.status is not TaskStatus.READY:
                    continue
                run_id = new_id("arun")
                run = self.projects.create_agent_run(
                    session,
                    run_id=run_id,
                    task=task,
                    backend=plan.route.adapter_id,
                    role=plan.spec.role,
                    session_id=plan.session_id,
                    mode=plan.route.session_policy.value,
                    config={
                        "task_spec": plan.spec.model_dump(mode="json"),
                        "context_pack": plan.context_pack.model_dump(mode="json"),
                        "route": plan.route.model_dump(mode="json"),
                        "session_id": plan.session_id,
                        "logical_executor": task.executor.value.lower(),
                        "model_tier": plan.route.model_tier,
                        "mock": plan.adapter_config,
                        "workdir": str(
                            self.workspace_root / ".mock-agent" / "runs" / run_id
                        ),
                    },
                    correlation_id=correlation_id,
                )
                request_path = self.workspace_root / ".agent-controller" / "requests" / f"{run.id}.json"
                _atomic_json(
                    request_path,
                    {
                        "schema_version": "agent-request-record/v0.1",
                        "agent_task_spec": plan.spec.model_dump(mode="json"),
                        "context_pack": plan.context_pack.model_dump(mode="json"),
                        "route_decision": plan.route.model_dump(mode="json"),
                        "session_id": plan.session_id,
                    },
                )
                artifact = self.artifact_store.ingest_file(
                    session,
                    project_id=task.project_id,
                    task_id=task.id,
                    source=request_path,
                    logical_name="agent-request.json",
                    kind="AGENT_REQUEST",
                    producer_type="AGENT_RUN",
                    producer_ref_id=run.id,
                    schema_name="agent-request-record/v0.1",
                    correlation_id=correlation_id,
                )
                run.request_artifact_id = artifact.id
                self.transitions.transition_agent_run(
                    session,
                    run,
                    AgentRunStatus.STARTING,
                    expected_status=AgentRunStatus.QUEUED,
                    correlation_id=correlation_id,
                )
                task.not_before = None
                task.lease_owner = self.controller_id
                task.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
                self.transitions.transition_task(
                    session,
                    task,
                    TaskStatus.RUNNING,
                    expected_status=TaskStatus.READY,
                    correlation_id=correlation_id,
                )
                created.append(run.id)
        return created
