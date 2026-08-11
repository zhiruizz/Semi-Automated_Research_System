from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from research_controller.db.models import Event, Task
from research_controller.domain.enums import TaskStatus
from research_controller.services.project_state import ProjectStateService
from research_controller.services.task_readiness import TaskReadinessService
from research_controller.services.transitions import TransitionService
from tests.conftest import create_compute_task


def test_task_dependency(runtime, tmp_path):
    _engine, factory, _workspace = runtime
    project, first = create_compute_task(factory, tmp_path)
    service = ProjectStateService()
    with factory.begin() as session:
        first_db = session.get(Task, first.id)
        second = service.create_task(
            session,
            project_id=project.id,
            stage=first_db.stage,
            kind=first_db.kind,
            action="dependent",
            executor=first_db.executor,
            idempotency_key="dependent",
            spec=first_db.spec_json,
            dependency_ids=[first.id],
        )
        second_id = second.id
    readiness = TaskReadinessService()
    transitions = TransitionService()
    with factory.begin() as session:
        readiness.reconcile(session)
    with factory() as session:
        assert session.get(Task, first.id).status is TaskStatus.READY
        assert session.get(Task, second_id).status is TaskStatus.PENDING
    with factory.begin() as session:
        first_db = session.get(Task, first.id)
        transitions.transition_task(session, first_db, TaskStatus.RUNNING)
        transitions.transition_task(session, first_db, TaskStatus.VERIFYING)
        transitions.transition_task(session, first_db, TaskStatus.SUCCEEDED)
        readiness.reconcile(session)
    with factory() as session:
        assert session.get(Task, second_id).status is TaskStatus.READY


def test_task_creation_is_idempotent(runtime, tmp_path):
    _engine, factory, _workspace = runtime
    project, task = create_compute_task(factory, tmp_path, idempotency_key="stable-key")
    service = ProjectStateService()
    with factory.begin() as session:
        existing = service.create_task(
            session,
            project_id=project.id,
            stage=task.stage,
            kind=task.kind,
            action="ignored duplicate",
            executor=task.executor,
            idempotency_key="stable-key",
            spec=task.spec_json,
        )
        assert existing.id == task.id
    with factory() as session:
        assert session.scalar(select(func.count(Task.id))) == 1


def test_expired_lease_recovers_without_external_work(runtime, tmp_path):
    _engine, factory, _workspace = runtime
    _project, task = create_compute_task(factory, tmp_path)
    readiness = TaskReadinessService()
    transitions = TransitionService()
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    with factory.begin() as session:
        readiness.reconcile(session)
        persistent = session.get(Task, task.id)
        transitions.transition_task(session, persistent, TaskStatus.RUNNING)
        persistent.lease_owner = "dead-controller"
        persistent.lease_expires_at = expired
    with factory.begin() as session:
        recovered = readiness.recover_expired_leases(session)
        assert recovered == [task.id]
    with factory() as session:
        persistent = session.get(Task, task.id)
        assert persistent.status is TaskStatus.READY
        event_types = session.scalars(
            select(Event.event_type).where(Event.entity_id == task.id).order_by(Event.seq)
        ).all()
        assert "TASK_LEASE_EXPIRED" in event_types
        assert "TASK_RETRIED" in event_types
