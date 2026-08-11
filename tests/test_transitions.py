from __future__ import annotations

import pytest
from sqlalchemy import func, select

from research_controller.db.models import Event, Task
from research_controller.domain.enums import TaskStatus
from research_controller.services.transitions import InvalidTransition, TransitionService
from tests.conftest import create_compute_task


def test_transition_legality(runtime, tmp_path):
    _engine, factory, _workspace = runtime
    _project, task = create_compute_task(factory, tmp_path)
    transitions = TransitionService()
    with factory.begin() as session:
        persistent = session.get(Task, task.id)
        transitions.transition_task(session, persistent, TaskStatus.READY)
        transitions.transition_task(session, persistent, TaskStatus.RUNNING)
        transitions.transition_task(session, persistent, TaskStatus.VERIFYING)
        transitions.transition_task(session, persistent, TaskStatus.SUCCEEDED)
    with factory.begin() as session:
        persistent = session.get(Task, task.id)
        with pytest.raises(InvalidTransition):
            transitions.transition_task(session, persistent, TaskStatus.READY)


def test_transition_and_event_rollback_atomically(runtime, tmp_path):
    _engine, factory, _workspace = runtime
    _project, task = create_compute_task(factory, tmp_path)
    transitions = TransitionService()
    with pytest.raises(RuntimeError):
        with factory.begin() as session:
            persistent = session.get(Task, task.id)
            transitions.transition_task(session, persistent, TaskStatus.READY)
            session.flush()
            raise RuntimeError("simulate transaction failure")
    with factory() as session:
        persistent = session.get(Task, task.id)
        assert persistent.status is TaskStatus.PENDING
        assert session.scalar(select(func.count(Event.id))) == 2


def test_direct_task_status_mutation_is_rejected(runtime, tmp_path):
    _engine, factory, _workspace = runtime
    _project, task = create_compute_task(factory, tmp_path)
    with pytest.raises(ValueError, match="TransitionService"):
        with factory.begin() as session:
            persistent = session.get(Task, task.id)
            persistent.status = TaskStatus.SUCCEEDED
            session.flush()
