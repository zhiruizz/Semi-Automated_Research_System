from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import func, select

from research_controller.compute.local.provider import LocalProvider
from research_controller.compute.registry import ProviderRegistry
from research_controller.controller import ResearchController
from research_controller.db.models import Artifact, ComputeJob, Event, Task
from research_controller.domain.enums import (
    ComputeExecutionStatus,
    ProjectStage,
    TaskExecutor,
    TaskKind,
    TaskStatus,
)
from research_controller.domain.ids import new_id
from research_controller.protocols.compute import ComputeTaskSpec
from research_controller.services.project_state import ProjectStateService
from research_controller.services.reconciler import job_view
from research_controller.services.resource_allocation import (
    ACTIVE_GPU_RESERVATION_STATUSES,
    LocalGpuAllocationService,
)
from tests.test_nvidia_smi import FakeNvidiaSmiClient, two_gpus


def create_local_tasks(
    factory,
    tmp_path: Path,
    requests: list[tuple[str, int, float, float]],
) -> dict[str, str]:
    """Create (name, gpu_count, min_memory_gb, sleep_seconds) compute tasks."""
    worker = tmp_path / f"gpu-worker-{new_id('script')}.py"
    worker.write_text(
        "import json, os, sys, time\n"
        "with open('launch_count.txt', 'a', encoding='utf-8') as h: h.write('launch\\n')\n"
        "with open('gpu.json', 'w', encoding='utf-8') as h: "
        "json.dump({'cuda_visible_devices': os.environ.get('CUDA_VISIBLE_DEVICES')}, h)\n"
        "with open('metrics.json', 'w', encoding='utf-8') as h: json.dump({'loss': 1.0}, h)\n"
        "time.sleep(float(sys.argv[1]))\n",
        encoding="utf-8",
    )
    service = ProjectStateService()
    result: dict[str, str] = {}
    with factory.begin() as session:
        project = service.create_project(
            session,
            slug=new_id("gpu-project"),
            title="GPU allocation test",
            workspace_uri=str(tmp_path / "workspace"),
        )
        for index, (name, gpu_count, memory_gb, sleep_seconds) in enumerate(requests):
            task_id = new_id("tsk")
            spec = ComputeTaskSpec.model_validate(
                {
                    "schema_version": "compute-task-spec/v0.1",
                    "project_id": project.id,
                    "task_id": task_id,
                    "submission_key": f"{project.id}:{task_id}:a1",
                    "execution": {
                        "command": [sys.executable, str(worker)],
                        "argv": [str(sleep_seconds)],
                    },
                    "resources": {
                        "gpu_count": gpu_count,
                        "min_gpu_memory_gb": memory_gb,
                        "required_capabilities": ["cuda"] if gpu_count else [],
                    },
                    "outputs": [
                        {
                            "logical_name": "metrics.json",
                            "glob": "metrics.json",
                            "required": True,
                            "artifact_kind": "METRICS",
                            "evidence_candidate": True,
                        },
                        {
                            "logical_name": "gpu.json",
                            "glob": "gpu.json",
                            "required": True,
                            "artifact_kind": "GPU_ENV",
                        },
                        {
                            "logical_name": "launch_count.txt",
                            "glob": "launch_count.txt",
                            "required": True,
                            "artifact_kind": "DEBUG_MARKER",
                        },
                    ],
                    "routing": {"allowed_providers": ["local"]},
                    "success": {
                        "required_validators": [
                            "exit_code_zero",
                            "metrics_json",
                            "no_nan",
                        ]
                    },
                }
            )
            service.create_task(
                session,
                task_id=task_id,
                project_id=project.id,
                stage=ProjectStage.TOY_RUN,
                kind=TaskKind.COMPUTE,
                action=name,
                executor=TaskExecutor.COMPUTE,
                idempotency_key=f"{name}:v1",
                spec=spec.model_dump(mode="json"),
                priority=len(requests) - index,
                acceptance_policy={
                    "required_artifacts": [
                        "metrics.json",
                        "gpu.json",
                        "launch_count.txt",
                        "run.out",
                        "run.error",
                        "exit.json",
                    ],
                    "validators": ["metrics_json", "no_nan"],
                },
            )
            result[name] = task_id
    return result


