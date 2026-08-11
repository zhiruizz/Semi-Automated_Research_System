from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import select

from research_controller.agents.codex.adapter import CodexAdapter
from research_controller.agents.codex.models import CodexConfig, CodexModelTier
from research_controller.agents.registry import AgentAdapterRegistry
from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Artifact, Task
from research_controller.domain.enums import AgentRunStatus, TaskExecutor, TaskStatus
from research_controller.services.agent_reconciler import agent_run_view

from tests.conftest import create_agent_task


def codex_controller(factory, workspace: Path, fake_state: Path, **behavior):
    fake_state.mkdir(parents=True, exist_ok=True)
    (fake_state / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")
    script = Path(__file__).with_name("fake_codex_app_server.py")
    import os

    os.environ["SARS_FAKE_CODEX_STATE_DIR"] = str(fake_state)
    config = CodexConfig(
        app_server_command=[sys.executable, str(script)],
        poll_interval_sec=0.02,
        request_timeout_sec=1,
        approval_poll_interval_sec=0.02,
        model_tiers={"supervisor": CodexModelTier()},
    )
    registry = AgentAdapterRegistry([CodexAdapter(workspace, config)])
    return ResearchController(
        factory,
        workspace,
        agent_registry=registry,
        poll_interval_seconds=0,
    )


def make_codex_task(factory, tmp_path: Path, **kwargs):
    kwargs.setdefault("permissions", {"filesystem_write": True})
    project, task = create_agent_task(
        factory,
        tmp_path,
        role=kwargs.pop("role", "scientific_supervisor"),
        executor=kwargs.pop("executor", TaskExecutor.CODEX),
        **kwargs,
    )
    with factory.begin() as session:
        session.get(Task, task.id).routing_policy_json = {"codex": {}}
    return project, task


@pytest.mark.asyncio
async def test_codex_fake_vertical_slice_uses_native_schema_sandbox_and_artifacts(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "fake-codex"
    controller = codex_controller(factory, workspace, state)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)

    with factory() as session:
        persistent = session.get(Task, task.id)
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        artifacts = session.scalars(select(Artifact).where(Artifact.task_id == task.id)).all()
        assert persistent.status is TaskStatus.SUCCEEDED
        assert run.status is AgentRunStatus.SUCCEEDED
        assert run.session_id.startswith("thr-")
        assert run.external_run_id.startswith("turn-")
        assert (run.input_tokens, run.cached_tokens, run.output_tokens) == (17, 3, 11)
        assert {item.kind for item in artifacts} >= {"AGENT_REQUEST", "RAW_AGENT_RESPONSE", "AGENT_RESPONSE", "RESULT_SUMMARY"}

    params = json.loads((state / "last_turn_params.json").read_text(encoding="utf-8"))
    assert params["cwd"].endswith(task.id) is False
    assert params["sandboxPolicy"]["type"] == "workspaceWrite"
    assert params["sandboxPolicy"]["networkAccess"] is False
    assert params["approvalPolicy"] == "on-request"
    assert params["outputSchema"]["additionalProperties"] is False
    assert "task_id" in params["outputSchema"]["required"]


@pytest.mark.asyncio
async def test_codex_thread_continuity_and_reviewer_isolation(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, first = make_codex_task(factory, tmp_path, session_policy="resume_role")
    state = tmp_path / "fake-codex-session"
    controller = codex_controller(factory, workspace, state)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    _project, second = make_codex_task(
        factory,
        tmp_path,
        project_id=project.id,
        session_policy="resume_role",
    )
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    _project, reviewer = make_codex_task(
        factory,
        tmp_path,
        project_id=project.id,
        role="result_reviewer",
        session_policy="new",
    )
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        runs = {
            run.task_id: run
            for run in session.scalars(select(AgentRun).where(AgentRun.project_id == project.id))
        }
        assert runs[first.id].session_id == runs[second.id].session_id
        assert runs[reviewer.id].session_id != runs[first.id].session_id
        assert runs[first.id].external_run_id != runs[second.id].external_run_id
    counts = json.loads((state / "counts.json").read_text(encoding="utf-8"))
    assert counts["thread_start"] == 2
    assert counts["thread_resume"] == 1


@pytest.mark.asyncio
async def test_codex_generic_approval_maps_once_to_accept(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "fake-codex-approval"
    controller = codex_controller(factory, workspace, state, approval=True)
    for _ in range(100):
        await controller.tick()
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            if run and run.status is AgentRunStatus.WAITING_APPROVAL:
                view = agent_run_view(run)
                break
        await __import__("asyncio").sleep(0.02)
    else:
        raise AssertionError("approval was not observed")
    await controller.agent_registry.get("codex").respond_approval(view, "once")
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    assert json.loads((state / "approval_response.json").read_text())["decision"] == "accept"
