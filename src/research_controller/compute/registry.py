from __future__ import annotations

from research_controller.compute.base import ComputeProvider


class ProviderRegistry:
    def __init__(self, providers: list[ComputeProvider] | None = None) -> None:
        self._providers = {provider.provider_id: provider for provider in providers or []}

    def register(self, provider: ComputeProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"provider already registered: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> ComputeProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise LookupError(f"unknown provider: {provider_id}") from exc

    def all(self) -> list[ComputeProvider]:
        return list(self._providers.values())