def gpu_controller(
    factory,
    workspace: Path,
    *,
    retry_seconds: float = 0.02,
    inventory=None,
) -> ResearchController:
    provider = LocalProvider(
        workspace / ".local-provider",
        nvidia_smi=FakeNvidiaSmiClient(inventory or two_gpus()),
    )
    return ResearchController(
        factory,
        workspace,
        registry=ProviderRegistry([provider]),
        poll_interval_seconds=0,
        dispatch_retry_seconds=retry_seconds,
    )


def read_artifact_json(factory, task_id: str, logical_name: str) -> dict:
    with factory() as session:
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.task_id == task_id,
                Artifact.logical_name == logical_name,
            )
        )
        assert artifact is not None
        return json.loads(Path(artifact.uri).read_text(encoding="utf-8"))


def assert_launched_once(factory, task_id: str) -> None:
    with factory() as session:
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.task_id == task_id,
                Artifact.logical_name == "launch_count.txt",
            )
        )
        assert artifact is not None
        assert Path(artifact.uri).read_text(encoding="utf-8").splitlines() == ["launch"]


@pytest.mark.asyncio
async def test_two_gpu_jobs_reserve_distinct_devices_and_third_waits(runtime, tmp_path):
    _engine, factory, workspace = runtime
    tasks = create_local_tasks(
        factory,
        tmp_path,
        [("A", 1, 16, 0.15), ("B", 1, 16, 0.15), ("C", 1, 16, 0.05)],
    )
    controller = gpu_controller(factory, workspace)
    await controller.tick()

    with factory() as session:
        jobs = session.scalars(select(ComputeJob).order_by(ComputeJob.created_at)).all()
        assert {job.resource_class for job in jobs} == {"local_gpu_0", "local_gpu_1"}
        assert all(job.execution_status is ComputeExecutionStatus.CREATED for job in jobs)
        assert session.get(Task, tasks["C"]).status is TaskStatus.READY
        assert session.get(Task, tasks["C"]).not_before is not None
        assert len({job.resource_class for job in jobs}) == len(jobs)
        for job in jobs:
            allocation = job.provider_metadata_json["allocation"]
            assert allocation["gpu_index"] in {0, 1}
            assert allocation["gpu_uuid"] in {"GPU-0", "GPU-1"}
            assert allocation["allocation_type"] == "exclusive"

    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    assert {
        read_artifact_json(factory, tasks["A"], "gpu.json")["cuda_visible_devices"],
        read_artifact_json(factory, tasks["B"], "gpu.json")["cuda_visible_devices"],
    } == {"0", "1"}
    assert read_artifact_json(factory, tasks["C"], "gpu.json")[
        "cuda_visible_devices"
    ] in {"0", "1"}
    with factory() as session:
        assert all(
            session.get(Task, task_id).status is TaskStatus.SUCCEEDED
            for task_id in tasks.values()
        )
        assert session.scalar(select(func.count(ComputeJob.id))) == 3
    for task_id in tasks.values():
        assert_launched_once(factory, task_id)


@pytest.mark.asyncio
async def test_cpu_task_dispatches_while_both_gpus_reserved(runtime, tmp_path):
    _engine, factory, workspace = runtime
    tasks = create_local_tasks(
        factory,
        tmp_path,
        [("A", 1, 16, 0.1), ("B", 1, 16, 0.1), ("CPU", 0, 0, 0.01)],
    )
    controller = gpu_controller(factory, workspace)
    await controller.tick()
    with factory() as session:
        jobs = session.scalars(select(ComputeJob)).all()
        assert {job.resource_class for job in jobs} == {
            "local_gpu_0",
            "local_gpu_1",
            "local_cpu",
        }
        assert session.get(Task, tasks["CPU"]).status is TaskStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize("gpu_count,memory_gb", [(1, 24), (2, 1)])
