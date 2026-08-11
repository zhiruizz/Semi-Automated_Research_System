from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Task
from research_controller.domain.enums import TaskStatus
from tests.conftest import create_agent_task


pytestmark = [
    pytest.mark.hermes_integration,
    pytest.mark.skipif(
        os.environ.get("SARS_HERMES_INTEGRATION") != "1",
        reason="real Hermes integration is explicit opt-in",
    ),
]


@pytest.mark.asyncio
async def test_tiny_real_hermes_run(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, first = create_agent_task(
        factory,
        tmp_path,
        session_policy="new",
        permissions={"filesystem_write": True},
        timeout_sec=180,
    )
    with factory.begin() as session:
        task = session.get(Task, first.id)
        task.routing_policy_json = {"hermes": {}}
        task.spec_json = {
            **task.spec_json,
            "objective": "Write one implementation summary under 80 words and return the strict result envelope.",
            "instructions": ["Use only the assigned run outputs directory."],
        }
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.run_until_idle(timeout_seconds=180, sleep_seconds=0.5)
    with factory() as session:
        first_run = session.scalar(select(AgentRun).where(AgentRun.task_id == first.id))
        assert session.get(Task, first.id).status is TaskStatus.SUCCEEDED
        assert first_run.session_id
