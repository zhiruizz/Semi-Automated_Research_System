from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import func, select

from research_controller.agents.hermes.adapter import HermesAdapter
from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Artifact, Event, Task
from research_controller.domain.enums import AgentRunStatus, TaskStatus
from research_controller.services.agent_reconciler import agent_run_view
from tests.conftest import create_agent_task
from tests.fake_hermes import FakeHermesApiServer


def make_real(factory, tmp_path: Path, **kwargs):
    project, task = create_agent_task(factory, tmp_path, **kwargs)
    with factory.begin() as session:
        session.get(Task, task.id).routing_policy_json = {"hermes": {}}
    return project, task


async def wait_for_status(factory, task_id: str, status: TaskStatus, controller, ticks: int = 150):
    for _ in range(ticks):
        await controller.tick()
        with factory() as session:
            if session.get(Task, task_id).status is status:
                return
        await asyncio.sleep(0.02)
    pytest.fail(f"Task {task_id} did not reach {status}")


@pytest.mark.asyncio
async def test_waiting_approval_never_auto_approves_and_once_resumes(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        fake.next_status = "waiting_for_approval"
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = make_real(factory, tmp_path)
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        for _ in range(100):
            await controller.tick()
            with factory() as session:
                run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
                if run is not None and run.status is AgentRunStatus.WAITING_APPROVAL:
                    view = agent_run_view(run)
                    break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("run did not wait for approval")
        assert fake.approvals == []
        adapter = controller.agent_registry.get("hermes")
        assert isinstance(adapter, HermesAdapter)
        await adapter.respond_approval(view, "once")
        assert fake.approvals == ["once"]
        fake.runs[view.external_run_id]["status"] = "completed"
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        with factory() as session:
            assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
            assert session.scalar(
                select(func.count(Event.id)).where(
                    Event.entity_id == view.id,
                    Event.event_type == "AGENT_APPROVAL_REQUIRED",
                )
            ) == 1


@pytest.mark.asyncio
async def test_approval_deny_finishes_as_backend_failure(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        fake.next_status = "waiting_for_approval"
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = make_real(factory, tmp_path)
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        for _ in range(100):
            await controller.tick()
            with factory() as session:
                run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
                if run is not None and run.status is AgentRunStatus.WAITING_APPROVAL:
                    view = agent_run_view(run)
                    break
            await asyncio.sleep(0.02)
        adapter = controller.agent_registry.get("hermes")
        assert isinstance(adapter, HermesAdapter)
        await adapter.respond_approval(view, "deny")
        await wait_for_status(factory, task.id, TaskStatus.FAILED, controller)
        assert fake.approvals == ["deny"]


@pytest.mark.asyncio
async def test_timeout_requests_stop_then_waits_for_remote_cancel(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        fake.next_status = "running"
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = make_real(factory, tmp_path, timeout_sec=1)
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        await controller.run_until_idle(timeout_seconds=5, sleep_seconds=0.02)
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            assert run.status is AgentRunStatus.TIMEOUT
            assert session.get(Task, task.id).status is TaskStatus.FAILED
        assert fake.stop_count == 1


@pytest.mark.asyncio
async def test_session_resume_new_and_tier_isolation(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        project, first = make_real(factory, tmp_path, session_policy="new")
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        _project, resumed = make_real(
            factory, tmp_path, project_id=project.id, session_policy="resume_role"
        )
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        _project, new_task = make_real(
            factory, tmp_path, project_id=project.id, session_policy="new"
        )
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        _project, strong = make_real(
            factory, tmp_path, project_id=project.id, session_policy="resume_role"
        )
        with factory.begin() as session:
            session.get(Task, strong.id).routing_policy_json = {
                "hermes": {},
                "model_tier": "strong",
            }
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        assert "session_id" not in fake.requests[0]
        assert fake.requests[1]["session_id"] == "session-run_fake_1"
        assert "session_id" not in fake.requests[2]
        assert "session_id" not in fake.requests[3]


@pytest.mark.asyncio
async def test_ephemeral_real_session_is_persisted_but_never_resumed(
    runtime, tmp_path, monkeypatch
):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        project, ephemeral = make_real(factory, tmp_path, session_policy="ephemeral")
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        _project, resumed = make_real(
            factory, tmp_path, project_id=project.id, session_policy="resume_role"
        )
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        with factory() as session:
            first = session.scalar(select(AgentRun).where(AgentRun.task_id == ephemeral.id))
            second = session.scalar(select(AgentRun).where(AgentRun.task_id == resumed.id))
            assert first.session_id
            assert second.session_id
            assert first.session_id != second.session_id
        assert "session_id" not in fake.requests[0]
        assert "session_id" not in fake.requests[1]


@pytest.mark.asyncio
async def test_unknown_status_is_nonterminal_then_recovers(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        fake.unknown_status = True
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = make_real(factory, tmp_path)
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        for _ in range(30):
            await controller.tick()
            await asyncio.sleep(0.02)
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            assert run.status is AgentRunStatus.RUNNING
            assert run.error_type == "UNKNOWN_REMOTE_STATUS"
        fake.unknown_status = False
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        with factory() as session:
            assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_remote_run_disappearance_blocks_task(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        fake.next_status = "running"
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = make_real(factory, tmp_path)
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        while fake.post_count == 0:
            await controller.tick()
            await asyncio.sleep(0.02)
        fake.runs.clear()
        await wait_for_status(factory, task.id, TaskStatus.BLOCKED, controller)
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            assert run.error_type == "REMOTE_RUN_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "expected"),
    [("invalid_result", "INVALID_AGENT_RESULT_ENVELOPE"), ("mismatch_result", "AGENT_RESULT_MISMATCH")],
)
async def test_invalid_or_mismatched_result_preserves_raw(
    runtime, tmp_path, monkeypatch, flag, expected
):
    _engine, factory, workspace = runtime
    with FakeHermesApiServer() as fake:
        setattr(fake, flag, True)
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = make_real(factory, tmp_path)
        controller = ResearchController(factory, workspace, poll_interval_seconds=0)
        await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.02)
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            assert run.error_type == expected
            assert session.scalar(
                select(Artifact.id).where(
                    Artifact.task_id == task.id,
                    Artifact.kind == "RAW_AGENT_RESPONSE",
                )
            ) is not None


@pytest.mark.asyncio
async def test_controller_sigkill_bridge_recovery_posts_once(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    database = tmp_path / "controller.db"
    with FakeHermesApiServer() as fake:
        fake.next_status = "running"
        monkeypatch.setenv("SARS_HERMES_API_KEY", fake.token)
        monkeypatch.setenv("SARS_HERMES_BASE_URL", fake.base_url)
        _project, task = make_real(factory, tmp_path)
        environment = os.environ.copy()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "research_controller.cli",
                "--database",
                str(database),
                "--workspace",
                str(workspace),
                "run",
                "--interval",
                "0.01",
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and fake.post_count == 0:
                await asyncio.sleep(0.02)
            assert fake.post_count == 1
            process.kill()
            process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        fake.next_status = "completed"
        fake.runs["run_fake_1"]["status"] = "completed"
        await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
            timeout_seconds=10, sleep_seconds=0.02
        )
        with factory() as session:
            assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
        assert fake.post_count == 1
