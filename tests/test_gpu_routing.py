from __future__ import annotations

from pathlib import Path

import pytest

from research_controller.compute.local.provider import LocalProvider
from research_controller.compute.registry import ProviderRegistry
from research_controller.compute.router import (
    ComputeRouter,
    NoFeasibleComputeRoute,
    RouteFailureReason,
)
from research_controller.protocols.compute import ComputeTaskSpec
from tests.test_nvidia_smi import FakeNvidiaSmiClient, two_gpus


def gpu_spec(*, memory_gb: float = 16, gpu_count: int = 1) -> ComputeTaskSpec:
    return ComputeTaskSpec.model_validate(
        {
            "schema_version": "compute-task-spec/v0.1",
            "project_id": "project",
            "task_id": "task",
            "submission_key": "submission",
            "execution": {"command": ["true"]},
            "resources": {
                "gpu_count": gpu_count,
                "min_gpu_memory_gb": memory_gb,
                "required_capabilities": ["cuda"],
            },
            "routing": {"allowed_providers": ["local"]},
        }
    )


@pytest.mark.asyncio
async def test_router_avoids_external_busy_gpu(tmp_path: Path):
    inventory = two_gpus()
    inventory[0] = inventory[0].__class__(
        index=0,
        uuid="GPU-0",
        name="NVIDIA Test GPU",
        memory_total_mb=20_480,
        memory_used_mb=2_048,
        utilization_percent=0,
    )
    provider = LocalProvider(tmp_path, nvidia_smi=FakeNvidiaSmiClient(inventory))
    route = await ComputeRouter(ProviderRegistry([provider])).route(gpu_spec())
    assert route.resource_class == "local_gpu_1"


@pytest.mark.asyncio
async def test_router_distinguishes_busy_from_incapable(tmp_path: Path):
    busy_provider = LocalProvider(
        tmp_path / "busy",
        nvidia_smi=FakeNvidiaSmiClient(two_gpus(memory_used_mb=2_048)),
    )
    with pytest.raises(NoFeasibleComputeRoute) as busy:
        await ComputeRouter(ProviderRegistry([busy_provider])).route(gpu_spec())
    assert busy.value.reason is RouteFailureReason.TEMPORARILY_BUSY
    assert busy.value.retryable is True

    capable_provider = LocalProvider(
        tmp_path / "small",
        nvidia_smi=FakeNvidiaSmiClient(two_gpus()),
    )
    with pytest.raises(NoFeasibleComputeRoute) as incapable:
        await ComputeRouter(ProviderRegistry([capable_provider])).route(
            gpu_spec(memory_gb=24)
        )
    assert incapable.value.reason is RouteFailureReason.NO_CAPABLE_RESOURCE
    assert incapable.value.retryable is False


@pytest.mark.asyncio
async def test_local_multi_gpu_is_explicitly_unsupported(tmp_path: Path):
    provider = LocalProvider(tmp_path, nvidia_smi=FakeNvidiaSmiClient(two_gpus()))
    with pytest.raises(NoFeasibleComputeRoute) as failure:
        await ComputeRouter(ProviderRegistry([provider])).route(gpu_spec(gpu_count=2))
    assert failure.value.reason is RouteFailureReason.NO_CAPABLE_RESOURCE


@pytest.mark.asyncio
async def test_prepare_uses_selected_resource_without_rediscovery(tmp_path: Path):
    class CountingClient(FakeNvidiaSmiClient):
        inventory_calls = 0

        async def inventory(self):
            self.inventory_calls += 1
            return await super().inventory()

    client = CountingClient(two_gpus())
    provider = LocalProvider(tmp_path, nvidia_smi=client)
    prepared = await provider.prepare(gpu_spec(), "local_gpu_1")
    assert client.inventory_calls == 0
    assert prepared.env["CUDA_VISIBLE_DEVICES"] == "1"
    assert prepared.metadata["allocation"] == {
        "gpu_index": 1,
        "allocation_type": "exclusive",
    }
