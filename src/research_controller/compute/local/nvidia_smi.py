from __future__ import annotations

import asyncio
from dataclasses import dataclass


GPU_QUERY = "index,uuid,name,memory.total,memory.used,utilization.gpu"
COMPUTE_PROCESS_QUERY = "gpu_uuid,pid,used_gpu_memory"


@dataclass(frozen=True)
class GpuInventory:
    index: int
    uuid: str
    name: str
    memory_total_mb: int
    memory_used_mb: int
    utilization_percent: int


@dataclass(frozen=True)
class GpuComputeProcess:
    gpu_uuid: str
    pid: int
    used_memory_mb: int | None


def _parse_int(value: str) -> int:
    return int(value.strip())


def parse_gpu_inventory(output: str) -> list[GpuInventory]:
    """Parse deterministic no-header/no-unit nvidia-smi GPU CSV output."""
    devices: list[GpuInventory] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        pieces = [piece.strip() for piece in line.split(",", 5)]
        if len(pieces) != 6:
            continue
        try:
            devices.append(
                GpuInventory(
                    index=_parse_int(pieces[0]),
                    uuid=pieces[1],
                    name=pieces[2],
                    memory_total_mb=_parse_int(pieces[3]),
                    memory_used_mb=_parse_int(pieces[4]),
                    utilization_percent=_parse_int(pieces[5]),
                )
            )
        except ValueError:
            continue
    return devices


def parse_compute_processes(output: str) -> list[GpuComputeProcess]:
    """Parse compute-app CSV; an unknown memory amount still represents a process."""
    processes: list[GpuComputeProcess] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        pieces = [piece.strip() for piece in line.split(",", 2)]
        if len(pieces) != 3:
            continue
        try:
            pid = _parse_int(pieces[1])
        except ValueError:
            continue
        try:
            used_memory_mb: int | None = _parse_int(pieces[2])
        except ValueError:
            used_memory_mb = None
        processes.append(
            GpuComputeProcess(
                gpu_uuid=pieces[0],
                pid=pid,
                used_memory_mb=used_memory_mb,
            )
        )
    return processes


class NvidiaSmiClient:
    def __init__(self, executable: str = "nvidia-smi") -> None:
        self.executable = executable

    async def _query(self, query_flag: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                query_flag,
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return None
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        return stdout.decode("utf-8", errors="replace")

    async def inventory(self) -> list[GpuInventory]:
        output = await self._query(f"--query-gpu={GPU_QUERY}")
        return parse_gpu_inventory(output or "")

    async def compute_processes(self) -> list[GpuComputeProcess]:
        output = await self._query(f"--query-compute-apps={COMPUTE_PROCESS_QUERY}")
        return parse_compute_processes(output or "")
