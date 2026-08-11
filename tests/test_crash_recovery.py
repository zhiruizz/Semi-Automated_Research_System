from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import func, select

from research_controller.controller import ResearchController
from research_controller.db.models import Artifact, ComputeJob, Task
from research_controller.domain.enums import ComputeExecutionStatus, TaskStatus
from research_controller.protocols.compute import ComputeTaskSpec
from research_controller.services.task_readiness import TaskReadinessService
from tests.conftest import create_compute_task


def assert_single_launch(factory, task_id: str) -> None:
    with factory() as session:
        marker = session.scalar(
            select(Artifact).where(
                Artifact.task_id == task_id, Artifact.logical_name == "launch_count.txt"
            )
        )
        assert marker is not None
        assert Path(marker.uri).read_text(encoding="utf-8").splitlines() == ["launch"]
        assert session.scalar(
            select(func.count(ComputeJob.id)).where(ComputeJob.task_id == task_id)
        ) == 1


@pytest.mark.asyncio
async def test_recovery_after_task_ready(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_compute_task(factory, tmp_path)
    with factory.begin() as session:
        TaskReadinessService().reconcile(session)
    restarted = ResearchController(factory, workspace, poll_interval_seconds=0)
    await restarted.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    assert_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_recovery_after_compute_job_created(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_compute_task(factory, tmp_path)
    first = ResearchController(factory, workspace, poll_interval_seconds=0)
    await first.tick()  # readiness + durable CREATED job, no external submit yet
    with factory() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == task.id))
        assert job.execution_status is ComputeExecutionStatus.CREATED
    restarted = ResearchController(factory, workspace, poll_interval_seconds=0)
    await restarted.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    assert_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_uncertain_submit_reconciles_before_retry(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_compute_task(factory, tmp_path, sleep_seconds=0.1)
    first = ResearchController(factory, workspace, poll_interval_seconds=0)
    await first.tick()
    with factory() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == task.id))
        view_spec = ComputeTaskSpec.model_validate(job.spec_json)
        resource_class = job.resource_class
    provider = first.registry.get("local")
    prepared = await provider.prepare(view_spec, resource_class)
    external_id = await provider.submit(prepared)
    # Simulate a crash before external_job_id and SUBMITTED are committed.
    with factory() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == task.id))
        assert job.external_job_id is None
        assert job.execution_status is ComputeExecutionStatus.CREATED
    restarted = ResearchController(factory, workspace, poll_interval_seconds=0)
    await restarted.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    with factory() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == task.id))
        assert job.external_job_id == external_id
    assert_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_recovery_while_running_and_after_exit_before_collect(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_compute_task(factory, tmp_path, sleep_seconds=0.25)
    first = ResearchController(factory, workspace, poll_interval_seconds=0)
    await first.tick()  # CREATED
    await first.tick()  # submit
    await first.tick()  # observe RUNNING (normally)
    with factory() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == task.id))
        workdir = Path(job.remote_workdir)
        assert job.execution_status in {
            ComputeExecutionStatus.SUBMITTED,
            ComputeExecutionStatus.RUNNING,
        }
    while not (workdir / "exit.json").exists():
        await asyncio.sleep(0.01)
    # The database still reflects the last observation; collection has not run.
    restarted = ResearchController(factory, workspace, poll_interval_seconds=0)
    await restarted.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    with factory() as session:
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
    assert_single_launch(factory, task.id)


@pytest.mark.asyncio
async def test_real_controller_sigkill_then_restart(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_compute_task(factory, tmp_path, sleep_seconds=0.6)
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
                job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == task.id))
                if job is not None and job.execution_status in {
                    ComputeExecutionStatus.SUBMITTED,
                    ComputeExecutionStatus.RUNNING,
                }:
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("controller did not submit the local job before timeout")
        process.kill()  # SIGKILL: the detached research-runner must survive.
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    restarted = ResearchController(factory, workspace, poll_interval_seconds=0)
    await restarted.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    with factory() as session:
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
    assert_single_launch(factory, task.id)
