from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_controller.db.models import ComputeJob
from research_controller.domain.enums import ComputeExecutionStatus
from research_controller.protocols.compute import ResourceSnapshot


ACTIVE_GPU_RESERVATION_STATUSES = {
    ComputeExecutionStatus.CREATED,
    ComputeExecutionStatus.SUBMITTED,
    ComputeExecutionStatus.PENDING,
    ComputeExecutionStatus.RUNNING,
}


class ReservationConflict(RuntimeError):
    pass


class LocalGpuAllocationService:
    """Controller-side durable availability overlay backed by ComputeJob rows."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    @staticmethod
    def is_gpu_resource(resource_class: str) -> bool:
        return resource_class.startswith("local_gpu_")

    @staticmethod
    def is_reserved(session: Session, provider_id: str, resource_class: str) -> bool:
        return (
            session.scalar(
                select(ComputeJob.id).where(
                    ComputeJob.provider_id == provider_id,
                    ComputeJob.resource_class == resource_class,
                    ComputeJob.execution_status.in_(ACTIVE_GPU_RESERVATION_STATUSES),
                ).limit(1)
            )
            is not None
        )

    def active_reservations(self) -> dict[str, str]:
        with self.session_factory() as session:
            jobs = session.scalars(
                select(ComputeJob)
                .where(
                    ComputeJob.provider_id == "local",
                    ComputeJob.resource_class.like("local_gpu_%"),
                    ComputeJob.execution_status.in_(ACTIVE_GPU_RESERVATION_STATUSES),
                )
                .order_by(ComputeJob.created_at)
            ).all()
        reservations: dict[str, str] = {}
        for job in jobs:
            existing = reservations.get(job.resource_class)
            if existing is not None and existing != job.id:
                raise ReservationConflict(
                    f"multiple active jobs reserve {job.resource_class}: {existing}, {job.id}"
                )
            reservations[job.resource_class] = job.id
        return reservations

    def overlay(self, snapshot: ResourceSnapshot) -> ResourceSnapshot:
        if snapshot.provider_id != "local":
            return snapshot
        reservations = self.active_reservations()
        offers = []
        for offer in snapshot.offers:
            job_id = reservations.get(offer.resource_class)
            if job_id is None:
                offers.append(offer)
                continue
            metadata = {
                **offer.metadata,
                "controller_reserved": True,
                "busy_reason": "controller_reserved",
                "reserved_by_compute_job_id": job_id,
            }
            offers.append(
                offer.model_copy(
                    update={
                        "schedulable": False,
                        "idle_nodes": 0,
                        "allocated_nodes": max(1, offer.allocated_nodes),
                        "metadata": metadata,
                    }
                )
            )
        return snapshot.model_copy(update={"offers": offers})
