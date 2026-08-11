from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_controller.agents.gateway import AgentGateway
from research_controller.artifacts.store import ArtifactStore
from research_controller.db.models import AgentRun, Task
from research_controller.domain.enums import AgentRunStatus, TaskStatus
from research_controller.domain.ids import new_id
from research_controller.protocols.agent import (
    AgentOutcome,
    AgentRunView,
    AgentTaskSpec,
)
from research_controller.services.agent_validation import AgentResultValidator
from research_controller.services.transitions import TransitionService


def agent_run_view(run: AgentRun) -> AgentRunView:
    return AgentRunView(
        id=run.id,
        project_id=run.project_id,
        task_id=run.task_id,
        backend=run.backend,
        role=run.role,
        session_id=run.session_id,
        external_run_id=run.external_run_id,
        status=run.status,
        started_at=run.started_at,
        config=run.config_json,
    )


class AgentReconciler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        gateway: AgentGateway,
        artifact_store: ArtifactStore,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.artifact_store = artifact_store
        self.transitions = TransitionService()
        self.validator = AgentResultValidator()

    async def reconcile_starting(self) -> list[str]:
        with self.session_factory() as session:
            run_ids = session.scalars(
                select(AgentRun.id).where(AgentRun.status == AgentRunStatus.STARTING)
            ).all()
        reconciled: list[str] = []
        for run_id in run_ids:
            with self.session_factory() as session:
                run = session.get(AgentRun, run_id)
                if run is None or run.status is not AgentRunStatus.STARTING:
                    continue
                adapter = self.gateway.registry.get(run.backend)
                request = self.gateway.request_for_run(
                    run, Path(run.config_json["workdir"])
                )
            external = await adapter.reconcile(run_id)
            if external is None:
                external = await adapter.start(request)
            with self.session_factory.begin() as session:
                run = session.get(AgentRun, run_id)
                if run is None or run.status is not AgentRunStatus.STARTING:
                    continue
                run.external_run_id = external.external_run_id
                run.session_id = external.session_id
                self.transitions.transition_agent_run(
                    session,
                    run,
                    AgentRunStatus.RUNNING,
                    expected_status=AgentRunStatus.STARTING,
                    correlation_id=new_id("corr"),
                    payload={
                        "external_run_id": external.external_run_id,
                        "reconciled": external.status is not AgentRunStatus.STARTING,
                    },
                )
                reconciled.append(run.id)
        return reconciled

    async def reconcile_due(self) -> list[str]:
        with self.session_factory() as session:
            views = [
                agent_run_view(run)
                for run in session.scalars(
                    select(AgentRun).where(AgentRun.status == AgentRunStatus.RUNNING)
                ).all()
            ]
        observed: list[str] = []
        for view in views:
            adapter = self.gateway.registry.get(view.backend)
            timeout = int(view.config["task_spec"]["execution_policy"]["timeout_sec"])
            if view.started_at is not None:
                started_at = view.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
                if elapsed > timeout:
                    await adapter.cancel(view)
                    self._fail_run(view.id, AgentRunStatus.TIMEOUT, "TIMEOUT", "Agent timeout")
                    observed.append(view.id)
                    continue
            try:
                observation = await adapter.poll(view)
            except Exception as exc:
                # Poll failures are observation failures, not proof that the external run failed.
                with self.session_factory.begin() as session:
                    run = session.get(AgentRun, view.id)
                    if run is not None and run.status is AgentRunStatus.RUNNING:
                        run.error_type = "POLL_ERROR"
                        run.error_message = f"{type(exc).__name__}: {exc}"
                continue
            observed.append(view.id)
            if observation.status is AgentRunStatus.RUNNING:
                with self.session_factory.begin() as session:
                    run = session.get(AgentRun, view.id)
                    if run is not None and run.status is AgentRunStatus.RUNNING:
                        run.heartbeat_at = observation.observed_at
                continue
            if observation.status is AgentRunStatus.FAILED:
                self._fail_run(
                    view.id,
                    AgentRunStatus.FAILED,
                    observation.error_type or "BACKEND_FAILED",
                    observation.error_message or observation.raw_state,
                )
                continue
            if observation.status is AgentRunStatus.SUCCEEDED and observation.result_available:
                await self._collect_result(view, observation.metadata.get("response_path"))
        return observed

    def _fail_run(
        self,
        run_id: str,
        status: AgentRunStatus,
        error_type: str,
        message: str,
    ) -> None:
        with self.session_factory.begin() as session:
            run = session.get(AgentRun, run_id)
            if run is None or run.status is not AgentRunStatus.RUNNING:
                return
            correlation_id = new_id("corr")
            run.error_type = error_type
            run.error_message = message
            self.transitions.transition_agent_run(
                session,
                run,
                status,
                expected_status=AgentRunStatus.RUNNING,
                correlation_id=correlation_id,
                payload={"error_type": error_type},
            )
            task = session.get(Task, run.task_id)
            if task is not None and task.status is TaskStatus.RUNNING:
                task.error_summary = message
                self.transitions.transition_task(
                    session,
                    task,
                    TaskStatus.FAILED,
                    expected_status=TaskStatus.RUNNING,
                    correlation_id=correlation_id,
                )

    async def _collect_result(self, view: AgentRunView, response_path_value: object) -> None:
        run_root = Path(view.config["workdir"]).resolve()
        response_path = Path(str(response_path_value)).resolve()
        if not response_path.is_relative_to(run_root) or not response_path.is_file():
            self._fail_run(
                view.id,
                AgentRunStatus.FAILED,
                "INVALID_RESULT_PATH",
                "raw Agent response path is outside AgentRun workdir or missing",
            )
            return
        correlation_id = new_id("corr")
        with self.session_factory.begin() as session:
            run = session.get(AgentRun, view.id)
            if run is None or run.status is not AgentRunStatus.RUNNING:
                return
            artifact = self.artifact_store.ingest_file(
                session,
                project_id=run.project_id,
                task_id=run.task_id,
                source=response_path,
                logical_name="agent-response.json",
                kind="AGENT_RESPONSE",
                producer_type="AGENT_RUN",
                producer_ref_id=run.id,
                schema_name="agent-result/v0.1",
                correlation_id=correlation_id,
            )
            run.response_artifact_id = artifact.id
        adapter = self.gateway.registry.get(view.backend)
        try:
            result = await adapter.get_result(view)
        except (ValidationError, ValueError, OSError) as exc:
            with self.session_factory.begin() as session:
                run = session.get(AgentRun, view.id)
                if run is None or run.status is not AgentRunStatus.RUNNING:
                    return
                run.error_type = "INVALID_RESULT"
                run.error_message = str(exc)
                self.transitions.events.append(
                    session,
                    project_id=run.project_id,
                    event_type="AGENT_RESULT_INVALID",
                    entity_type="AGENT_RUN",
                    entity_id=run.id,
                    correlation_id=correlation_id,
                    payload={"error_type": type(exc).__name__},
                )
                self.transitions.transition_agent_run(
                    session,
                    run,
                    AgentRunStatus.FAILED,
                    expected_status=AgentRunStatus.RUNNING,
                    correlation_id=correlation_id,
                )
                task = session.get(Task, run.task_id)
                if task is not None and task.status is TaskStatus.RUNNING:
                    task.error_summary = "invalid AgentResult"
                    self.transitions.transition_task(
                        session,
                        task,
                        TaskStatus.FAILED,
                        expected_status=TaskStatus.RUNNING,
                        correlation_id=correlation_id,
                    )
            return

        spec = AgentTaskSpec.model_validate(view.config["task_spec"])
        validation = self.validator.validate(result, spec, allowed_root=run_root)
        for produced in validation.safe_artifacts:
            with self.session_factory.begin() as session:
                self.artifact_store.ingest_file(
                    session,
                    project_id=view.project_id,
                    task_id=view.task_id,
                    source=produced.path,
                    logical_name=produced.logical_name,
                    kind=produced.artifact_kind,
                    producer_type="AGENT_RUN",
                    producer_ref_id=view.id,
                    evidence_eligible=False,
                    metadata={
                        **produced.metadata,
                        "evidence_candidate": produced.evidence_candidate,
                    },
                    correlation_id=correlation_id,
                )
        with self.session_factory.begin() as session:
            run = session.get(AgentRun, view.id)
            if run is None or run.status is not AgentRunStatus.RUNNING:
                return
            run.result_json = {
                "result": result.model_dump(mode="json"),
                "validation_reasons": validation.reasons,
                "policy_violations": validation.policy_violations,
            }
            if validation.policy_violations:
                self.transitions.events.append(
                    session,
                    project_id=run.project_id,
                    event_type="AGENT_POLICY_VIOLATION",
                    entity_type="AGENT_RUN",
                    entity_id=run.id,
                    correlation_id=correlation_id,
                    payload={"violations": validation.policy_violations},
                )
            if result.transition_request is not None:
                self.transitions.events.append(
                    session,
                    project_id=run.project_id,
                    event_type="AGENT_TRANSITION_REQUESTED",
                    entity_type="AGENT_RUN",
                    entity_id=run.id,
                    correlation_id=correlation_id,
                    payload=result.transition_request.model_dump(mode="json"),
                )
            if result.requested_tasks:
                self.transitions.events.append(
                    session,
                    project_id=run.project_id,
                    event_type="AGENT_TASKS_REQUESTED",
                    entity_type="AGENT_RUN",
                    entity_id=run.id,
                    correlation_id=correlation_id,
                    payload={
                        "requests": [item.model_dump(mode="json") for item in result.requested_tasks]
                    },
                )
            if result.protocol_amendment_request is not None:
                self.transitions.events.append(
                    session,
                    project_id=run.project_id,
                    event_type="AGENT_PROTOCOL_AMENDMENT_REQUESTED",
                    entity_type="AGENT_RUN",
                    entity_id=run.id,
                    correlation_id=correlation_id,
                    payload=result.protocol_amendment_request.model_dump(mode="json"),
                )
            self.transitions.transition_agent_run(
                session,
                run,
                AgentRunStatus.SUCCEEDED,
                expected_status=AgentRunStatus.RUNNING,
                correlation_id=correlation_id,
                payload={"outcome": result.outcome.value},
            )
            task = session.get(Task, run.task_id)
            if task is None or task.status is not TaskStatus.RUNNING:
                return
            task.result_summary_json = {
                "agent_outcome": result.outcome.value,
                "summary": result.summary,
                "validation_reasons": validation.reasons,
                "policy_violations": validation.policy_violations,
                "needs_escalation": result.needs_escalation,
                "transition_request": (
                    result.transition_request.model_dump(mode="json")
                    if result.transition_request
                    else None
                ),
                "requested_tasks": [
                    item.model_dump(mode="json") for item in result.requested_tasks
                ],
                "protocol_amendment_request": (
                    result.protocol_amendment_request.model_dump(mode="json")
                    if result.protocol_amendment_request
                    else None
                ),
            }
            if result.outcome is AgentOutcome.BLOCKED:
                task.block_reason = "AGENT_BLOCKED"
                self.transitions.transition_task(
                    session,
                    task,
                    TaskStatus.BLOCKED,
                    expected_status=TaskStatus.RUNNING,
                    correlation_id=correlation_id,
                )
            elif result.outcome is AgentOutcome.FAILED:
                task.error_summary = result.summary
                self.transitions.transition_task(
                    session,
                    task,
                    TaskStatus.FAILED,
                    expected_status=TaskStatus.RUNNING,
                    correlation_id=correlation_id,
                )
            else:
                self.transitions.transition_task(
                    session,
                    task,
                    TaskStatus.VERIFYING,
                    expected_status=TaskStatus.RUNNING,
                    correlation_id=correlation_id,
                )
