from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from research_controller.agents.base import AgentAdapter, AgentAdapterError
from research_controller.agents.codex.client import probe_codex
from research_controller.agents.codex.models import (
    CodexConfig,
    CodexHealth,
    CodexStatus,
    map_turn_status,
)
from research_controller.agents.codex.prompt_builder import (
    SYSTEM_INSTRUCTIONS,
    build_prompt,
    sandbox_policy,
    stage_context,
    validate_staged_context,
)
from research_controller.agents.codex.result_parser import (
    parse_structured_result,
    write_canonical_result,
)
from research_controller.agents.codex.schema import (
    CodexSchemaCompatibilityError,
    CodexStructuredOutputAdapter,
)
from research_controller.agents.codex.util import atomic_json, atomic_text, load_object
from research_controller.domain.enums import AgentRunStatus
from research_controller.protocols.agent import (
    AgentExecutionRequest,
    AgentObservation,
    AgentResult,
    AgentRunView,
    ContextPack,
    ExternalAgentRun,
    SessionPolicy,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _process_alive(bridge: Path) -> bool:
    for name in ("recovery.claim", "launch.claim"):
        claim = bridge / name
        if not claim.exists():
            continue
        try:
            pid = int(load_object(claim).get("pid", 0))
            if pid <= 0:
                continue
            os.kill(pid, 0)
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            if b"research_controller.agents.codex.start_bridge" in command and str(
                bridge
            ).encode() in command:
                return True
        except (OSError, ValueError):
            continue
    return False


class CodexAdapter(AgentAdapter):
    adapter_id = "codex"

    def __init__(
        self,
        workspace_root: Path | str,
        config: CodexConfig | None = None,
        *,
        structured_output: CodexStructuredOutputAdapter | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.config = config or CodexConfig.load()
        self.bridge_root = self.workspace_root / ".codex-bridge"
        self.bridge_root.mkdir(parents=True, exist_ok=True)
        self._status: CodexStatus | None = None
        self.structured_output = structured_output or CodexStructuredOutputAdapter()

    def bridge_dir(self, run_key: str) -> Path:
        return self.bridge_root / run_key

    async def status(self, *, refresh: bool = False) -> CodexStatus:
        if refresh or self._status is None:
            self._status = await probe_codex(self.config, self.workspace_root)
        return self._status

    async def model_options(self) -> list[dict[str, Any]]:
        return (await self.status(refresh=True)).models

    def _raise_error(self, bridge: Path) -> None:
        path = bridge / "error.json"
        if not path.exists():
            return
        error = load_object(path)
        error_type = str(error.get("error_type", "CODEX_BRIDGE_FAILED"))
        raise AgentAdapterError(
            error_type,
            str(error.get("message", error_type)),
            block_task=bool(error.get("uncertain"))
            or error_type
            in {
                "CODEX_START_STATE_UNCERTAIN",
                "CODEX_THREAD_NOT_FOUND",
                "CODEX_AUTH_REQUIRED",
                "CODEX_STAGED_CONTEXT_CHANGED",
            },
        )

    def _spawn_bridge(self, bridge: Path, *, recover: bool = False) -> None:
        command = [
            sys.executable,
            "-m",
            "research_controller.agents.codex.start_bridge",
            "--bridge-dir",
            str(bridge),
        ]
        if recover:
            command.append("--recover")
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        atomic_json(
            bridge / ("recovery-process.json" if recover else "process.json"),
            {"pid": process.pid, "spawned_at": _now().isoformat()},
        )

    def _maybe_recover_dead_bridge(self, bridge: Path) -> None:
        if _process_alive(bridge) or (bridge / "terminal.json").exists() or (bridge / "error.json").exists():
            return
        if not (bridge / "launch.claim").exists():
            return
        if (bridge / "thread.json").exists():
            if not (bridge / "recovery.claim").exists():
                self._spawn_bridge(bridge, recover=True)
            elif not _process_alive(bridge):
                raise AgentAdapterError(
                    "CODEX_START_STATE_UNCERTAIN",
                    "Codex recovery bridge stopped without a terminal record",
                    block_task=True,
                )
            return
        raise AgentAdapterError(
            "CODEX_START_STATE_UNCERTAIN",
            "Codex bridge stopped before durable Thread and Turn identities were recorded",
            block_task=True,
        )

    def _external(self, run_key: str, workdir: Path) -> ExternalAgentRun:
        bridge = self.bridge_dir(run_key)
        thread = load_object(bridge / "thread.json") if (bridge / "thread.json").exists() else {}
        turn = load_object(bridge / "turn.json") if (bridge / "turn.json").exists() else {}
        terminal = load_object(bridge / "terminal.json") if (bridge / "terminal.json").exists() else {}
        return ExternalAgentRun(
            run_key=run_key,
            external_run_id=str(turn.get("id") or f"bridge:{run_key}"),
            session_id=str(thread["id"]) if thread.get("id") else None,
            status=map_turn_status(str(terminal.get("status", turn.get("status", "inProgress")))).status,
            workdir=workdir,
            metadata={
                "bridge_dir": str(bridge),
                "session_tree_id": thread.get("session_tree_id"),
                "native_idempotency": False,
            },
        )

    async def _ensure_ready(self, request: AgentExecutionRequest) -> tuple[str | None, str | None]:
        status = await self.status(refresh=True)
        if status.health is CodexHealth.AUTH_REQUIRED:
            raise AgentAdapterError(
                "CODEX_AUTH_REQUIRED", "Codex App Server has no authenticated account", block_task=True
            )
        if status.health is not CodexHealth.HEALTHY:
            raise AgentAdapterError(
                status.error_type or "CODEX_UNAVAILABLE",
                status.message or "Codex App Server is unavailable",
                block_task=True,
            )
        tier = self.config.model_tiers.get(request.route.model_tier)
        model = tier.model if tier else None
        effort = tier.effort if tier else None
        if model is not None:
            option = next((item for item in status.models if item.get("model") == model or item.get("id") == model), None)
            if option is None:
                raise AgentAdapterError("CODEX_MODEL_UNAVAILABLE", f"configured model is unavailable: {model}", block_task=True)
            if effort is not None and effort not in option.get("supported_efforts", []):
                raise AgentAdapterError("CODEX_EFFORT_UNAVAILABLE", f"reasoning effort {effort} is unavailable for {model}", block_task=True)
        return model, effort

    async def start(self, request: AgentExecutionRequest) -> ExternalAgentRun:
        if not self.config.enabled:
            raise AgentAdapterError("CODEX_DISABLED", "Codex backend is disabled", block_task=True)
        try:
            contract = self.structured_output.for_agent_result()
        except CodexSchemaCompatibilityError as exc:
            raise AgentAdapterError(
                "CODEX_OUTPUT_SCHEMA_INVALID", str(exc), block_task=True
            ) from exc
        model, effort = await self._ensure_ready(request)
        workdir = request.workdir.resolve()
        manifest = stage_context(request)
        prompt = build_prompt(request, manifest)
        atomic_text(workdir / "prompt.txt", prompt)
        schema = contract.codex_schema
        atomic_json(workdir / "result_schema.json", schema)
        policy = sandbox_policy(request)
        sandbox_mode = "workspace-write" if policy["type"] == "workspaceWrite" else "read-only"

        thread_params: dict[str, Any] = {
            "cwd": str(workdir),
            "sandbox": sandbox_mode,
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "developerInstructions": SYSTEM_INSTRUCTIONS,
        }
        if request.session_id is None:
            thread_params["serviceName"] = "semi-automated-research-system"
        if request.route.session_policy is SessionPolicy.EPHEMERAL:
            thread_params["ephemeral"] = True
        if model:
            thread_params["model"] = model
        turn_params: dict[str, Any] = {
            "cwd": str(workdir),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandboxPolicy": policy,
            "input": [{"type": "text", "text": prompt}],
            "outputSchema": schema,
        }
        if model:
            turn_params["model"] = model
        if effort:
            turn_params["effort"] = effort

        bridge = self.bridge_dir(request.run_key)
        bridge.mkdir(parents=True, exist_ok=True)
        bridge_request = {
            "schema_version": "codex-start-bridge/v0.1",
            "run_key": request.run_key,
            "session_id": request.session_id,
            "workdir": str(workdir),
            "config": self.config.model_dump(mode="json"),
            "thread_params": thread_params,
            "turn_params": turn_params,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "domain_schema_hash": contract.domain_schema_hash,
            "wire_schema_hash": contract.wire_schema_hash,
            "codex_schema_hash": contract.codex_schema_hash,
            "schema_adapter_version": contract.schema_adapter_version,
            "sandbox_summary": {
                "mode": sandbox_mode,
                "network_access": policy["networkAccess"],
                "context_enforcement": "verified-disposable-copy",
            },
        }
        request_path = bridge / "request.json"
        if request_path.exists():
            if load_object(request_path) != bridge_request:
                raise AgentAdapterError("RUN_KEY_REUSED", "Codex run key was reused with a different request", block_task=True)
        else:
            atomic_json(request_path, bridge_request)
        self._raise_error(bridge)
        self._maybe_recover_dead_bridge(bridge)
        if not (bridge / "launch.claim").exists() and not (bridge / "terminal.json").exists():
            self._spawn_bridge(bridge)
        return self._external(request.run_key, workdir)

    async def reconcile(self, run_key: str) -> ExternalAgentRun | None:
        bridge = self.bridge_dir(run_key)
        if not (bridge / "request.json").exists():
            return None
        self._raise_error(bridge)
        self._maybe_recover_dead_bridge(bridge)
        if not (bridge / "launch.claim").exists():
            return None
        request = load_object(bridge / "request.json")
        return self._external(run_key, Path(str(request["workdir"])).resolve())

    async def poll(self, run: AgentRunView) -> AgentObservation:
        bridge = self.bridge_dir(run.id)
        self._raise_error(bridge)
        self._maybe_recover_dead_bridge(bridge)
        thread = load_object(bridge / "thread.json") if (bridge / "thread.json").exists() else {}
        turn = load_object(bridge / "turn.json") if (bridge / "turn.json").exists() else {}
        terminal = load_object(bridge / "terminal.json") if (bridge / "terminal.json").exists() else {}
        approval = load_object(bridge / "approval.json") if (bridge / "approval.json").exists() else None
        raw_status = str(terminal.get("status", turn.get("status", "inProgress")))
        mapped = map_turn_status(raw_status)
        status = AgentRunStatus.WAITING_APPROVAL if approval and not mapped.terminal else mapped.status
        usage_record = load_object(bridge / "usage.json") if (bridge / "usage.json").exists() else {}
        last = usage_record.get("last") if isinstance(usage_record.get("last"), dict) else {}
        usage = {
            "input_tokens": last.get("inputTokens"),
            "cached_tokens": last.get("cachedInputTokens"),
            "output_tokens": last.get("outputTokens"),
            "reasoning_output_tokens": last.get("reasoningOutputTokens"),
            "total_tokens": last.get("totalTokens"),
            "source": "thread_token_usage_last",
        }
        usage = {key: value for key, value in usage.items() if value is not None}
        terminal_error = str(terminal.get("error") or "")
        if mapped.status is AgentRunStatus.FAILED:
            if "invalid_json_schema" in terminal_error or "invalid schema" in terminal_error.lower():
                failure_type = "CODEX_INVALID_OUTPUT_SCHEMA"
            elif "rate limit" in terminal_error.lower() or '"status": 429' in terminal_error:
                failure_type = "CODEX_RATE_LIMITED"
            else:
                failure_type = "CODEX_TURN_FAILED"
        else:
            failure_type = mapped.error_type
        return AgentObservation(
            run_key=run.id,
            external_run_id=str(turn.get("id") or run.external_run_id or f"bridge:{run.id}"),
            session_id=str(thread["id"]) if thread.get("id") else run.session_id,
            observed_at=_now(),
            status=status,
            raw_state="approvalRequested" if approval else raw_status,
            result_available=mapped.status is AgentRunStatus.SUCCEEDED and bool(terminal.get("response_path")),
            error_type=failure_type,
            error_message=terminal.get("error"),
            metadata={
                "known_status": mapped.known,
                "response_path": terminal.get("response_path"),
                "usage": usage,
                "approval": approval or {},
                "session_tree_id": thread.get("session_tree_id"),
                "model": thread.get("model"),
                "reasoning_effort": thread.get("reasoning_effort"),
                "instruction_sources": thread.get("instruction_sources", []),
            },
        )

    async def get_result(self, run: AgentRunView) -> AgentResult:
        workdir = Path(run.config["workdir"]).resolve()
        validate_staged_context(
            workdir, ContextPack.model_validate(run.config["context_pack"])
        )
        raw = (workdir / "raw_response.txt").read_text(encoding="utf-8")
        result = parse_structured_result(
            raw,
            AgentResult,
            expected_task_id=run.task_id,
            structured_output=self.structured_output,
        )
        assert isinstance(result, AgentResult)
        write_canonical_result(workdir / "parsed_result.json", result)
        return result

    async def cancel(self, run: AgentRunView) -> None:
        atomic_json(
            self.bridge_dir(run.id) / "interrupt-request.json",
            {"requested_at": _now().isoformat()},
        )

    async def respond_approval(self, run: AgentRunView, choice: str) -> dict[str, Any]:
        if choice not in {"once", "deny"}:
            raise AgentAdapterError("INVALID_APPROVAL_CHOICE", "choice must be once or deny")
        bridge = self.bridge_dir(run.id)
        if not (bridge / "approval.json").exists():
            raise AgentAdapterError("APPROVAL_NOT_PENDING", "Codex approval is no longer pending")
        atomic_json(
            bridge / "approval-decision.json",
            {"choice": choice, "decided_at": _now().isoformat()},
        )
        return {"choice": choice, "native_decision": "accept" if choice == "once" else "decline"}
