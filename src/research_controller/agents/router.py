from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from research_controller.domain.enums import TaskExecutor
from research_controller.protocols.agent import AgentTaskSpec, RouteDecision


class BackendNotImplementedError(ValueError):
    pass


DEFAULT_ROLES: dict[str, dict[str, str]] = {
    "implementation_worker": {
        "logical_executor": "hermes",
        "model_tier": "cheap",
        "session_policy": "resume_role",
        "context_profile": "implementation",
    },
    "debug_worker": {
        "logical_executor": "hermes",
        "model_tier": "cheap",
        "session_policy": "resume_role",
        "context_profile": "debug",
    },
    "scientific_supervisor": {
        "logical_executor": "codex",
        "model_tier": "supervisor",
        "session_policy": "resume_role",
        "context_profile": "scientific",
    },
    "experiment_planner": {
        "logical_executor": "codex",
        "model_tier": "supervisor",
        "session_policy": "resume_role",
        "context_profile": "scientific",
    },
    "result_reviewer": {
        "logical_executor": "codex",
        "model_tier": "supervisor",
        "session_policy": "new",
        "context_profile": "scientific_review",
    },
    "paper_writer": {
        "logical_executor": "codex",
        "model_tier": "supervisor",
        "session_policy": "resume_role",
        "context_profile": "writing",
    },
    "paper_reviewer": {
        "logical_executor": "codex",
        "model_tier": "supervisor",
        "session_policy": "new",
        "context_profile": "paper_review",
    },
    "integrity_reviewer": {
        "logical_executor": "codex",
        "model_tier": "supervisor",
        "session_policy": "new",
        "context_profile": "scientific_review",
    },
}


class AgentRouter:
    def __init__(self, roles_path: Path | str | None = None) -> None:
        path = Path(roles_path) if roles_path else Path(__file__).resolve().parents[3] / "config" / "roles.yaml"
        self.roles = DEFAULT_ROLES
        if path.is_file():
            with path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
            if isinstance(loaded.get("roles"), dict):
                self.roles = {**DEFAULT_ROLES, **loaded["roles"]}

    def route(
        self,
        spec: AgentTaskSpec,
        executor: TaskExecutor,
        routing_policy: dict[str, Any] | None = None,
    ) -> RouteDecision:
        policy = routing_policy or {}
        configured: dict[str, Any] = self.roles.get(spec.role, {})
        logical = str(configured.get("logical_executor", executor.value.lower()))
        if logical != executor.value.lower():
            # Task.executor is the persisted source of logical ownership.
            logical = executor.value.lower()
        explicit_adapter = policy.get("adapter")
        if explicit_adapter == "mock" or "mock" in policy:
            adapter_id = "mock"
            reason = "explicit test/demo mock route"
        elif executor is TaskExecutor.HERMES and spec.role in {
            "implementation_worker",
            "debug_worker",
        }:
            adapter_id = "hermes"
            reason = "production Hermes Runs API route"
        elif executor is TaskExecutor.CODEX and spec.role in {
            "scientific_supervisor",
            "experiment_planner",
            "result_reviewer",
            "paper_writer",
            "paper_reviewer",
            "integrity_reviewer",
        }:
            adapter_id = "codex"
            reason = "production Codex App Server route"
        else:
            raise BackendNotImplementedError(
                f"no production Agent adapter for executor={executor.value} role={spec.role}"
            )
        if adapter_id == "codex" and spec.execution_policy.session_policy.value == "fork_role":
            raise BackendNotImplementedError("CODEX_FORK_ROLE_DEFERRED")
        return RouteDecision(
            adapter_id=adapter_id,
            logical_executor=logical,
            role=spec.role,
            model_tier=str(policy.get("model_tier", configured.get("model_tier", "cheap"))),
            session_policy=spec.execution_policy.session_policy,
            context_profile=str(configured.get("context_profile", "default")),
            reason=reason,
        )
