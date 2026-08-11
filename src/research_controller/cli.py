from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from sqlalchemy import select

from research_controller.controller import ResearchController
from research_controller.agents.hermes.client import HermesApiError
from research_controller.agents.hermes.adapter import HermesAdapter
from research_controller.agents.codex.adapter import CodexAdapter
from research_controller.agents.codex.schema import CodexStructuredOutputAdapter
from research_controller.artifacts.store import ArtifactStore
from research_controller.db.models import AgentRun, Artifact, ComputeJob, Event, Project, Task
from research_controller.db.session import (
    create_session_factory,
    create_sqlite_engine,
    initialize_database,
)
from research_controller.domain.enums import AgentRunStatus, ProjectStage, TaskExecutor, TaskKind
from research_controller.domain.ids import new_id
from research_controller.protocols.compute import ComputeTaskSpec
from research_controller.protocols.agent import AgentTaskSpec
from research_controller.observability import configure_structured_logging
from research_controller.services.project_state import ProjectStateService
from research_controller.services.agent_reconciler import agent_run_view
from research_controller.services.transitions import TransitionService


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


def create_hermes_demo(factory, workspace: Path) -> tuple[str, str]:
    service = ProjectStateService()
    with factory.begin() as session:
        project = session.scalar(select(Project).where(Project.slug == "hermes-api-demo"))
        if project is None:
            project = service.create_project(
                session,
                slug="hermes-api-demo",
                title="Tiny real Hermes Runs API demo",
                workspace_uri=str(workspace.resolve()),
            )
        existing = session.scalar(
            select(Task).where(
                Task.project_id == project.id,
                Task.idempotency_key == "hermes-api-demo:v1",
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
                "objective": "Write one short implementation summary confirming the typed result contract.",
                "instructions": ["Keep the response and deliverable under 100 words."],
                "deliverables": [
                    {
                        "logical_name": "implementation_summary",
                        "artifact_kind": "RESULT_SUMMARY",
                        "required": True,
                    }
                ],
                "permissions": {"filesystem_write": True},
                "execution_policy": {"session_policy": "new", "timeout_sec": 120},
            }
        )
        task = service.create_task(
            session,
            task_id=task_id,
            project_id=project.id,
            stage=ProjectStage.TOY_IMPLEMENT,
            kind=TaskKind.AGENT,
            action="run_tiny_real_hermes_demo",
            executor=TaskExecutor.HERMES,
            idempotency_key="hermes-api-demo:v1",
            spec=spec.model_dump(mode="json"),
            routing_policy={"hermes": {}},
        )
        return project.id, task.id


async def print_hermes_status(controller: ResearchController) -> None:
    adapter = controller.agent_registry.get("hermes")
    assert isinstance(adapter, HermesAdapter)
    try:
        health = await adapter.client.probe()
        capabilities = await adapter.client.capabilities()
        value = {
            "health": health.value,
            "base_url": adapter.config.base_url,
            "api_key_env": adapter.config.api_key_env,
            "capabilities": capabilities.model_dump(exclude={"raw"}),
        }
    except HermesApiError as exc:
        value = {
            "health": (
                "auth_required"
                if exc.error_type == "HERMES_AUTH_REQUIRED"
                else "unavailable"
            ),
            "base_url": adapter.config.base_url,
            "api_key_env": adapter.config.api_key_env,
            "error_type": exc.error_type,
            "message": str(exc),
        }
    print(json.dumps(value, indent=2))


async def print_hermes_models(controller: ResearchController) -> None:
    adapter = controller.agent_registry.get("hermes")
    assert isinstance(adapter, HermesAdapter)
    try:
        value = await adapter.client.model_options()
    except HermesApiError as exc:
        value = {"error_type": exc.error_type, "message": str(exc)}
    print(json.dumps(value, indent=2))


async def print_codex_status(controller: ResearchController) -> None:
    adapter = controller.agent_registry.get("codex")
    assert isinstance(adapter, CodexAdapter)
    status = await adapter.status(refresh=True)
    value = {
        "enabled": adapter.config.enabled,
        "runtime": "app-server-stdio",
        "cli_version": status.cli_version,
        "sdk_version": None,
        "health": status.health.value,
        "auth_type": status.auth_type,
        "auth_available": status.health.value == "healthy",
        "default_model": status.default_model,
        "default_effort": status.default_effort,
        "available_model_count": len(status.models),
        "error_type": status.error_type,
        "message": status.message,
    }
    print(json.dumps(value, indent=2))


