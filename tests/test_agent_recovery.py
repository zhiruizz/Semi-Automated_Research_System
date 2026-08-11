from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import func, select

from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Artifact, Project, Task
from research_controller.domain.enums import AgentRunStatus, ProjectLifecycle, TaskStatus
from research_controller.services.task_readiness import TaskReadinessService
from tests.conftest import create_agent_task


def assert_agent_single_launch(factory, task_id: str) -> None:
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
        assert run is not None
        assert session.scalar(
            select(func.count(AgentRun.id)).where(AgentRun.task_id == task_id)
        ) == 1
        launch_path = Path(run.config_json["workdir"]) / "launch_count.txt"
        assert launch_path.read_text(encoding="utf-8").splitlines() == ["launch"]


@pytest.mark.asyncio
async def test_agent_recovery_after_task_ready(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path)
    with factory.begin() as session:
        TaskReadinessService().reconcile(session)
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    assert_agent_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_agent_starting_recovery(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path)
    first = ResearchController(factory, workspace, poll_interval_seconds=0)
    await first.tick()
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.STARTING
        assert run.external_run_id is None
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    assert_agent_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_agent_uncertain_start_reconciles_before_retry(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path, mock={"delay_sec": 0.2})
    first = ResearchController(factory, workspace, poll_interval_seconds=0)
    await first.tick()
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        request = first.agent_gateway.request_for_run(
            run, Path(run.config_json["workdir"])
        )
        run_id = run.id
    adapter = first.agent_registry.get("mock")
    external = await adapter.start(request)
    duplicate = await adapter.start(request)
    assert duplicate.external_run_id == external.external_run_id
    with factory() as session:
        run = session.get(AgentRun, run_id)
        assert run.status is AgentRunStatus.STARTING
        assert run.external_run_id is None
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        assert session.get(AgentRun, run_id).external_run_id == external.external_run_id
    assert_agent_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_agent_running_recovery(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path, mock={"delay_sec": 0.3})
    first = ResearchController(factory, workspace, poll_interval_seconds=0)
    await first.tick()
    await first.tick()
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.RUNNING
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    assert_agent_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_agent_external_crash_is_backend_failure(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path, mock={"external_crash": True})
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.FAILED
        assert session.get(Task, task.id).status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_agent_timeout(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(
        factory, tmp_path, mock={"delay_sec": 2}, timeout_sec=1
    )
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=5, sleep_seconds=0.02
    )
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.TIMEOUT
        assert session.get(Task, task.id).status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_agent_running_and_result_before_collect_recovery(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path, mock={"delay_sec": 0.2})
    first = ResearchController(factory, workspace, poll_interval_seconds=0)
    await first.tick()
    await first.tick()
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        run_id = run.id
        run_dir = Path(run.config_json["workdir"])
        assert run.status is AgentRunStatus.RUNNING
    while not (run_dir / "raw_response.json").exists():
        await asyncio.sleep(0.01)
    with factory() as session:
        assert session.get(AgentRun, run_id).status is AgentRunStatus.RUNNING
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
    assert_agent_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_active_agent_run_prevents_duplicate_task_dispatch(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path, mock={"delay_sec": 0.4})
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.tick()
    await controller.tick()
    with factory.begin() as session:
        persistent = session.get(Task, task.id)
        assert persistent.status is TaskStatus.RUNNING
        persistent.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await controller.tick()
    with factory() as session:
        assert session.scalar(
            select(func.count(AgentRun.id)).where(AgentRun.task_id == task.id)
        ) == 1
        assert session.get(Task, task.id).status is TaskStatus.RUNNING
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    assert_agent_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_paused_project_still_reconciles_existing_agent(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = create_agent_task(factory, tmp_path, mock={"delay_sec": 0.2})
    first = ResearchController(factory, workspace, poll_interval_seconds=0)
    await first.tick()
    await first.tick()
    with factory.begin() as session:
        session.get(Project, project.id).lifecycle = ProjectLifecycle.PAUSED
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
    assert_agent_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_real_controller_sigkill_agent_recovery(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path, mock={"delay_sec": 0.7})
    database = tmp_path / "controller.db"
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
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with factory() as session:
                run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
                if run is not None and run.status is AgentRunStatus.RUNNING:
                    run_dir = Path(run.config_json["workdir"])
                    if (run_dir / "started.json").exists():
                        break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Controller did not start MockAgent before timeout")
        process.kill()
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
    assert_agent_single_launch(factory, task.id)
