from __future__ import annotations

import pytest
from sqlalchemy import select

from research_controller.db.models import AgentRun, Event, Task
from research_controller.domain.enums import AgentRunStatus
from research_controller.services.project_state import ProjectStateService
from research_controller.services.transitions import InvalidTransition, TransitionService
from tests.conftest import create_agent_task


def create_run(factory, task_id: str) -> str:
    projects = ProjectStateService()
    transitions = TransitionService(projects.events)
    with factory.begin() as session:
        task = session.get(Task, task_id)
        run = projects.create_agent_run(
            session,
            task=task,
            backend="mock",
            role="implementation_worker",
            session_id="session-1",
            mode="new",
            config={},
            correlation_id="corr-create",
        )
        transitions.transition_agent_run(session, run, AgentRunStatus.STARTING)
        return run.id


def test_agent_run_transition_legality_and_events(runtime, tmp_path):
    _engine, factory, _workspace = runtime
    _project, task = create_agent_task(factory, tmp_path)
    run_id = create_run(factory, task.id)
    transitions = TransitionService()
    with factory.begin() as session:
        run = session.get(AgentRun, run_id)
        transitions.transition_agent_run(session, run, AgentRunStatus.RUNNING)
        transitions.transition_agent_run(session, run, AgentRunStatus.SUCCEEDED)
        with pytest.raises(InvalidTransition):
            transitions.transition_agent_run(session, run, AgentRunStatus.RUNNING)
    with factory() as session:
        types = session.scalars(
            select(Event.event_type).where(Event.entity_id == run_id).order_by(Event.seq)
        ).all()
        assert types == [
            "AGENT_RUN_CREATED",
            "AGENT_RUN_STARTING",
            "AGENT_RUN_STARTED",
            "AGENT_RUN_SUCCEEDED",
        ]


def test_direct_agent_run_status_mutation_is_rejected(runtime, tmp_path):
    _engine, factory, _workspace = runtime
    _project, task = create_agent_task(factory, tmp_path)
    run_id = create_run(factory, task.id)
    with pytest.raises(ValueError, match="TransitionService"):
        with factory.begin() as session:
            run = session.get(AgentRun, run_id)
            run.status = AgentRunStatus.SUCCEEDED
            session.flush()
