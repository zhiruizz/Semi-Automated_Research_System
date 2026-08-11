from __future__ import annotations

from dataclasses import dataclass

from research_controller.compute.registry import ProviderRegistry
from research_controller.protocols.compute import ComputeTaskSpec, ResourceSnapshot


@dataclass(frozen=True)
class ComputeRoute:
    provider_id: str
    resource_class: str
    snapshot: ResourceSnapshot


class ComputeRouter:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    async def route(self, spec: ComputeTaskSpec) -> ComputeRoute:
        for provider_id in spec.routing.allowed_providers:
            provider = self.registry.get(provider_id)
            health = await provider.probe()
            if not health.can_submit:
                continue
            snapshot = await provider.discover_resources()
            if not await provider.can_run(spec, snapshot):
                continue
            for offer in snapshot.offers:
                if not offer.operational or not offer.schedulable:
                    continue
                if spec.resources.gpu_count == 0 and offer.gpu_per_node == 0:
                    return ComputeRoute(provider_id, offer.resource_class, snapshot)
                if (
                    spec.resources.gpu_count > 0
                    and offer.gpu_per_node >= spec.resources.gpu_count
                    and (offer.gpu_memory_gb or 0) >= spec.resources.min_gpu_memory_gb
                ):
                    return ComputeRoute(provider_id, offer.resource_class, snapshot)
        raise RuntimeError("no feasible compute provider")
