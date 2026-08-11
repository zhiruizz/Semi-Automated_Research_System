from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from research_controller.compute.registry import ProviderRegistry
from research_controller.protocols.compute import ComputeTaskSpec, ResourceSnapshot


@dataclass(frozen=True)
class ComputeRoute:
    provider_id: str
    resource_class: str
    snapshot: ResourceSnapshot
    provider_metadata: dict[str, Any]


class RouteFailureReason(StrEnum):
    NO_CAPABLE_RESOURCE = "NO_CAPABLE_RESOURCE"
    TEMPORARILY_BUSY = "TEMPORARILY_BUSY"


class NoFeasibleComputeRoute(RuntimeError):
    def __init__(
        self,
        reason: RouteFailureReason,
        *,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.retryable = retryable
        self.details = details or {}


def _offer_is_capable(spec: ComputeTaskSpec, offer: Any) -> bool:
    if not offer.operational:
        return False
    if not set(spec.resources.required_capabilities).issubset(set(offer.capabilities)):
        return False
    if spec.resources.gpu_count == 0:
        return offer.gpu_per_node == 0
    if spec.resources.gpu_count != 1:
        return False
    return (
        offer.gpu_per_node == 1
        and (offer.gpu_memory_gb or 0) >= spec.resources.min_gpu_memory_gb
    )


class ComputeRouter:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry

    async def route(
        self,
        spec: ComputeTaskSpec,
        *,
        snapshot_overlay: Callable[[ResourceSnapshot], ResourceSnapshot] | None = None,
    ) -> ComputeRoute:
        physically_capable = False
        for provider_id in spec.routing.allowed_providers:
            provider = self.registry.get(provider_id)
            health = await provider.probe()
            if not health.can_submit:
                continue
            snapshot = await provider.discover_resources()
            if snapshot_overlay is not None:
                snapshot = snapshot_overlay(snapshot)
            if not await provider.can_run(spec, snapshot):
                continue
            capable_offers = [
                offer for offer in snapshot.offers if _offer_is_capable(spec, offer)
            ]
            if not capable_offers:
                continue
            physically_capable = True
            for offer in capable_offers:
                if not offer.schedulable:
                    continue
                allocation: dict[str, Any] = {"allocation_type": "cpu"}
                if spec.resources.gpu_count == 1:
                    allocation = {
                        "gpu_index": offer.metadata.get("gpu_index"),
                        "gpu_uuid": offer.metadata.get("gpu_uuid"),
                        "gpu_model": offer.gpu_model,
                        "allocation_type": "exclusive",
                    }
                return ComputeRoute(
                    provider_id,
                    offer.resource_class,
                    snapshot,
                    {"allocation": allocation},
                )
        if physically_capable:
            raise NoFeasibleComputeRoute(
                RouteFailureReason.TEMPORARILY_BUSY,
                retryable=True,
            )
        raise NoFeasibleComputeRoute(
            RouteFailureReason.NO_CAPABLE_RESOURCE,
            retryable=False,
        )
