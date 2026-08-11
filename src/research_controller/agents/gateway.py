from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from research_controller.agents.context import ContextBuilder
from research_controller.agents.registry import AgentAdapterRegistry
from research_controller.agents.router import AgentRouter
from research_controller.agents.sessions import SessionManager
from research_controller.db.models import AgentRun, Task
from research_controller.protocols.agent import (
    AgentExecutionRequest,
    AgentTaskSpec,
    ContextPack,
    RouteDecision,
)


@dataclass(frozen=True)
class AgentExecutionPlan:
    spec: AgentTaskSpec
    context_pack: ContextPack
    route: RouteDecision
    session_id: str
    adapter_config: dict[str, Any]


class AgentGateway:
    def __init__(
        self,
        registry: AgentAdapterRegistry,
        *,
        router: AgentRouter | None = None,
        context_builder: ContextBuilder | None = None,
        sessions: SessionManager | None = None,
    ) -> None:
        self.registry = registry
        self.router = router or AgentRouter()
        self.context_builder = context_builder or ContextBuilder()
        self.sessions = sessions or SessionManager()

    def prepare(self, session: Session, task: Task) -> AgentExecutionPlan:
        spec = AgentTaskSpec.model_validate(task.spec_json)
        if spec.project_id != task.project_id or spec.task_id != task.id:
            raise ValueError("AgentTaskSpec identity does not match Task")
        route = self.router.route(spec, task.executor)
        context_pack = self.context_builder.build(session, spec)
        session_id = self.sessions.select(
            session,
            project_id=task.project_id,
            role=spec.role,
            backend=route.adapter_id,
            policy=route.session_policy,
        )
        return AgentExecutionPlan(
            spec=spec,
            context_pack=context_pack,
            route=route,
            session_id=session_id,
            adapter_config=dict(task.routing_policy_json.get("mock", {})),
        )

    def request_for_run(self, run: AgentRun, workdir: Path) -> AgentExecutionRequest:
        config = run.config_json
        return AgentExecutionRequest(
            run_key=run.id,
            task_spec=AgentTaskSpec.model_validate(config["task_spec"]),
            context_pack=ContextPack.model_validate(config["context_pack"]),
            route=RouteDecision.model_validate(config["route"]),
            session_id=run.session_id or config["session_id"],
            workdir=workdir,
            adapter_config=dict(config.get("mock", {})),
        )
