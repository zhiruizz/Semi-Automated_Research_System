from __future__ import annotations

from abc import ABC, abstractmethod

from research_controller.protocols.compute import (
    ArtifactCandidate,
    ComputeJobView,
    ComputeTaskSpec,
    JobObservation,
    PreparedJob,
    ProviderHealth,
    ResourceSnapshot,
)


class ComputeProvider(ABC):
    provider_id: str

    @abstractmethod
    async def probe(self) -> ProviderHealth: ...

    @abstractmethod
    async def discover_resources(self) -> ResourceSnapshot: ...

    @abstractmethod
    async def can_run(self, spec: ComputeTaskSpec, snapshot: ResourceSnapshot) -> bool: ...

    @abstractmethod
    async def prepare(self, spec: ComputeTaskSpec, resource_class: str) -> PreparedJob: ...

    @abstractmethod
    async def submit(self, prepared: PreparedJob) -> str: ...

    @abstractmethod
    async def reconcile_submission(self, submission_key: str) -> str | None: ...

    @abstractmethod
    async def poll(self, job: ComputeJobView) -> JobObservation: ...

    @abstractmethod
    async def collect(self, job: ComputeJobView) -> list[ArtifactCandidate]: ...

    @abstractmethod
    async def cancel(self, job: ComputeJobView) -> None: ...
