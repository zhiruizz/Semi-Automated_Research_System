from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from research_controller.controller import ResearchController
from research_controller.db.models import Artifact, ComputeJob, Event, Task
from research_controller.domain.enums import ComputeExecutionStatus, TaskStatus
from tests.conftest import create_compute_task


@pytest.mark.asyncio
async def test_local_compute_success_vertical_slice(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_compute_task(factory, tmp_path)
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    with factory() as session:
        persistent = session.get(Task, task.id)
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == task.id))
        artifacts = session.scalars(select(Artifact).where(Artifact.task_id == task.id)).all()
        event_types = session.scalars(
            select(Event.event_type).where(Event.project_id == task.project_id).order_by(Event.seq)
        ).all()
        assert persistent.status is TaskStatus.SUCCEEDED
        assert job.execution_status is ComputeExecutionStatus.SUCCEEDED
        assert job.exit_code == 0
        assert {item.logical_name for item in artifacts} >= {
            "metrics.json",
            "launch_count.txt",
            "run.out",
            "run.error",
            "exit.json",
        }
        metrics = next(item for item in artifacts if item.logical_name == "metrics.json")
        assert metrics.evidence_eligible is True
        for required in (
            "TASK_CREATED",
            "TASK_READY",
            "TASK_STARTED",
            "COMPUTE_JOB_CREATED",
            "COMPUTE_JOB_SUBMITTED",
            "COMPUTE_JOB_STARTED",
            "COMPUTE_JOB_SUCCEEDED",
            "TASK_VERIFYING",
            "TASK_SUCCEEDED",
        ):
            assert required in event_types


@pytest.mark.asyncio
async def test_local_compute_failure_is_verified_not_trusted(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_compute_task(factory, tmp_path, exit_code=3)
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    with factory() as session:
        persistent = session.get(Task, task.id)
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == task.id))
        assert job.execution_status is ComputeExecutionStatus.FAILED
        assert job.exit_code == 3
        assert persistent.status is TaskStatus.FAILED
        assert "exit code" in persistent.error_summary
