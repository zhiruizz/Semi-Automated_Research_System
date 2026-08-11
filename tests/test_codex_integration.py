from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from research_controller.agents.codex.adapter import CodexAdapter
from research_controller.agents.codex.models import CodexHealth
from research_controller.cli import create_codex_demo
from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Task
from research_controller.domain.enums import AgentRunStatus, TaskStatus


pytestmark = [
    pytest.mark.codex_integration,
    pytest.mark.skipif(
        os.environ.get("SARS_CODEX_INTEGRATION") != "1",
        reason="set SARS_CODEX_INTEGRATION=1 for one tiny real Codex turn",
    ),
]


@pytest.mark.asyncio
async def test_tiny_real_codex_facts_fixture(runtime):
    _engine, factory, workspace = runtime
    _project_id, task_id = create_codex_demo(factory, workspace)
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    adapter = controller.agent_registry.get("codex")
    assert isinstance(adapter, CodexAdapter)
    status = await adapter.status(refresh=True)
    if status.health is not CodexHealth.HEALTHY:
        pytest.skip(f"Codex auth/runtime unavailable: {status.health.value}")
    await controller.run_until_idle(timeout_seconds=240, sleep_seconds=0.1)
    with factory() as session:
        task = session.get(Task, task_id)
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
        assert task.status is TaskStatus.SUCCEEDED
        assert run.status is AgentRunStatus.SUCCEEDED
        assert run.session_id
        assert run.external_run_id
        assert run.input_tokens is not None
        assert run.output_tokens is not None
