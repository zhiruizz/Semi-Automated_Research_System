from __future__ import annotations

from abc import ABC, abstractmethod

from research_controller.protocols.agent import (
    AgentExecutionRequest,
    AgentObservation,
    AgentResult,
    AgentRunView,
    ExternalAgentRun,
)


class AgentAdapterError(RuntimeError):
    """A classified backend failure safe to persist in the Controller DB."""

    def __init__(self, error_type: str, message: str, *, block_task: bool = False) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.block_task = block_task


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

    async def respond_approval(self, run: AgentRunView, choice: str) -> dict[str, object]:
        raise AgentAdapterError(
            "APPROVAL_NOT_SUPPORTED",
            f"adapter {self.adapter_id} does not support approvals",
        )
