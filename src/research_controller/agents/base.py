from __future__ import annotations

from abc import ABC, abstractmethod

from research_controller.protocols.agent import (
    AgentExecutionRequest,
    AgentObservation,
    AgentResult,
    AgentRunView,
    ExternalAgentRun,
)


class AgentAdapter(ABC):
    adapter_id: str

    @abstractmethod
    async def start(self, request: AgentExecutionRequest) -> ExternalAgentRun: ...

    @abstractmethod
    async def reconcile(self, run_key: str) -> ExternalAgentRun | None: ...

    @abstractmethod
    async def poll(self, run: AgentRunView) -> AgentObservation: ...

    @abstractmethod
    async def get_result(self, run: AgentRunView) -> AgentResult: ...

    @abstractmethod
    async def cancel(self, run: AgentRunView) -> None: ...
