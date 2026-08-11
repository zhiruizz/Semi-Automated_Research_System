from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Artifact, Event, Project, Task
from research_controller.domain.enums import AgentRunStatus, ProjectStage, TaskStatus
from tests.conftest import create_agent_task


@pytest.mark.asyncio
async def test_mock_agent_success(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path)
    controller = ResearchController(factory, workspace, poll_interval_seconds=0)
    await controller.run_until_idle(timeout_seconds=10, sleep_seconds=0.01)
    with factory() as session:
        persistent = session.get(Task, task.id)
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        artifacts = session.scalars(select(Artifact).where(Artifact.task_id == task.id)).all()
        assert persistent.status is TaskStatus.SUCCEEDED
        assert run.status is AgentRunStatus.SUCCEEDED
        assert run.external_run_id == f"mock:{run.id}"
        assert run.request_artifact_id is not None
        assert run.response_artifact_id is not None
        assert {item.logical_name for item in artifacts} >= {
            "agent-request.json",
            "agent-response.json",
            "implementation_summary",
        }
        assert Path(run.config_json["workdir"], "launch_count.txt").read_text().splitlines() == [
            "launch"
        ]


@pytest.mark.asyncio
async def test_mock_agent_semantic_blocked(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(
        factory,
        tmp_path,
        mock={"outcome": "blocked", "needs_escalation": True},
    )
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        persistent = session.get(Task, task.id)
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.SUCCEEDED
        assert persistent.status is TaskStatus.BLOCKED
        assert persistent.result_summary_json["needs_escalation"] is True


@pytest.mark.asyncio
async def test_mock_agent_invalid_result_preserves_raw_response(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path, mock={"invalid_result": True})
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.FAILED
        assert run.error_type == "INVALID_RESULT"
        assert session.get(Task, task.id).status is TaskStatus.FAILED
        response = session.get(Artifact, run.response_artifact_id)
        assert response.kind == "AGENT_RESPONSE"


@pytest.mark.asyncio
async def test_missing_required_deliverable_fails_task_not_agent_run(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(
        factory, tmp_path, mock={"omit_deliverables": ["implementation_summary"]}
    )
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        persistent = session.get(Task, task.id)
        assert run.status is AgentRunStatus.SUCCEEDED
        assert persistent.status is TaskStatus.FAILED
        assert "required deliverable missing" in persistent.error_summary


@pytest.mark.asyncio
async def test_transition_request_does_not_transition_project(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = create_agent_task(
        factory,
        tmp_path,
        permissions={"request_transition": True},
        mock={
            "transition_request": {
                "project_id": None,
                "from_stage": "TOY_GATE",
                "to_stage": "FULL_IMPLEMENT",
                "reason": "mock evidence passed",
            }
        },
    )
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        assert session.get(Project, project.id).stage is ProjectStage.TOY_RUN
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
        assert session.scalar(
            select(func.count(Event.id)).where(
                Event.project_id == project.id,
                Event.event_type == "AGENT_TRANSITION_REQUESTED",
            )
        ) == 1


@pytest.mark.asyncio
async def test_agent_path_escape_is_policy_violation(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path, mock={"path_escape": True})
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.SUCCEEDED
        assert session.get(Task, task.id).status is TaskStatus.FAILED
        assert session.scalar(
            select(func.count(Event.id)).where(
                Event.entity_id == run.id,
                Event.event_type == "AGENT_POLICY_VIOLATION",
            )
        ) == 1
        assert session.scalar(
            select(func.count(Artifact.id)).where(
                Artifact.task_id == task.id,
                Artifact.logical_name == "implementation_summary",
            )
        ) == 0


@pytest.mark.asyncio
async def test_requested_tasks_cannot_bypass_permission_or_create_tasks(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = create_agent_task(
        factory,
        tmp_path,
        mock={
            "requested_tasks": [
                {
                    "action": "spawn_more",
                    "role": "implementation_worker",
                    "objective": "more work",
                    "reason": "mock request",
                    "inputs": [],
                }
            ]
        },
    )
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=10, sleep_seconds=0.01
    )
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.SUCCEEDED
        assert session.get(Task, task.id).status is TaskStatus.FAILED
        assert session.scalar(
            select(func.count(Task.id)).where(Task.project_id == project.id)
        ) == 1
