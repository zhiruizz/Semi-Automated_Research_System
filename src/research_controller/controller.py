from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_controller.artifacts.store import ArtifactStore
from research_controller.agents.gateway import AgentGateway
from research_controller.agents.mock_adapter import MockAgentAdapter
from research_controller.agents.registry import AgentAdapterRegistry
from research_controller.compute.local.provider import LocalProvider
from research_controller.compute.registry import ProviderRegistry
from research_controller.db.models import Task
from research_controller.domain.enums import TaskExecutor, TaskKind, TaskStatus
from research_controller.domain.ids import new_id
from research_controller.services.dispatcher import ComputeDispatcher
from research_controller.services.agent_dispatcher import AgentDispatcher
from research_controller.services.agent_reconciler import AgentReconciler
from research_controller.services.reconciler import ComputeReconciler
from research_controller.services.task_readiness import TaskReadinessService
from research_controller.services.transitions import TransitionService
from research_controller.services.validators import TaskValidator


LOGGER = logging.getLogger("research_controller")


@dataclass(frozen=True)
class TickResult:
    observed_jobs: int = 0
    observed_agent_runs: int = 0
    recovered_tasks: int = 0
    reconciled_submissions: int = 0
    reconciled_agent_starts: int = 0
    readied_tasks: int = 0
    verified_tasks: int = 0
    dispatched_jobs: int = 0
    dispatched_agent_runs: int = 0


class ResearchController:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        workspace_root: Path | str,
        *,
        registry: ProviderRegistry | None = None,
        agent_registry: AgentAdapterRegistry | None = None,
        controller_id: str | None = None,
        poll_interval_seconds: float = 0.1,
        dispatch_retry_seconds: float = 5.0,
    ) -> None:
        self.session_factory = session_factory
        self.workspace_root = Path(workspace_root).resolve()
        self.controller_id = controller_id or new_id("controller")
        self.registry = registry or ProviderRegistry(
            [LocalProvider(self.workspace_root / ".local-provider")]
        )
        self.artifacts = ArtifactStore(self.workspace_root)
        self.agent_registry = agent_registry or AgentAdapterRegistry(
            [MockAgentAdapter(self.workspace_root / ".mock-agent")]
        )
        self.agent_gateway = AgentGateway(self.agent_registry)
        self.transitions = TransitionService()
        self.readiness = TaskReadinessService(self.transitions)
        self.dispatcher = ComputeDispatcher(
            session_factory,
            self.registry,
            controller_id=self.controller_id,
            retry_seconds=dispatch_retry_seconds,
        )
        self.reconciler = ComputeReconciler(
            session_factory,
            self.registry,
            self.artifacts,
            poll_interval_seconds=poll_interval_seconds,
        )
        self.agent_dispatcher = AgentDispatcher(
            session_factory,
            self.agent_gateway,
            self.artifacts,
            self.workspace_root,
            controller_id=self.controller_id,
        )
        self.agent_reconciler = AgentReconciler(
            session_factory,
            self.agent_gateway,
            self.artifacts,
        )
        self.validator = TaskValidator()

    async def tick(self) -> TickResult:
        # Existing external work always comes first.
        observed = await self.reconciler.reconcile_due()
        observed_agents = await self.agent_reconciler.reconcile_due()

        with self.session_factory.begin() as session:
            recovered = self.readiness.recover_expired_leases(session)

        reconciled = await self.reconciler.reconcile_created()
        reconciled_agents = await self.agent_reconciler.reconcile_starting()

        with self.session_factory.begin() as session:
            readied = self.readiness.reconcile(session)

        verified = self.verify_completed_work()

        # Phase 1 has no scientific stage gates; this is the deterministic hook.
        self.evaluate_project_transitions()

        dispatched = await self.dispatcher.dispatch_ready()
        dispatched_agents = await self.agent_dispatcher.dispatch_ready()
        result = TickResult(
            observed_jobs=len(observed),
            observed_agent_runs=len(observed_agents),
            recovered_tasks=len(recovered),
            reconciled_submissions=len(reconciled),
            reconciled_agent_starts=len(reconciled_agents),
            readied_tasks=len(readied),
            verified_tasks=len(verified),
            dispatched_jobs=len(dispatched),
            dispatched_agent_runs=len(dispatched_agents),
        )
        LOGGER.info(
            "controller tick completed: %s",
            result,
            extra={"event_type": "CONTROLLER_TICK"},
        )
        return result

    def verify_completed_work(self) -> list[str]:
        verified: list[str] = []
        with self.session_factory.begin() as session:
            tasks = session.scalars(
                select(Task).where(
                    Task.status == TaskStatus.VERIFYING,
                    Task.executor == TaskExecutor.COMPUTE,
                )
            ).all()
            for task in tasks:
                result = self.validator.validate_compute_task(session, task)
                task.result_summary_json = {
                    "validation_passed": result.passed,
                    "reasons": result.reasons,
                }
                if result.passed:
                    self.transitions.transition_task(
                        session,
                        task,
                        TaskStatus.SUCCEEDED,
                        expected_status=TaskStatus.VERIFYING,
                    )
                else:
                    task.error_summary = "; ".join(result.reasons)
                    self.transitions.transition_task(
                        session,
                        task,
                        TaskStatus.FAILED,
                        expected_status=TaskStatus.VERIFYING,
                        payload={"validation_reasons": result.reasons},
                    )
                verified.append(task.id)
            agent_tasks = session.scalars(
                select(Task).where(
                    Task.status == TaskStatus.VERIFYING,
                    Task.kind == TaskKind.AGENT,
                    Task.executor.in_([TaskExecutor.HERMES, TaskExecutor.CODEX]),
                )
            ).all()
            for task in agent_tasks:
                result = self.validator.validate_agent_task(session, task)
                task.result_summary_json = {
                    **task.result_summary_json,
                    "validation_passed": result.passed,
                    "reasons": result.reasons,
                }
                if result.passed:
                    self.transitions.transition_task(
                        session,
                        task,
                        TaskStatus.SUCCEEDED,
                        expected_status=TaskStatus.VERIFYING,
                    )
                else:
                    task.error_summary = "; ".join(result.reasons)
                    self.transitions.transition_task(
                        session,
                        task,
                        TaskStatus.FAILED,
                        expected_status=TaskStatus.VERIFYING,
                        payload={"validation_reasons": result.reasons},
                    )
                verified.append(task.id)
        return verified

    def evaluate_project_transitions(self) -> None:
        """Reserved deterministic Phase 1 hook; scientific gates are deferred."""

    async def run_until_idle(
        self, *, timeout_seconds: float = 30, sleep_seconds: float = 0.05
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            await self.tick()
            with self.session_factory() as session:
                remaining = session.scalar(
                    select(Task.id).where(
                        Task.status.in_(
                            [
                                TaskStatus.PENDING,
                                TaskStatus.READY,
                                TaskStatus.RUNNING,
                                TaskStatus.VERIFYING,
                            ]
                        )
                    ).limit(1)
                )
            if remaining is None:
                return
            await asyncio.sleep(sleep_seconds)
        raise TimeoutError("controller did not become idle before timeout")
