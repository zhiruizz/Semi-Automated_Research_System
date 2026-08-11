from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import func, select

from research_controller.agents.codex.adapter import CodexAdapter
from research_controller.agents.codex.models import CodexConfig, CodexModelTier
from research_controller.agents.registry import AgentAdapterRegistry
from research_controller.artifacts.store import ArtifactStore
from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Artifact, Event, Project, Task
from research_controller.domain.enums import AgentRunStatus, ProjectStage, TaskStatus
from research_controller.services.agent_reconciler import agent_run_view
from tests.test_codex_vertical_slice import codex_controller, make_codex_task


async def wait_task(factory, task_id: str, expected: TaskStatus, controller, ticks: int = 200):
    for _ in range(ticks):
        await controller.tick()
        with factory() as session:
            if session.get(Task, task_id).status is expected:
                return
        await asyncio.sleep(0.02)
    pytest.fail(f"task did not reach {expected}")


@pytest.mark.asyncio
async def test_codex_configured_model_missing_blocks(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "missing-model"
    controller = codex_controller(factory, workspace, state)
    adapter = controller.agent_registry.get("codex")
    adapter.config.model_tiers["supervisor"] = CodexModelTier(model="not-present")
    await wait_task(factory, task.id, TaskStatus.BLOCKED, controller)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.error_type == "CODEX_MODEL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_codex_ephemeral_thread_is_not_resumed(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, first = make_codex_task(factory, tmp_path, session_policy="ephemeral")
    state = tmp_path / "ephemeral"
    controller = codex_controller(factory, workspace, state)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    _project, second = make_codex_task(
        factory, tmp_path, project_id=project.id, session_policy="resume_role"
    )
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        runs = {run.task_id: run for run in session.scalars(select(AgentRun))}
        assert runs[first.id].session_id != runs[second.id].session_id
    params = json.loads((state / "thread_start_params.json").read_text())
    counts = json.loads((state / "counts.json").read_text())
    assert counts["thread_start"] == 2
    assert counts.get("thread_resume", 0) == 0
    assert params[0]["ephemeral"] is True
    assert params[1].get("ephemeral") is not True


@pytest.mark.asyncio
async def test_codex_tier_isolation_starts_new_thread(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, first = make_codex_task(factory, tmp_path, session_policy="resume_role")
    state = tmp_path / "tier"
    controller = codex_controller(factory, workspace, state)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    _project, second = make_codex_task(
        factory, tmp_path, project_id=project.id, session_policy="resume_role"
    )
    with factory.begin() as session:
        session.get(Task, second.id).routing_policy_json = {
            "codex": {},
            "model_tier": "alternate",
        }
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        runs = {run.task_id: run for run in session.scalars(select(AgentRun))}
        assert runs[first.id].session_id != runs[second.id].session_id


@pytest.mark.asyncio
async def test_codex_thread_not_found_blocks_without_new_thread(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, first = make_codex_task(factory, tmp_path, session_policy="resume_role")
    state = tmp_path / "missing-thread"
    controller = codex_controller(factory, workspace, state)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        first_run = session.scalar(select(AgentRun).where(AgentRun.task_id == first.id))
        thread_id = first_run.session_id
    (state / "threads" / f"{thread_id}.json").unlink()
    _project, second = make_codex_task(
        factory, tmp_path, project_id=project.id, session_policy="resume_role"
    )
    await wait_task(factory, second.id, TaskStatus.BLOCKED, controller)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == second.id))
        assert run.error_type == "CODEX_THREAD_NOT_FOUND"
    counts = json.loads((state / "counts.json").read_text())
    assert counts["thread_start"] == 1


@pytest.mark.asyncio
async def test_codex_turn_failure_is_classified(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    controller = codex_controller(factory, workspace, tmp_path / "failed-turn", turn_failed=True)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.FAILED
        assert run.error_type == "CODEX_TURN_FAILED"


@pytest.mark.asyncio
async def test_codex_invalid_result_preserves_raw_artifact(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    controller = codex_controller(factory, workspace, tmp_path / "invalid", invalid_result=True)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.error_type == "INVALID_CODEX_WIRE_RESULT"
        assert session.scalar(
            select(Artifact.id).where(
                Artifact.task_id == task.id, Artifact.kind == "RAW_AGENT_RESPONSE"
            )
        )


@pytest.mark.asyncio
async def test_codex_summary_only_completion_hydrates_same_turn(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "summary-only-completion"
    controller = codex_controller(
        factory, workspace, state, summary_only_completion=True
    )
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.SUCCEEDED
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
    counts = json.loads((state / "counts.json").read_text())
    assert counts["turn_start"] == 1


@pytest.mark.asyncio
async def test_codex_cancel_interrupts_once_and_maps_cancelled(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "cancel"
    controller = codex_controller(factory, workspace, state, delay_sec=30)
    for _ in range(100):
        await controller.tick()
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            if run and run.external_run_id and not run.external_run_id.startswith("bridge:"):
                view = agent_run_view(run)
                break
        await asyncio.sleep(0.02)
    await controller.agent_registry.get("codex").cancel(view)
    await wait_task(factory, task.id, TaskStatus.CANCELLED, controller)
    counts = json.loads((state / "counts.json").read_text())
    assert counts["turn_interrupt"] == 1


@pytest.mark.asyncio
async def test_codex_timeout_interrupts_once_and_maps_timeout(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path, timeout_sec=1)
    state = tmp_path / "timeout"
    controller = codex_controller(factory, workspace, state, delay_sec=30)
    await controller.run_until_idle(timeout_seconds=5, sleep_seconds=0.02)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.status is AgentRunStatus.TIMEOUT
        assert session.get(Task, task.id).status is TaskStatus.FAILED
    assert json.loads((state / "counts.json").read_text())["turn_interrupt"] == 1


@pytest.mark.asyncio
async def test_codex_uncertain_turn_response_reconciles_marker_without_duplicate(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "uncertain-turn"
    controller = codex_controller(factory, workspace, state, drop_turn_response=True)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
    counts = json.loads((state / "counts.json").read_text())
    assert counts["turn_start"] == 1


@pytest.mark.asyncio
async def test_codex_unknown_new_thread_gap_blocks_without_retry(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "uncertain-thread"
    controller = codex_controller(factory, workspace, state, drop_thread_response=True)
    await wait_task(factory, task.id, TaskStatus.BLOCKED, controller)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.error_type == "CODEX_START_STATE_UNCERTAIN"
    assert json.loads((state / "counts.json").read_text())["thread_start"] == 1


@pytest.mark.asyncio
async def test_codex_approval_survives_controller_restart(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "approval-restart"
    first = codex_controller(factory, workspace, state, approval=True)
    for _ in range(100):
        await first.tick()
        with factory() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
            if run and run.status is AgentRunStatus.WAITING_APPROVAL:
                break
        await asyncio.sleep(0.02)
    restarted = codex_controller(factory, workspace, state, approval=True)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        view = agent_run_view(run)
    await restarted.agent_registry.get("codex").respond_approval(view, "deny")
    await restarted.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    assert json.loads((state / "approval_response.json").read_text())["decision"] == "decline"


@pytest.mark.asyncio
async def test_codex_terminal_before_collection_is_idempotently_recovered(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "collect-recovery"
    first = codex_controller(factory, workspace, state, delay_sec=0.1)
    await first.tick()  # readiness + durable AgentRun dispatch
    await first.tick()  # bridge start
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        bridge = workspace / ".codex-bridge" / run.id
    for _ in range(100):
        if (bridge / "terminal.json").exists():
            break
        await asyncio.sleep(0.02)
    restarted = codex_controller(factory, workspace, state, delay_sec=0.1)
    await restarted.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    await restarted.tick()
    with factory() as session:
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
        assert session.scalar(
            select(func.count(Artifact.id)).where(
                Artifact.task_id == task.id, Artifact.kind == "AGENT_RESPONSE"
            )
        ) == 1


@pytest.mark.asyncio
async def test_codex_transition_request_never_mutates_project_directly(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = make_codex_task(factory, tmp_path)
    transition = {
        "schema_version": "transition-request/v0.1",
        "project_id": project.id,
        "from_stage": ProjectStage.LITERATURE_REVIEW.value,
        "to_stage": ProjectStage.IDEA_GENERATION.value,
        "reason": "request only",
        "evidence_artifact_ids": [],
        "asserted_preconditions": [],
    }
    controller = codex_controller(
        factory, workspace, tmp_path / "transition", transition_request=transition
    )
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        assert session.get(Project, project.id).stage is not ProjectStage.IDEA_GENERATION
        assert session.scalar(
            select(func.count(Event.id)).where(
                Event.entity_id == task.id,
                Event.event_type == "PROJECT_STAGE_CHANGED",
            )
        ) == 0


@pytest.mark.asyncio
async def test_codex_controller_sigkill_bridge_survives_without_duplicate_turn(runtime, tmp_path):
    _engine, factory, workspace = runtime
    database = tmp_path / "controller.db"
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "sigkill-state"
    state.mkdir()
    (state / "behavior.json").write_text(json.dumps({"delay_sec": 1.0}), encoding="utf-8")
    script = Path(__file__).with_name("fake_codex_app_server.py")
    environment = os.environ.copy()
    environment.update(
        {
            "SARS_CODEX_TEST_MODE": "1",
            "SARS_CODEX_APP_SERVER_COMMAND": f"{sys.executable} {script}",
            "SARS_FAKE_CODEX_STATE_DIR": str(state),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "research_controller.cli",
            "--database",
            str(database),
            "--workspace",
            str(workspace),
            "run",
            "--interval",
            "0.01",
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            counts = load_counts = json.loads((state / "counts.json").read_text()) if (state / "counts.json").exists() else {}
            if load_counts.get("turn_start") == 1:
                break
            await asyncio.sleep(0.02)
        assert counts.get("turn_start") == 1
        process.kill()
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        bridge = workspace / ".codex-bridge" / run.id
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not (bridge / "terminal.json").exists():
        await asyncio.sleep(0.02)
    assert (bridge / "terminal.json").exists()
    restarted = codex_controller(factory, workspace, state, delay_sec=1.0)
    await restarted.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert session.get(Task, task.id).status is TaskStatus.SUCCEEDED
        assert run.session_id.startswith("thr-")
        assert run.external_run_id.startswith("turn-")
    assert json.loads((state / "counts.json").read_text())["turn_start"] == 1


@pytest.mark.asyncio
async def test_codex_context_is_verified_copy_and_source_remains_immutable(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = make_codex_task(factory, tmp_path)
    source = tmp_path / "facts.txt"
    source.write_text("alpha=1\nbeta=2\n", encoding="utf-8")
    store = ArtifactStore(workspace)
    with factory.begin() as session:
        artifact = store.ingest_file(
            session,
            project_id=project.id,
            task_id=None,
            source=source,
            logical_name="facts.txt",
            kind="FACTS",
            producer_type="SYSTEM",
            producer_ref_id="context-test",
        )
        persistent = session.get(Task, task.id)
        spec = dict(persistent.spec_json)
        spec["context"] = [
            {
                "artifact_id": artifact.id,
                "purpose": "authoritative facts",
                "required": True,
                "mode": "FULL",
            }
        ]
        persistent.spec_json = spec
        source_uri = Path(artifact.uri)
        source_hash = artifact.sha256
    controller = codex_controller(factory, workspace, tmp_path / "context-copy")
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        run_root = Path(run.config_json["workdir"])
    staged = next((run_root / "inputs").iterdir())
    assert staged.read_text() == source_uri.read_text()
    assert staged.stat().st_ino != source_uri.stat().st_ino
    assert staged.stat().st_mode & 0o222 == 0
    assert __import__("hashlib").sha256(source_uri.read_bytes()).hexdigest() == source_hash


@pytest.mark.asyncio
async def test_codex_cannot_hide_staged_context_tampering_by_editing_manifest(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = make_codex_task(factory, tmp_path)
    source = tmp_path / "tamper-facts.txt"
    source.write_text("trusted\n", encoding="utf-8")
    store = ArtifactStore(workspace)
    with factory.begin() as session:
        artifact = store.ingest_file(
            session,
            project_id=project.id,
            task_id=None,
            source=source,
            logical_name="tamper-facts.txt",
            kind="FACTS",
            producer_type="SYSTEM",
            producer_ref_id="tamper-test",
        )
        persistent = session.get(Task, task.id)
        spec = dict(persistent.spec_json)
        spec["context"] = [{"artifact_id": artifact.id, "purpose": "trusted", "mode": "FULL"}]
        persistent.spec_json = spec
        source_uri = Path(artifact.uri)
    controller = codex_controller(
        factory, workspace, tmp_path / "tamper-context", tamper_context=True
    )
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.error_type == "CODEX_STAGED_CONTEXT_CHANGED"
        assert source_uri.read_text(encoding="utf-8") == "trusted\n"


@pytest.mark.asyncio
async def test_codex_read_only_and_explicit_network_sandbox(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(
        factory,
        tmp_path,
        permissions={"filesystem_write": False, "network": True},
        required_deliverable=False,
    )
    state = tmp_path / "read-only-network"
    controller = codex_controller(factory, workspace, state)
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    params = json.loads((state / "last_turn_params.json").read_text())
    assert params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": True}
    assert json.loads((state / "last_thread_start_params.json").read_text())["sandbox"] == "read-only"


@pytest.mark.asyncio
async def test_codex_full_access_is_forbidden(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    with factory.begin() as session:
        session.get(Task, task.id).routing_policy_json = {
            "codex": {"sandbox": "danger-full-access"}
        }
    controller = codex_controller(factory, workspace, tmp_path / "full-access")
    await wait_task(factory, task.id, TaskStatus.BLOCKED, controller)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert run.error_type == "CODEX_FULL_ACCESS_FORBIDDEN"


@pytest.mark.asyncio
async def test_codex_request_artifact_has_prompt_schema_and_sandbox_metadata(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    controller = codex_controller(factory, workspace, tmp_path / "request-artifact")
    await controller.run_until_idle(timeout_seconds=8, sleep_seconds=0.02)
    with factory() as session:
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.task_id == task.id, Artifact.kind == "AGENT_REQUEST"
            )
        )
        assert len(artifact.metadata_json["prompt_sha256"]) == 64
        assert len(artifact.metadata_json["domain_schema_hash"]) == 64
        assert len(artifact.metadata_json["wire_schema_hash"]) == 64
        assert len(artifact.metadata_json["codex_schema_hash"]) == 64
        assert artifact.metadata_json["schema_adapter_version"] == "codex-structured-schema/v0.1"
        assert artifact.metadata_json["sandbox"]["network_access"] is False
        record = json.loads(Path(artifact.uri).read_text())
        assert record["adapter_contract"]["codex_schema_hash"] == artifact.metadata_json["codex_schema_hash"]


def test_codex_fork_role_is_explicitly_deferred():
    from research_controller.agents.router import AgentRouter, BackendNotImplementedError
    from research_controller.domain.enums import TaskExecutor
    from research_controller.protocols.agent import AgentTaskSpec

    spec = AgentTaskSpec.model_validate(
        {
            "schema_version": "agent-task/v0.1",
            "project_id": "prj",
            "task_id": "tsk",
            "role": "scientific_supervisor",
            "objective": "fork test",
            "execution_policy": {"session_policy": "fork_role"},
        }
    )
    with pytest.raises(BackendNotImplementedError, match="CODEX_FORK_ROLE_DEFERRED"):
        AgentRouter().route(spec, TaskExecutor.CODEX, {})
