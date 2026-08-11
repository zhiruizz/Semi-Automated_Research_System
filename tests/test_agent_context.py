from __future__ import annotations

import pytest
from sqlalchemy import func, select

from research_controller.agents.context import ContextBuilder
from research_controller.artifacts.store import ArtifactStore
from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Task
from research_controller.domain.enums import TaskStatus
from research_controller.protocols.agent import AgentTaskSpec
from tests.conftest import create_agent_task


def test_context_builder_loads_only_referenced_artifacts(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = create_agent_task(factory, tmp_path)
    source = tmp_path / "context.txt"
    source.write_text("selected context", encoding="utf-8")
    with factory.begin() as session:
        artifact = ArtifactStore(workspace).ingest_file(
            session,
            project_id=project.id,
            task_id=None,
            source=source,
            logical_name="selected-context",
            kind="SOURCE",
            producer_type="TEST",
            producer_ref_id="context-1",
        )
        spec_value = dict(session.get(Task, task.id).spec_json)
        spec_value["context"] = [
            {
                "artifact_id": artifact.id,
                "purpose": "input",
                "required": True,
                "mode": "FULL",
            }
        ]
        spec = AgentTaskSpec.model_validate(spec_value)
        pack = ContextBuilder().build(session, spec)
    assert len(pack.items) == 1
    assert pack.items[0].content == "selected context"


@pytest.mark.asyncio
async def test_missing_required_context_blocks_before_agent_start(runtime, tmp_path):
    _engine, factory, workspace = runtime
    _project, task = create_agent_task(factory, tmp_path)
    with factory.begin() as session:
        persistent = session.get(Task, task.id)
        spec = dict(persistent.spec_json)
        spec["context"] = [
            {
                "artifact_id": "art_missing",
                "purpose": "required",
                "required": True,
                "mode": "FULL",
            }
        ]
        persistent.spec_json = spec
    await ResearchController(factory, workspace, poll_interval_seconds=0).run_until_idle(
        timeout_seconds=5, sleep_seconds=0.01
    )
    with factory() as session:
        persistent = session.get(Task, task.id)
        assert persistent.status is TaskStatus.BLOCKED
        assert persistent.block_reason == "CONTEXT_MISSING"
        assert session.scalar(
            select(func.count(AgentRun.id)).where(AgentRun.task_id == task.id)
        ) == 0