async def print_codex_models(controller: ResearchController) -> None:
    adapter = controller.agent_registry.get("codex")
    assert isinstance(adapter, CodexAdapter)
    status = await adapter.status(refresh=True)
    print(
        json.dumps(
            {"health": status.health.value, "models": status.models},
            indent=2,
        )
    )


def print_codex_schema(model_name: str, *, as_json: bool) -> None:
    adapter = CodexStructuredOutputAdapter()
    contract = (
        adapter.for_agent_result()
        if model_name == "agent-result"
        else adapter.for_decision_result()
    )
    value = contract.introspection(include_schema=True)
    if as_json:
        print(json.dumps(value, indent=2))
        return
    report = value["compatibility_report"]
    print(f"domain model: {value['domain_model']}")
    print(f"wire model: {value['wire_model']}")
    print(f"schema adapter: {value['schema_adapter_version']}")
    print(f"domain schema SHA256: {value['domain_schema_hash']}")
    print(f"wire schema SHA256: {value['wire_schema_hash']}")
    print(f"Codex schema SHA256: {value['codex_schema_hash']}")
    print(f"compatibility validator: {'PASS' if report['compatible'] else 'FAIL'}")
    print(f"top-level properties: {', '.join(report['top_level_properties'])}")
    print(f"$defs: {report['definition_count']}; closed objects: {report['object_count']}")
    print(json.dumps(value["codex_schema"], indent=2))


def create_codex_demo(factory, workspace: Path) -> tuple[str, str]:
    service = ProjectStateService()
    artifacts = ArtifactStore(workspace)
    with factory.begin() as session:
        project = session.scalar(
            select(Project).where(Project.slug == "codex-schema-facts-demo")
        )
        if project is None:
            project = service.create_project(
                session,
                slug="codex-schema-facts-demo",
                title="Tiny read-only Codex structured-output demo",
                workspace_uri=str(workspace.resolve()),
            )
        existing = session.scalar(
            select(Task).where(
                Task.project_id == project.id,
                Task.idempotency_key == "codex-schema-facts-demo:v3",
            )
        )
        if existing is not None:
            return project.id, existing.id
        fixture = Path(__file__).resolve().parents[2] / "examples" / "codex_facts.json"
        fixture_value = json.loads(fixture.read_text(encoding="utf-8"))
        context = artifacts.ingest_file(
            session,
            project_id=project.id,
            task_id=None,
            source=fixture,
            logical_name="codex-facts.json",
            kind="FACTS_FIXTURE",
            producer_type="SYSTEM",
            producer_ref_id="codex-schema-demo-v3",
        )
        task_id = new_id("tsk")
        spec = AgentTaskSpec.model_validate(
            {
                "schema_version": "agent-task/v0.1",
                "project_id": project.id,
                "task_id": task_id,
                "role": "scientific_supervisor",
                "objective": "State which supplied method meets the supplied threshold, using only the tiny facts fixture.",
                "instructions": [
                    "Do not invent measurements, uncertainty estimates, or broader scientific claims.",
                    "The authoritative fixture values are also supplied inline because local-command access is disabled: "
                    + json.dumps(fixture_value, sort_keys=True, separators=(",", ":")),
                    "Keep the summary under 80 words.",
                ],
                "context": [
                    {
                        "artifact_id": context.id,
                        "purpose": "authoritative tiny facts fixture",
                        "mode": "FULL",
                    }
                ],
                "deliverables": [],
                "permissions": {
                    "filesystem_write": False,
                    "local_command": False,
                    "network": False,
                    "compute_submit": False,
                    "request_tasks": False,
                    "request_transition": False,
                    "request_protocol_amendment": False,
                },
                "execution_policy": {"session_policy": "new", "timeout_sec": 180},
            }
        )
        task = service.create_task(
            session,
            task_id=task_id,
            project_id=project.id,
            stage=ProjectStage.RESULT_VALIDATION,
            kind=TaskKind.AGENT,
            action="run_tiny_real_codex_facts_demo",
            executor=TaskExecutor.CODEX,
            idempotency_key="codex-schema-facts-demo:v3",
            spec=spec.model_dump(mode="json"),
            routing_policy={"codex": {}},
        )
        return project.id, task.id


