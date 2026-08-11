from __future__ import annotations

import pytest
from sqlalchemy import select

from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun
from research_controller.domain.enums import TaskExecutor
from tests.conftest import create_agent_task


def run_for(factory, task_id: str) -> AgentRun:
    with factory() as session:
        return session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))


@pytest.mark.asyncio
async def test_agent_new_session(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, first = create_agent_task(factory, tmp_path, session_policy="new")
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    _project, second = create_agent_task(
        factory, tmp_path, project_id=project.id, session_policy="new"
    )
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    assert run_for(factory, first.id).session_id != run_for(factory, second.id).session_id


@pytest.mark.asyncio
async def test_agent_resume_role_session(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, first = create_agent_task(factory, tmp_path, session_policy="new")
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    _project, second = create_agent_task(
        factory, tmp_path, project_id=project.id, session_policy="resume_role"
    )
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    assert run_for(factory, first.id).session_id == run_for(factory, second.id).session_id


@pytest.mark.asyncio
async def test_agent_role_session_isolation(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, writer = create_agent_task(
        factory,
        tmp_path,
        role="paper_writer",
        executor=TaskExecutor.CODEX,
        session_policy="new",
    )
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    _project, reviewer = create_agent_task(
        factory,
        tmp_path,
        project_id=project.id,
        role="paper_reviewer",
        executor=TaskExecutor.CODEX,
        session_policy="resume_role",
    )
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    assert run_for(factory, writer.id).session_id != run_for(factory, reviewer.id).session_id


@pytest.mark.asyncio
async def test_agent_ephemeral_session_is_not_resumed(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, ephemeral = create_agent_task(
        factory, tmp_path, session_policy="ephemeral"
    )
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    _project, resumed = create_agent_task(
        factory, tmp_path, project_id=project.id, session_policy="resume_role"
    )
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    first = run_for(factory, ephemeral.id)
    second = run_for(factory, resumed.id)
    assert first.session_id.startswith("mock-ephemeral-session")
    assert first.session_id != second.session_id
