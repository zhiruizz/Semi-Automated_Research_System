from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest

from research_controller.db.models import Project, Task
from research_controller.db.session import (
    create_session_factory,
    create_sqlite_engine,
    initialize_database,
)
from research_controller.domain.enums import ProjectStage, TaskExecutor, TaskKind
from research_controller.domain.ids import new_id
from research_controller.protocols.compute import ComputeTaskSpec
from research_controller.services.project_state import ProjectStateService


@pytest.fixture
def runtime(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "controller.db")
    initialize_database(engine)
    factory = create_session_factory(engine)
    workspace = tmp_path / "workspace"
    yield engine, factory, workspace
    engine.dispose()


def create_compute_task(
    factory,
    tmp_path: Path,
    *,
    exit_code: int = 0,
    sleep_seconds: float = 0.02,
    idempotency_key: str | None = None,
    with_dependency: str | None = None,
) -> tuple[Project, Task]:
    marker_script = tmp_path / f"worker-{new_id('script')}.py"
    marker_script.write_text(
        "import json, time\n"
        "with open('launch_count.txt', 'a', encoding='utf-8') as h: h.write('launch\\n')\n"
        f"time.sleep({sleep_seconds!r})\n"
        "with open('metrics.json', 'w', encoding='utf-8') as h: json.dump({'loss': 1.0}, h)\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    service = ProjectStateService()
    with factory.begin() as session:
        project = service.create_project(
            session,
            slug=new_id("project"),
            title="test",
            workspace_uri=str(tmp_path / "workspace"),
        )
        task_id = new_id("tsk")
        spec = ComputeTaskSpec.model_validate(
            {
                "schema_version": "compute-task-spec/v0.1",
                "project_id": project.id,
                "task_id": task_id,
                "submission_key": f"{project.id}:{task_id}:a1",
                "execution": {"command": [sys.executable, str(marker_script)]},
                "resources": {"gpu_count": 0},
                "outputs": [
                    {
                        "logical_name": "metrics.json",
                        "glob": "metrics.json",
                        "required": True,
                        "artifact_kind": "METRICS",
                        "evidence_candidate": True,
                    },
                    {
                        "logical_name": "launch_count.txt",
                        "glob": "launch_count.txt",
                        "required": True,
                        "artifact_kind": "DEBUG_MARKER",
                    },
                ],
                "routing": {"allowed_providers": ["local"]},
                "success": {
                    "required_validators": ["exit_code_zero", "metrics_json", "no_nan"]
                },
            }
        )
        task = service.create_task(
            session,
            task_id=task_id,
            project_id=project.id,
            stage=ProjectStage.TOY_RUN,
            kind=TaskKind.COMPUTE,
            action="test_compute",
            executor=TaskExecutor.COMPUTE,
            idempotency_key=idempotency_key or new_id("idem"),
            spec=spec.model_dump(mode="json"),
            acceptance_policy={
                "required_artifacts": [
                    "metrics.json",
                    "launch_count.txt",
                    "run.out",
                    "run.error",
                    "exit.json",
                ],
                "validators": ["metrics_json", "no_nan"],
            },
            dependency_ids=[with_dependency] if with_dependency else [],
            max_attempts=2,
        )
        project_id = project.id
        task_id = task.id
    with factory() as session:
        return session.get(Project, project_id), session.get(Task, task_id)