async def test_structurally_unsupported_local_gpu_task_blocks(
    runtime, tmp_path, gpu_count, memory_gb
):
    _engine, factory, workspace = runtime
    tasks = create_local_tasks(
        factory,
        tmp_path,
        [("unsupported", gpu_count, memory_gb, 0.01)],
    )
    controller = gpu_controller(factory, workspace)
    await controller.tick()
    with factory() as session:
        task = session.get(Task, tasks["unsupported"])
        assert task.status is TaskStatus.BLOCKED
        assert task.block_reason == "RESOURCE_UNAVAILABLE"
        assert session.scalar(
            select(func.count(ComputeJob.id)).where(ComputeJob.task_id == task.id)
        ) == 0


@pytest.mark.asyncio
async def test_busy_queue_does_not_emit_event_spam(runtime, tmp_path):
    _engine, factory, workspace = runtime
    tasks = create_local_tasks(
        factory,
        tmp_path,
        [("A", 1, 16, 0.2), ("B", 1, 16, 0.2), ("C", 1, 16, 0.02)],
    )
    controller = gpu_controller(factory, workspace, retry_seconds=5)
    await controller.tick()
    with factory() as session:
        before = session.scalar(
            select(func.count(Event.id)).where(Event.entity_id == tasks["C"])
        )
    for _ in range(5):
        await controller.tick()
    with factory() as session:
        after = session.scalar(
            select(func.count(Event.id)).where(Event.entity_id == tasks["C"])
        )
        assert before == after == 2  # TASK_CREATED + TASK_READY only
    await asyncio.sleep(0.25)
    await controller.tick()


@pytest.mark.asyncio
async def test_created_jobs_remain_reservations_across_restart(runtime, tmp_path):
    _engine, factory, workspace = runtime
    tasks = create_local_tasks(
        factory,
        tmp_path,
        [("A", 1, 16, 0.1), ("B", 1, 16, 0.1), ("C", 1, 16, 0.01)],
    )
    first = gpu_controller(factory, workspace, retry_seconds=5)
    await first.tick()
    restarted = gpu_controller(factory, workspace, retry_seconds=5)
    await restarted.tick()
    with factory() as session:
        assert session.scalar(
            select(func.count(ComputeJob.id)).where(ComputeJob.task_id == tasks["C"])
        ) == 0
        reservations = session.scalars(
            select(ComputeJob).where(
                ComputeJob.execution_status.in_(ACTIVE_GPU_RESERVATION_STATUSES)
            )
        ).all()
        assert {job.resource_class for job in reservations} == {
            "local_gpu_0",
            "local_gpu_1",
        }
    await asyncio.sleep(0.15)
    await restarted.tick()


@pytest.mark.asyncio
async def test_cancel_does_not_release_until_observed_terminal(runtime, tmp_path):
    _engine, factory, workspace = runtime
    tasks = create_local_tasks(factory, tmp_path, [("A", 1, 16, 0.5)])
    controller = gpu_controller(factory, workspace)
    await controller.tick()
    await controller.tick()
    with factory() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == tasks["A"]))
        view = job_view(job)
    await controller.registry.get("local").cancel(view)
    snapshot = await controller.registry.get("local").discover_resources()
    effective = LocalGpuAllocationService(factory).overlay(snapshot)
    gpu0 = next(offer for offer in effective.offers if offer.resource_class == "local_gpu_0")
    assert gpu0.schedulable is False
    assert gpu0.metadata["busy_reason"] == "controller_reserved"


