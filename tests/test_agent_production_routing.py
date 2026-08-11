from __future__ import annotations

from research_controller.agents.router import AgentRouter
from research_controller.domain.enums import TaskExecutor
from research_controller.protocols.agent import AgentTaskSpec


def test_production_codex_routes_to_codex_not_mock_or_hermes():
    spec = AgentTaskSpec.model_validate(
        {
            "schema_version": "agent-task/v0.1",
            "project_id": "prj-test",
            "task_id": "tsk-test",
            "role": "scientific_supervisor",
            "objective": "review supplied facts",
        }
    )
    route = AgentRouter().route(spec, TaskExecutor.CODEX, {})
    assert route.adapter_id == "codex"
    assert route.logical_executor == "codex"
