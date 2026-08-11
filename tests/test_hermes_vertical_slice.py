from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Artifact, Task
from research_controller.domain.enums import AgentRunStatus, TaskStatus
from tests.conftest import create_agent_task
from tests.fake_hermes import FakeHermesApiServer


@pytest.mark.asyncio
async def test_real_adapter_against_fake_runs_api(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = create_agent_task(factory, tmp_path)
        with factory.begin() as session:
            persistent = session.get(Task, task.id)
            persistent.routing_policy_json = {"hermes": {}}
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
            assert run.status is AgentRunStatus.SUCCEEDED
            assert run.backend == "hermes"
            assert run.external_run_id == "run_fake_1"
            assert run.session_id == "session-run_fake_1"
            assert run.input_tokens == 11
            assert run.output_tokens == 7
            assert Path(run.config_json["workdir"]).parts[-2:] == ("agent-runs", run.id)
            artifacts = session.scalars(select(Artifact).where(Artifact.task_id == task.id)).all()
            assert {item.kind for item in artifacts} >= {"RAW_AGENT_RESPONSE", "AGENT_RESPONSE"}
        assert fake.post_count == 1
        bridge_request = next((workspace / ".hermes-bridge").glob("*/request.json"))
        assert fake.token not in bridge_request.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_uncertain_start_blocks_without_retry(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        fake.drop_start_response = True
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = create_agent_task(factory, tmp_path)
        with factory.begin() as session:
            session.get(Task, task.id).routing_policy_json = {"hermes": {}}
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        for _ in range(100):
            await controller.tick()
            with factory() as session:
                if session.get(Task, task.id).status is TaskStatus.BLOCKED:
                    break
            await asyncio.sleep(0.02)
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            assert session.get(Task, task.id).status is TaskStatus.BLOCKED
            assert run.error_type == "START_STATE_UNCERTAIN"
        assert fake.post_count == 1
