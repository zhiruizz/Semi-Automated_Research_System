from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from research_controller.artifacts.store import ArtifactStore
from research_controller.compute.registry import ProviderRegistry
from research_controller.db.models import ComputeJob, Task
from research_controller.domain.enums import (
    ComputeExecutionStatus,
    ObservationStatus,
    TaskStatus,
)
from research_controller.domain.ids import new_id
from research_controller.protocols.compute import ComputeJobView, ComputeTaskSpec, JobObservation
from research_controller.services.transitions import TransitionService


POLLABLE = {
    ComputeExecutionStatus.SUBMITTED,
    ComputeExecutionStatus.PENDING,
    ComputeExecutionStatus.RUNNING,
}


def job_view(job: ComputeJob) -> ComputeJobView:
    return ComputeJobView(
        id=job.id,
        project_id=job.project_id,
        task_id=job.task_id,
        provider_id=job.provider_id,
        resource_class=job.resource_class,
        submission_key=job.submission_key,
        external_job_id=job.external_job_id,
        remote_workdir=job.remote_workdir,
        execution_status=job.execution_status,
        observation_status=job.observation_status,
        log_cursor=job.log_cursor_json,
        provider_metadata=job.provider_metadata_json,
    )


class ComputeReconciler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: ProviderRegistry,
        artifact_store: ArtifactStore,
        *,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.artifact_store = artifact_store
        self.poll_interval_seconds = poll_interval_seconds
        self.transitions = TransitionService()

    async def reconcile_created(self) -> list[str]:
        with self.session_factory() as session:
            records = [
                (job_view(job), ComputeTaskSpec.model_validate(job.spec_json))
                for job in session.scalars(
                    select(ComputeJob).where(
                        ComputeJob.execution_status == ComputeExecutionStatus.CREATED
                    )
                ).all()
            ]
        reconciled: list[str] = []
        for view, spec in records:
            provider = self.registry.get(view.provider_id)
            external_id = await provider.reconcile_submission(view.submission_key)
            if external_id is None:
                prepared = await provider.prepare(spec, view.resource_class)
                external_id = await provider.submit(prepared)
            with self.session_factory.begin() as session:
                job = session.get(ComputeJob, view.id)
                if job is None or job.execution_status is not ComputeExecutionStatus.CREATED:
                    continue
                job.external_job_id = external_id
                self.transitions.transition_compute_job(
                    session,
                    job,
                    ComputeExecutionStatus.SUBMITTED,
                    expected_status=ComputeExecutionStatus.CREATED,
                    correlation_id=new_id("corr"),
                    payload={"external_job_id": external_id},
                )
                reconciled.append(job.id)
        return reconciled

    async def reconcile_due(self) -> list[str]:
        await self._reconcile_collecting()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            views = [
                job_view(job)
                for job in session.scalars(
                    select(ComputeJob).where(
                        ComputeJob.execution_status.in_(POLLABLE),
                        (ComputeJob.next_poll_at.is_(None) | (ComputeJob.next_poll_at <= now)),
                    )
                ).all()
            ]
        observed_ids: list[str] = []
        for view in views:
            provider = self.registry.get(view.provider_id)
            try:
                observation = await provider.poll(view)
            except Exception as exc:
                with self.session_factory.begin() as session:
                    job = session.get(ComputeJob, view.id)
                    if job is None:
                        continue
                    self.transitions.update_observation_status(
                        session,
                        job,
                        ObservationStatus.POLL_ERROR,
                        payload={"error_type": type(exc).__name__},
                    )
                    job.last_poll_at = now
                    job.next_poll_at = now + timedelta(seconds=self.poll_interval_seconds)
                continue
            self._record_observation(observation)
            observed_ids.append(view.id)
        await self._reconcile_collecting()
        return observed_ids

    def _record_observation(self, observation: JobObservation) -> None:
        terminal = {
            ComputeExecutionStatus.SUCCEEDED,
            ComputeExecutionStatus.FAILED,
            ComputeExecutionStatus.OOM,
            ComputeExecutionStatus.TIMEOUT,
            ComputeExecutionStatus.CANCELLED,
        }
        with self.session_factory.begin() as session:
            job = session.get(ComputeJob, observation.compute_job_id)
            if job is None or job.execution_status not in POLLABLE:
                return
            correlation_id = new_id("corr")
            self.transitions.update_observation_status(
                session,
                job,
                observation.observation_status,
                correlation_id=correlation_id,
            )
            job.last_poll_at = observation.observed_at
            job.last_confirmed_at = (
                observation.observed_at
                if observation.observation_status is ObservationStatus.FRESH
                else job.last_confirmed_at
            )
            job.next_poll_at = observation.observed_at + timedelta(
                seconds=self.poll_interval_seconds
            )
            job.log_cursor_json = observation.log_cursor
            if observation.progress:
                job.runtime_summary_json = observation.progress
                job.last_progress_at = observation.observed_at
            if observation.observation_status is not ObservationStatus.FRESH:
                return
            if observation.execution_status is ComputeExecutionStatus.RUNNING:
                if job.execution_status is not ComputeExecutionStatus.RUNNING:
                    self.transitions.transition_compute_job(
                        session,
                        job,
                        ComputeExecutionStatus.RUNNING,
                        correlation_id=correlation_id,
                    )
                return
            if observation.execution_status in terminal:
                if job.execution_status in {
                    ComputeExecutionStatus.SUBMITTED,
                    ComputeExecutionStatus.PENDING,
                }:
                    self.transitions.transition_compute_job(
                        session,
                        job,
                        ComputeExecutionStatus.RUNNING,
                        correlation_id=correlation_id,
                        payload={"reconciled_fast_completion": True},
                    )
                job.exit_code = observation.exit_code
                job.failure_class = observation.failure_class
                job.provider_metadata_json = {
                    **job.provider_metadata_json,
                    "observed_terminal_status": observation.execution_status.value,
                    "raw_scheduler_state": observation.raw_scheduler_state,
                }
                self.transitions.transition_compute_job(
                    session,
                    job,
                    ComputeExecutionStatus.COLLECTING,
                    correlation_id=correlation_id,
                )

    async def _reconcile_collecting(self) -> list[str]:
        with self.session_factory() as session:
            views = [
                job_view(job)
                for job in session.scalars(
                    select(ComputeJob).where(
                        ComputeJob.execution_status == ComputeExecutionStatus.COLLECTING
                    )
                ).all()
            ]
        collected: list[str] = []
        for view in views:
            provider = self.registry.get(view.provider_id)
            candidates = await provider.collect(view)
            correlation_id = new_id("corr")
            for candidate in candidates:
                with self.session_factory.begin() as session:
                    self.artifact_store.ingest_file(
                        session,
                        project_id=view.project_id,
                        task_id=view.task_id,
                        source=candidate.path,
                        logical_name=candidate.logical_name,
                        kind=candidate.artifact_kind,
                        producer_type="COMPUTE_JOB",
                        producer_ref_id=view.id,
                        evidence_eligible=False,
                        metadata={
                            **candidate.metadata,
                            "evidence_candidate": candidate.evidence_candidate,
                        },
                        correlation_id=correlation_id,
                    )
            with self.session_factory.begin() as session:
                job = session.get(ComputeJob, view.id)
                if job is None or job.execution_status is not ComputeExecutionStatus.COLLECTING:
                    continue
                raw_terminal = job.provider_metadata_json.get("observed_terminal_status", "FAILED")
                terminal = ComputeExecutionStatus(raw_terminal)
                self.transitions.transition_compute_job(
                    session,
                    job,
                    terminal,
                    expected_status=ComputeExecutionStatus.COLLECTING,
                    correlation_id=correlation_id,
                )
                task = session.get(Task, job.task_id)
                if task is not None and task.status is TaskStatus.RUNNING:
                    self.transitions.transition_task(
                        session,
                        task,
                        TaskStatus.VERIFYING,
                        expected_status=TaskStatus.RUNNING,
                        correlation_id=correlation_id,
                    )
                collected.append(job.id)
        return collected