@pytest.mark.asyncio
async def test_gpu_uncertain_submit_reconciles_before_new_allocation(runtime, tmp_path):
    _engine, factory, workspace = runtime
    tasks = create_local_tasks(
        factory,
        tmp_path,
        [("A", 1, 16, 0.15), ("C", 1, 16, 0.02)],
    )
    one_gpu = two_gpus()[:1]
    first = gpu_controller(factory, workspace, retry_seconds=5, inventory=one_gpu)
    await first.tick()
    with factory() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == tasks["A"]))
        spec = ComputeTaskSpec.model_validate(job.spec_json)
        resource_class = job.resource_class
    provider = first.registry.get("local")
    prepared = await provider.prepare(spec, resource_class)
    external_id = await provider.submit(prepared)

    # Crash boundary: workload launched, DB still has CREATED and no external id.
    with factory.begin() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == tasks["A"]))
        assert job.execution_status is ComputeExecutionStatus.CREATED
        assert job.external_job_id is None
        session.get(Task, tasks["C"]).not_before = None

    restarted = gpu_controller(factory, workspace, inventory=one_gpu)
    await restarted.tick()
    with factory() as session:
        job = session.scalar(select(ComputeJob).where(ComputeJob.task_id == tasks["A"]))
        assert job.external_job_id == external_id
        assert session.scalar(
            select(func.count(ComputeJob.id)).where(ComputeJob.task_id == tasks["C"])
        ) == 0
    await restarted.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    assert_launched_once(factory, tasks["A"])
    assert_launched_once(factory, tasks["C"])


def write_fake_nvidia_smi(directory: Path) -> Path:
    executable = directory / "nvidia-smi"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "query = ' '.join(sys.argv[1:])\n"
        "if '--query-gpu=' in query:\n"
        "    print('0, GPU-0, NVIDIA Test GPU, 20480, 0, 0')\n"
        "    print('1, GPU-1, NVIDIA Test GPU, 20480, 0, 0')\n"
        "elif '--query-compute-apps=' in query:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


@pytest.mark.asyncio
async def test_real_sigkill_preserves_two_gpu_reservations(runtime, tmp_path):
    _engine, factory, workspace = runtime
    tasks = create_local_tasks(
        factory,
        tmp_path,
        [("A", 1, 16, 0.7), ("B", 1, 16, 0.7), ("C", 1, 16, 0.02)],
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    write_fake_nvidia_smi(fake_bin)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "research_controller.cli",
            "--database",
            str(tmp_path / "controller.db"),
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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with factory() as session:
                active = session.scalars(
                    select(ComputeJob).where(
                        ComputeJob.execution_status.in_(
                            {
                                ComputeExecutionStatus.SUBMITTED,
                                ComputeExecutionStatus.RUNNING,
                            }
                        )
                    )
                ).all()
                c_jobs = session.scalar(
                    select(func.count(ComputeJob.id)).where(
                        ComputeJob.task_id == tasks["C"]
                    )
                )
                if len(active) == 2 and c_jobs == 0:
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("controller did not establish two active GPU jobs")
        process.kill()
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    # Make C immediately due. Reconciliation must still retain A/B reservations.
    with factory.begin() as session:
        session.get(Task, tasks["C"]).not_before = None
    restarted = gpu_controller(factory, workspace)
    await restarted.tick()
    with factory() as session:
        assert session.scalar(
            select(func.count(ComputeJob.id)).where(ComputeJob.task_id == tasks["C"])
        ) == 0
        assert {
            job.resource_class
            for job in session.scalars(
                select(ComputeJob).where(
                    ComputeJob.execution_status.in_(ACTIVE_GPU_RESERVATION_STATUSES)
                )
            ).all()
        } == {"local_gpu_0", "local_gpu_1"}

    await restarted.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    for task_id in tasks.values():
        with factory() as session:
            assert session.get(Task, task_id).status is TaskStatus.SUCCEEDED
        assert_launched_once(factory, task_id)
