from __future__ import annotations

from collections.abc import Iterable

from research_controller.agents.base import AgentAdapter


class AgentAdapterRegistry:
    def __init__(self, adapters: Iterable[AgentAdapter] = ()) -> None:
        self._adapters = {adapter.adapter_id: adapter for adapter in adapters}

    def register(self, adapter: AgentAdapter) -> None:
        self._adapters[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> AgentAdapter:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise KeyError(f"unknown Agent adapter: {adapter_id}") from exc