def print_agent_approvals(factory) -> None:
    with factory() as session:
        runs = session.scalars(
            select(AgentRun).where(AgentRun.status == AgentRunStatus.WAITING_APPROVAL)
        ).all()
        value = [
            {
                "agent_run_id": run.id,
                "external_run_id": run.external_run_id,
                "project_id": run.project_id,
                "role": run.role,
            }
            for run in runs
        ]
    print(json.dumps(value, indent=2))


async def respond_agent_approval(factory, controller: ResearchController, run_id: str, choice: str) -> None:
    with factory() as session:
        run = session.get(AgentRun, run_id)
        if run is None or run.status.value != "WAITING_APPROVAL":
            raise ValueError(f"AgentRun {run_id} is not waiting for approval")
        view = agent_run_view(run)
        project_id = run.project_id
    adapter = controller.agent_registry.get(view.backend)
    await adapter.respond_approval(view, choice)
    with factory.begin() as session:
        run = session.get(AgentRun, run_id)
        TransitionService().events.append(
            session,
            project_id=project_id,
            event_type="HUMAN_AGENT_APPROVAL",
            entity_type="AGENT_RUN",
            entity_id=run_id,
            actor_type="HUMAN",
            actor_id="controller-cli",
            payload={"choice": choice},
        )


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
    hermes_demo = subparsers.add_parser("hermes-demo")
    hermes_demo.add_argument("--real", action="store_true", required=True)
    hermes_demo.add_argument("--run", action="store_true")
    hermes = subparsers.add_parser("hermes")
    hermes_sub = hermes.add_subparsers(dest="hermes_command", required=True)
    hermes_sub.add_parser("status")
    hermes_sub.add_parser("models")
    codex_demo = subparsers.add_parser("codex-demo")
    codex_demo.add_argument("--real", action="store_true", required=True)
    codex_demo.add_argument("--run", action="store_true")
    codex = subparsers.add_parser("codex")
    codex_sub = codex.add_subparsers(dest="codex_command", required=True)
    codex_sub.add_parser("status")
    codex_sub.add_parser("models")
    codex_schema = codex_sub.add_parser("schema")
    codex_schema.add_argument("model_name", choices=["agent-result", "decision-result"])
    codex_schema.add_argument("--json", action="store_true")
    agent = subparsers.add_parser("agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("approvals")
    approve = agent_sub.add_parser("approve")
    approve.add_argument("agent_run_id")
    deny = agent_sub.add_parser("deny")
    deny.add_argument("agent_run_id")
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
        elif args.command == "hermes-demo":
            project_id, task_id = create_hermes_demo(factory, args.workspace)
            print(f"created real Hermes project={project_id} task={task_id}")
            if args.run:
                asyncio.run(controller.run_until_idle(timeout_seconds=180))
                print_status(factory)
        elif args.command == "hermes":
            if args.hermes_command == "status":
                asyncio.run(print_hermes_status(controller))
            elif args.hermes_command == "models":
                asyncio.run(print_hermes_models(controller))
        elif args.command == "codex-demo":
            project_id, task_id = create_codex_demo(factory, args.workspace)
            print(f"created real Codex project={project_id} task={task_id}")
            if args.run:
                asyncio.run(controller.run_until_idle(timeout_seconds=240))
                print_status(factory)
        elif args.command == "codex":
            if args.codex_command == "status":
                asyncio.run(print_codex_status(controller))
            elif args.codex_command == "models":
                asyncio.run(print_codex_models(controller))
            elif args.codex_command == "schema":
                print_codex_schema(args.model_name, as_json=args.json)
        elif args.command == "agent":
            if args.agent_command == "approvals":
                print_agent_approvals(factory)
            elif args.agent_command == "approve":
                asyncio.run(
                    respond_agent_approval(factory, controller, args.agent_run_id, "once")
                )
            elif args.agent_command == "deny":
                asyncio.run(
                    respond_agent_approval(factory, controller, args.agent_run_id, "deny")
                )
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
