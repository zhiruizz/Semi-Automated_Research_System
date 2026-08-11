from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any
from uuid import uuid4

from research_controller.compute.base import ComputeProvider
from research_controller.compute.local.nvidia_smi import NvidiaSmiClient
from research_controller.domain.enums import (
    AccessMode,
    AccessStatus,
    ComputeExecutionStatus,
    FailureClass,
    HealthLevel,
    ObservationStatus,
    ResourceState,
)
from research_controller.protocols.compute import (
    ArtifactCandidate,
    ComputeJobView,
    ComputeTaskSpec,
    JobObservation,
    PreparedJob,
    ProviderHealth,
    ResourceOffer,
    ResourceSnapshot,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


@dataclass(frozen=True)
class LocalGpuPolicy:
    avoid_external_busy: bool = True
    external_memory_threshold_mb: int = 1_024
    external_utilization_threshold_percent: int = 20


class LocalProvider(ComputeProvider):
    provider_id = "local"

    def __init__(
        self,
        root: Path | str,
        *,
        nvidia_smi: NvidiaSmiClient | None = None,
        gpu_policy: LocalGpuPolicy | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.jobs_root = self.root / "jobs"
        self.submissions_root = self.root / "submissions"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.submissions_root.mkdir(parents=True, exist_ok=True)
        self.nvidia_smi = nvidia_smi or NvidiaSmiClient()
        self.gpu_policy = gpu_policy or LocalGpuPolicy()

    def _submission_path(self, submission_key: str) -> Path:
        digest = hashlib.sha256(submission_key.encode("utf-8")).hexdigest()
        return self.submissions_root / f"{digest}.json"

    async def probe(self) -> ProviderHealth:
        checked = _now()
        writable = os.access(self.root, os.W_OK)
        return ProviderHealth(
            provider_id=self.provider_id,
            checked_at=checked,
            valid_until=checked + timedelta(seconds=30),
            level=HealthLevel.HEALTHY if writable else HealthLevel.UNAVAILABLE,
            access=AccessStatus.REACHABLE,
            transport="local_process",
            scheduler="local_process",
            storage="local_filesystem",
            can_submit=writable,
            can_poll=True,
            can_collect=True,
            reasons=[] if writable else ["provider root is not writable"],
            metadata={"root": str(self.root)},
        )

    async def discover_resources(self) -> ResourceSnapshot:
        observed = _now()
        gpus = await self.nvidia_smi.inventory()
        compute_processes = await self.nvidia_smi.compute_processes()
        process_uuids = {process.gpu_uuid for process in compute_processes}
        process_counts: dict[str, int] = {}
        for process in compute_processes:
            process_counts[process.gpu_uuid] = process_counts.get(process.gpu_uuid, 0) + 1
        offers = [
            ResourceOffer(
                resource_class="local_cpu",
                resource_state=ResourceState.UP,
                operational=True,
                schedulable=True,
                gpu_per_node=0,
                idle_nodes=1,
                capabilities=["cpu", "local_process"],
                access_mode=AccessMode.AUTOMATIC,
                metadata={"cpu_count": os.cpu_count() or 1},
            )
        ]
        for gpu in gpus:
            external_busy = False
            busy_reason: str | None = None
            if gpu.uuid in process_uuids:
                external_busy = True
                busy_reason = "external_process"
            elif gpu.memory_used_mb >= self.gpu_policy.external_memory_threshold_mb:
                external_busy = True
                busy_reason = "external_memory"
            elif (
                gpu.utilization_percent
                >= self.gpu_policy.external_utilization_threshold_percent
            ):
                external_busy = True
                busy_reason = "external_utilization"
            schedulable = not (
                external_busy and self.gpu_policy.avoid_external_busy
            )
            offers.append(
                ResourceOffer(
                    resource_class=f"local_gpu_{gpu.index}",
                    resource_state=ResourceState.UP,
                    operational=True,
                    schedulable=schedulable,
                    gpu_model=gpu.name,
                    gpu_memory_gb=round(gpu.memory_total_mb / 1024, 3),
                    gpu_per_node=1,
                    idle_nodes=1 if schedulable else 0,
                    allocated_nodes=0 if schedulable else 1,
                    capabilities=["cpu", "cuda", "local_process"],
                    access_mode=AccessMode.AUTOMATIC,
                    metadata={
                        "gpu_index": gpu.index,
                        "gpu_uuid": gpu.uuid,
                        "gpu_model": gpu.name,
                        "memory_total_mb": gpu.memory_total_mb,
                        "memory_used_mb": gpu.memory_used_mb,
                        "utilization_percent": gpu.utilization_percent,
                        "controller_reserved": False,
                        "external_busy": external_busy,
                        "external_compute_process_count": process_counts.get(gpu.uuid, 0),
                        "busy_reason": busy_reason,
                    },
                )
            )
        return ResourceSnapshot(
            provider_id=self.provider_id,
            observed_at=observed,
            valid_until=observed + timedelta(seconds=15),
            offers=offers,
            metadata={
                "gpu_count": len(gpus),
                "external_compute_process_count": len(compute_processes),
            },
        )

    async def can_run(self, spec: ComputeTaskSpec, snapshot: ResourceSnapshot) -> bool:
        if spec.resources.gpu_count > 1:
            return False
        required_caps = set(spec.resources.required_capabilities)
        for offer in snapshot.offers:
            if not offer.operational:
                continue
            if not required_caps.issubset(set(offer.capabilities)):
                continue
            if spec.resources.gpu_count == 0 and offer.gpu_per_node == 0:
                return True
            if (
                spec.resources.gpu_count > 0
                and offer.gpu_per_node >= spec.resources.gpu_count
                and (offer.gpu_memory_gb or 0) >= spec.resources.min_gpu_memory_gb
            ):
                return True
        return False

    async def prepare(self, spec: ComputeTaskSpec, resource_class: str) -> PreparedJob:
        if not spec.execution.command:
            raise ValueError(
                "LocalProvider requires execution.command; artifact staging is deferred"
            )
        key_hash = hashlib.sha256(spec.submission_key.encode("utf-8")).hexdigest()[:16]
        workdir = self.jobs_root / spec.project_id / spec.task_id / key_hash
        workdir.mkdir(parents=True, exist_ok=True)
        env = dict(spec.execution.env)
        allocation: dict[str, Any]
        if resource_class == "local_cpu":
            if spec.resources.gpu_count != 0:
                raise ValueError("GPU Task cannot be prepared on local_cpu")
            allocation = {"allocation_type": "cpu"}
        elif resource_class.startswith("local_gpu_"):
            if spec.resources.gpu_count != 1:
                raise ValueError("LocalProvider supports exactly one GPU per GPU job")
            suffix = resource_class.removeprefix("local_gpu_")
            if not suffix.isdigit():
                raise ValueError(f"invalid local GPU resource class: {resource_class}")
            gpu_index = int(suffix)
            env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
            allocation = {
                "gpu_index": gpu_index,
                "allocation_type": "exclusive",
            }
        else:
            raise ValueError(f"unknown local resource class: {resource_class}")
        prepared = PreparedJob(
            provider_id=self.provider_id,
            submission_key=spec.submission_key,
            resource_class=resource_class,
            workdir=workdir,
            command=[*spec.execution.command, *spec.execution.argv],
            env=env,
            outputs=spec.outputs,
            metadata={
                "project_id": spec.project_id,
                "task_id": spec.task_id,
                "allocation": allocation,
            },
        )
        _atomic_json(
            workdir / "prepared.json",
            prepared.model_dump(mode="json"),
        )
        return prepared

    async def submit(self, prepared: PreparedJob) -> str:
        record_path = self._submission_path(prepared.submission_key)
        if record_path.exists():
            record = _load_json(record_path)
            if record.get("submission_key") != prepared.submission_key:
                raise RuntimeError("submission hash collision")
            if record.get("status") == "LAUNCHED":
                return str(record["external_job_id"])
        else:
            record = {
                "schema_version": "local-submission/v0.1",
                "submission_key": prepared.submission_key,
                "external_job_id": f"local-{uuid4().hex}",
                "workdir": str(prepared.workdir),
                "status": "RESERVED",
                "reserved_at": _now().isoformat(),
            }
            _atomic_json(record_path, record)

        environment = os.environ.copy()
        environment.update(prepared.env)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "research_controller.runner",
                "--workdir",
                str(prepared.workdir),
                "--",
                *prepared.command,
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        record.update(
            {"status": "LAUNCHED", "pid": process.pid, "launched_at": _now().isoformat()}
        )
        _atomic_json(record_path, record)
        return str(record["external_job_id"])

    async def reconcile_submission(self, submission_key: str) -> str | None:
        record_path = self._submission_path(submission_key)
        if not record_path.exists():
            return None
        record = _load_json(record_path)
        if record.get("submission_key") != submission_key:
            raise RuntimeError("submission hash collision")
        if record.get("status") != "LAUNCHED":
            return None
        return str(record["external_job_id"])

    @staticmethod
    def _read_delta(path: Path, offset: int) -> tuple[str, int]:
        if not path.exists():
            return "", offset
        size = path.stat().st_size
        start = min(max(offset, 0), size)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
        return data.decode("utf-8", errors="replace"), size

    @staticmethod
    def _telemetry(*chunks: str) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for chunk in chunks:
            for line in chunk.splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and any(
                    key in value for key in ("step", "loss", "tokens_per_sec", "elapsed_sec")
                ):
                    latest.update(value)
        return latest

    async def poll(self, job: ComputeJobView) -> JobObservation:
        observed = _now()
        workdir = job.remote_workdir
        stdout_offset = int(job.log_cursor.get("stdout", {}).get("offset", 0))
        stderr_offset = int(job.log_cursor.get("stderr", {}).get("offset", 0))
        stdout_delta, stdout_next = self._read_delta(workdir / "run.out", stdout_offset)
        stderr_delta, stderr_next = self._read_delta(workdir / "run.error", stderr_offset)
        cursor = {
            "stdout": {"path": str(workdir / "run.out"), "offset": stdout_next},
            "stderr": {"path": str(workdir / "run.error"), "offset": stderr_next},
        }
        progress = self._telemetry(stdout_delta, stderr_delta)
        exit_path = workdir / "exit.json"
        if exit_path.exists():
            exit_data = _load_json(exit_path)
            exit_code = int(exit_data["exit_code"])
            status = (
                ComputeExecutionStatus.SUCCEEDED
                if exit_code == 0
                else ComputeExecutionStatus.FAILED
            )
            failure = FailureClass.NONE if exit_code == 0 else FailureClass.CODE
            raw_state = "EXITED"
        else:
            record_path = self._submission_path(job.submission_key)
            record = _load_json(record_path) if record_path.exists() else {}
            pid = int(record.get("pid", 0))
            alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            if alive:
                status = ComputeExecutionStatus.RUNNING
                failure = FailureClass.NONE
                raw_state = "PROCESS_ALIVE"
                exit_code = None
            else:
                status = ComputeExecutionStatus.FAILED
                failure = FailureClass.INFRASTRUCTURE
                raw_state = "PROCESS_MISSING_WITHOUT_EXIT_RECORD"
                exit_code = None
        return JobObservation(
            provider_id=self.provider_id,
            compute_job_id=job.id,
            external_job_id=job.external_job_id,
            observed_at=observed,
            observation_status=ObservationStatus.FRESH,
            execution_status=status,
            raw_scheduler_state=raw_state,
            exit_code=exit_code,
            failure_class=failure,
            retryable=failure is FailureClass.INFRASTRUCTURE,
            progress=progress,
            log_deltas={"stdout": stdout_delta, "stderr": stderr_delta},
            log_cursor=cursor,
        )

    async def collect(self, job: ComputeJobView) -> list[ArtifactCandidate]:
        workdir = job.remote_workdir
        prepared = _load_json(workdir / "prepared.json")
        candidates: list[ArtifactCandidate] = []
        fixed = [
            ("run.out", "run.out", "LOG"),
            ("run.error", "run.error", "LOG"),
            ("exit.json", "exit.json", "RUN_EXIT"),
            ("manifest.json", "manifest.json", "RUN_MANIFEST"),
        ]
        seen: set[Path] = set()
        for logical_name, filename, kind in fixed:
            path = workdir / filename
            if path.exists():
                candidates.append(
                    ArtifactCandidate(
                        logical_name=logical_name,
                        path=path,
                        artifact_kind=kind,
                        required=True,
                    )
                )
                seen.add(path.resolve())
        for output in prepared.get("outputs", []):
            matches = sorted(workdir.glob(str(output["glob"])))
            for index, path in enumerate(matches):
                if not path.is_file() or path.resolve() in seen:
                    continue
                logical_name = str(output["logical_name"])
                if len(matches) > 1:
                    logical_name = f"{logical_name}:{index}:{path.name}"
                candidates.append(
                    ArtifactCandidate(
                        logical_name=logical_name,
                        path=path,
                        artifact_kind=str(output["artifact_kind"]),
                        required=bool(output.get("required", False)),
                        evidence_candidate=bool(output.get("evidence_candidate", False)),
                        metadata={"source_glob": output["glob"]},
                    )
                )
                seen.add(path.resolve())
        return candidates

    async def cancel(self, job: ComputeJobView) -> None:
        record_path = self._submission_path(job.submission_key)
        if not record_path.exists():
            return
        record = _load_json(record_path)
        pid = int(record.get("pid", 0))
        if pid <= 0:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
