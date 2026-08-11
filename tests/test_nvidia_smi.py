from __future__ import annotations

from pathlib import Path

import pytest

from research_controller.compute.local.nvidia_smi import (
    GpuComputeProcess,
    GpuInventory,
    parse_compute_processes,
    parse_gpu_inventory,
)
from research_controller.compute.local.provider import LocalGpuPolicy, LocalProvider


class FakeNvidiaSmiClient:
    def __init__(
        self,
        inventory: list[GpuInventory],
        processes: list[GpuComputeProcess] | None = None,
    ) -> None:
        self._inventory = inventory
        self._processes = processes or []

    async def inventory(self) -> list[GpuInventory]:
        return list(self._inventory)

    async def compute_processes(self) -> list[GpuComputeProcess]:
        return list(self._processes)


def two_gpus(*, memory_used_mb: int = 0, utilization_percent: int = 0):
    return [
        GpuInventory(
            index=index,
            uuid=f"GPU-{index}",
            name="NVIDIA Test GPU",
            memory_total_mb=20_480,
            memory_used_mb=memory_used_mb,
            utilization_percent=utilization_percent,
        )
        for index in range(2)
    ]


def test_parse_gpu_inventory_is_pure_and_stable():
    parsed = parse_gpu_inventory(
        "0, GPU-a, NVIDIA GeForce RTX 3080, 20480, 123, 7\n"
        "1, GPU-b, NVIDIA GeForce RTX 3080, 20480, 456, 9\n"
    )
    assert parsed == [
        GpuInventory(0, "GPU-a", "NVIDIA GeForce RTX 3080", 20480, 123, 7),
        GpuInventory(1, "GPU-b", "NVIDIA GeForce RTX 3080", 20480, 456, 9),
    ]


def test_parse_compute_processes_handles_empty_and_rows():
    assert parse_compute_processes("") == []
    assert parse_compute_processes("GPU-a, 1234, 2048\n") == [
        GpuComputeProcess(gpu_uuid="GPU-a", pid=1234, used_memory_mb=2048)
    ]


@pytest.mark.asyncio
async def test_resource_snapshot_marks_external_process_busy(tmp_path: Path):
    provider = LocalProvider(
        tmp_path,
        nvidia_smi=FakeNvidiaSmiClient(
            two_gpus(),
            [GpuComputeProcess(gpu_uuid="GPU-0", pid=42, used_memory_mb=512)],
        ),
    )
    snapshot = await provider.discover_resources()
    gpu0, gpu1 = snapshot.offers[1:]
    assert gpu0.operational is True
    assert gpu0.schedulable is False
    assert gpu0.metadata["external_busy"] is True
    assert gpu0.metadata["busy_reason"] == "external_process"
    assert gpu1.schedulable is True
    assert gpu1.metadata["gpu_uuid"] == "GPU-1"


@pytest.mark.asyncio
async def test_external_memory_and_utilization_thresholds_are_policy(tmp_path: Path):
    provider = LocalProvider(
        tmp_path,
        nvidia_smi=FakeNvidiaSmiClient(two_gpus(memory_used_mb=900)),
        gpu_policy=LocalGpuPolicy(
            avoid_external_busy=True,
            external_memory_threshold_mb=1_024,
            external_utilization_threshold_percent=20,
        ),
    )
    snapshot = await provider.discover_resources()
    assert all(offer.schedulable for offer in snapshot.offers[1:])
