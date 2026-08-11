from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from sqlalchemy import select

from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Artifact, ComputeJob, Event, Project, Task
from research_controller.db.session import (
    create_session_factory,
    create_sqlite_engine,
    initialize_database,
)
from research_controller.domain.enums import ProjectStage, TaskExecutor, TaskKind
from research_controller.domain.ids import new_id
from research_controller.protocols.compute import ComputeTaskSpec
from research_controller.protocols.agent import AgentTaskSpec
from research_controller.observability import configure_structured_logging
from research_controller.services.project_state import ProjectStateService


def runtime(database: Path, workspace: Path):
    engine = create_sqlite_engine(database)
    initialize_database(engine)
    factory = create_session_factory(engine)
    controller = ResearchController(factory, workspace)
    return engine, factory, controller


def create_demo(factory, workspace: Path) -> tuple[str, str]:
    service = ProjectStateService()
    with factory.begin() as session:
        project = session.scalar(select(Project).where(Project.slug == "demo-count"))
        if project is None:
            project = service.create_project(
                session,
                slug="demo-count",
                title="Local count vertical slice",
                workspace_uri=str(workspace.resolve()),
            )
        existing = session.scalar(
            select(Task).where(
                Task.project_id == project.id,
                Task.idempotency_key == "demo-count:v1",
            )
        )
        if existing is not None:
            return project.id, existing.id
        task_id = new_id("tsk")
        example = Path(__file__).resolve().parents[2] / "examples" / "count.py"
        spec = ComputeTaskSpec.model_validate(
            {
                "schema_version": "compute-task-spec/v0.1",
                "project_id": project.id,
                "task_id": task_id,
                "submission_key": f"{project.id}:{task_id}:count:v1:a1",
                "execution": {"command": [sys.executable, str(example)]},
                "resources": {"gpu_count": 0, "cpu_cores": 1, "memory_gb": 1},
                "outputs": [
                    {
                        "logical_name": "metrics.json",
                        "glob": "metrics.json",
                        "required": True,
                        "artifact_kind": "METRICS",
                        "evidence_candidate": True,
                    }
                ],
                "routing": {"allowed_providers": ["local"]},
                "success": {
                    "require_zero_exit_code": True,
                    "required_validators": ["exit_code_zero", "metrics_json", "no_nan"],
                },
            }
        )
        task = service.create_task(
            session,
            task_id=task_id,
            project_id=project.id,
            stage=ProjectStage.TOY_RUN,
            kind=TaskKind.COMPUTE,
            action="run_count_demo",
            executor=TaskExecutor.COMPUTE,
            idempotency_key="demo-count:v1",
            spec=spec.model_dump(mode="json"),
            acceptance_policy={
                "required_artifacts": ["metrics.json", "run.out", "run.error", "exit.json"],
                "validators": ["metrics_json", "no_nan"],
            },
        )
        return project.id, task.id


def create_agent_demo(factory, workspace: Path) -> tuple[str, str]:
    service = ProjectStateService()
    with factory.begin() as session:
        project = session.scalar(select(Project).where(Project.slug == "demo-agent"))
        if project is None:
            project = service.create_project(
                session,
                slug="demo-agent",
                title="Typed MockAgent vertical slice",
                workspace_uri=str(workspace.resolve()),
            )
        existing = session.scalar(
            select(Task).where(
                Task.project_id == project.id,
                Task.idempotency_key == "demo-agent:v1",
            )
        )
        if existing is not None:
            return project.id, existing.id
        task_id = new_id("tsk")
        spec = AgentTaskSpec.model_validate(
            {
                "schema_version": "agent-task/v0.1",
                "project_id": project.id,
                "task_id": task_id,
                "role": "implementation_worker",
                "objective": "produce a typed implementation summary",
                "instructions": ["return only the declared deliverable"],
                "deliverables": [
                    {
                        "logical_name": "implementation_summary",
                        "artifact_kind": "RESULT_SUMMARY",
                        "required": True,
                    }
                ],
                "execution_policy": {"session_policy": "new", "timeout_sec": 30},
            }
        )
        task = service.create_task(
            session,
            task_id=task_id,
            project_id=project.id,
            stage=ProjectStage.TOY_IMPLEMENT,
            kind=TaskKind.AGENT,
            action="run_mock_agent_demo",
            executor=TaskExecutor.HERMES,
            idempotency_key="demo-agent:v1",
            spec=spec.model_dump(mode="json"),
            routing_policy={"mock": {"delay_sec": 0.1}},
        )
        return project.id, task.id


def print_status(factory) -> None:
    with factory() as session:
        projects = session.scalars(select(Project)).all()
        tasks = session.scalars(select(Task)).all()
        jobs = session.scalars(select(ComputeJob)).all()
        agent_runs = session.scalars(select(AgentRun)).all()
        artifacts = session.scalars(select(Artifact)).all()
        events = session.scalars(select(Event).order_by(Event.project_id, Event.seq)).all()
        value = {
            "projects": [
                {"id": item.id, "slug": item.slug, "lifecycle": item.lifecycle.value}
                for item in projects
            ],
            "tasks": [
                {"id": item.id, "action": item.action, "status": item.status.value}
                for item in tasks
            ],
            "compute_jobs": [
                {
                    "id": item.id,
                    "provider": item.provider_id,
                    "status": item.execution_status.value,
                    "exit_code": item.exit_code,
                }
                for item in jobs
            ],
            "agent_runs": [
                {
                    "id": item.id,
                    "backend": item.backend,
                    "role": item.role,
                    "status": item.status.value,
                    "session_id": item.session_id,
                }
                for item in agent_runs
            ],
            "artifacts": [
                {
                    "id": item.id,
                    "logical_name": item.logical_name,
                    "sha256": item.sha256,
                    "evidence_eligible": item.evidence_eligible,
                }
                for item in artifacts
            ],
            "events": [
                {"seq": item.seq, "type": item.event_type, "entity_id": item.entity_id}
                for item in events
            ],
        }
    print(json.dumps(value, indent=2))


async def run_forever(controller: ResearchController, interval: float) -> None:
    while True:
        await controller.tick()
        await asyncio.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-controller")
    parser.add_argument("--database", type=Path, default=Path("data/controller.db"))
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db")
    demo = subparsers.add_parser("demo")
    demo.add_argument("--run", action="store_true")
    agent_demo = subparsers.add_parser("agent-demo")
    agent_demo.add_argument("--run", action="store_true")
    run = subparsers.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval", type=float, default=1.0)
    run.add_argument("--until-idle", action="store_true")
    run.add_argument("--timeout", type=float, default=30.0)
    subparsers.add_parser("status")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    configure_structured_logging()
    engine, factory, controller = runtime(args.database, args.workspace)
    try:
        if args.command == "init-db":
            print(f"initialized {args.database.resolve()}")
        elif args.command == "demo":
            project_id, task_id = create_demo(factory, args.workspace)
            print(f"created project={project_id} task={task_id}")
            if args.run:
                asyncio.run(controller.run_until_idle())
                print_status(factory)
        elif args.command == "agent-demo":
            project_id, task_id = create_agent_demo(factory, args.workspace)
            print(f"created project={project_id} task={task_id}")
            if args.run:
                asyncio.run(controller.run_until_idle())
                print_status(factory)
        elif args.command == "run":
            if args.until_idle:
                asyncio.run(controller.run_until_idle(timeout_seconds=args.timeout))
            elif args.once:
                result = asyncio.run(controller.tick())
                print(json.dumps(result.__dict__, indent=2))
            else:
                asyncio.run(run_forever(controller, args.interval))
        elif args.command == "status":
            print_status(factory)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
