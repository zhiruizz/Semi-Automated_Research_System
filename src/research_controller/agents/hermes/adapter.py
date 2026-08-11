from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from research_controller.agents.base import AgentAdapter, AgentAdapterError
from research_controller.agents.hermes.client import HermesApiClient, HermesApiError, redact_text
from research_controller.agents.hermes.models import HermesConfig, map_run_status
from research_controller.agents.hermes.prompt_builder import (
    SYSTEM_INSTRUCTIONS,
    build_context_manifest,
    build_prompt,
)
from research_controller.agents.hermes.result_parser import parse_dual_result
from research_controller.domain.enums import AgentRunStatus
from research_controller.protocols.agent import (
    AgentExecutionRequest,
    AgentObservation,
    AgentResult,
    AgentRunView,
    ExternalAgentRun,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _bridge_process_alive(bridge: Path) -> bool:
    claim_path = bridge / "launch.claim"
    if not claim_path.exists():
        return False
    try:
        pid = int(_load(claim_path).get("pid", 0))
        if pid <= 0:
            return False
        os.kill(pid, 0)
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        return b"research_controller.agents.hermes.start_bridge" in command and str(
            bridge
        ).encode() in command
    except (OSError, ValueError):
        return False


class HermesAdapter(AgentAdapter):
    adapter_id = "hermes"

    def __init__(self, workspace_root: Path | str, config: HermesConfig | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.config = config or HermesConfig.load()
        self.bridge_root = self.workspace_root / ".hermes-bridge"
        self.bridge_root.mkdir(parents=True, exist_ok=True)
        self.client = HermesApiClient(self.config)

    def bridge_dir(self, run_key: str) -> Path:
        return self.bridge_root / run_key

    def _bridge_external(self, run_key: str, workdir: Path) -> ExternalAgentRun:
        bridge = self.bridge_dir(run_key)
        response_path = bridge / "response.json"
        if response_path.exists():
            response = _load(response_path)
            external_id = str(response["run_id"])
            status = AgentRunStatus.RUNNING
        else:
            external_id = f"bridge:{run_key}"
            status = AgentRunStatus.STARTING
        request = _load(bridge / "request.json")
        return ExternalAgentRun(
            run_key=run_key,
            external_run_id=external_id,
            session_id=request.get("session_id"),
            status=status,
            workdir=workdir,
            metadata={"bridge_dir": str(bridge), "native_idempotency": False},
        )

    def _raise_bridge_error(self, bridge: Path) -> None:
        error_path = bridge / "error.json"
        if not error_path.exists():
            return
        record = _load(error_path)
        error_type = str(record.get("error_type", "START_BRIDGE_FAILED"))
        raise AgentAdapterError(
            error_type,
            str(record.get("message", error_type)),
            block_task=bool(record.get("uncertain"))
            or error_type in {"START_STATE_UNCERTAIN", "HERMES_AUTH_REQUIRED"},
        )

    def _raise_if_abandoned_claim(self, bridge: Path) -> None:
        if (
            (bridge / "launch.claim").exists()
            and not (bridge / "response.json").exists()
            and not (bridge / "error.json").exists()
            and not _bridge_process_alive(bridge)
        ):
            raise AgentAdapterError(
                "START_STATE_UNCERTAIN",
                "Hermes StartBridge claim exists but its process and durable response are missing",
                block_task=True,
            )

    async def start(self, request: AgentExecutionRequest) -> ExternalAgentRun:
        if not self.config.enabled:
            raise AgentAdapterError("HERMES_DISABLED", "Hermes backend is disabled", block_task=True)
        try:
            capabilities = await self.client.capabilities()
        except HermesApiError as exc:
            raise AgentAdapterError(exc.error_type, str(exc), block_task=True) from exc
        if not capabilities.run_submission or not capabilities.run_status:
            raise AgentAdapterError(
                "HERMES_RUNS_API_UNAVAILABLE",
                "Hermes live capabilities do not support run submission and polling",
                block_task=True,
            )

        workdir = request.workdir.resolve()
        outputs = workdir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        manifest_path = workdir / "context_manifest.json"
        _atomic_json(manifest_path, build_context_manifest(request))
        prompt = build_prompt(request, manifest_path)
        _atomic_text(workdir / "prompt.txt", prompt)

        payload: dict[str, Any] = {
            "input": prompt,
            "instructions": SYSTEM_INSTRUCTIONS,
        }
        if request.session_id:
            payload["session_id"] = request.session_id
        tier = self.config.model_tiers.get(request.route.model_tier)
        if tier is not None:
            if tier.model:
                payload["model"] = tier.model
            if tier.provider:
                payload["provider"] = tier.provider

        bridge = self.bridge_dir(request.run_key)
        bridge.mkdir(parents=True, exist_ok=True)
        bridge_request = {
            "schema_version": "hermes-start-bridge/v0.1",
            "run_key": request.run_key,
            "session_id": request.session_id,
            "workdir": str(workdir),
            "config": self.config.redacted(),
            "payload": payload,
        }
        request_path = bridge / "request.json"
        if request_path.exists():
            if _load(request_path) != bridge_request:
                raise AgentAdapterError(
                    "RUN_KEY_REUSED", "Hermes run key was reused with a different request", block_task=True
                )
        else:
            _atomic_json(request_path, bridge_request)
        self._raise_bridge_error(bridge)
        self._raise_if_abandoned_claim(bridge)
        if not (bridge / "launch.claim").exists() and not (bridge / "response.json").exists():
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "research_controller.agents.hermes.start_bridge",
                    "--bridge-dir",
                    str(bridge),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            _atomic_json(bridge / "process.json", {"pid": process.pid, "spawned_at": _now().isoformat()})
        return self._bridge_external(request.run_key, workdir)

    async def reconcile(self, run_key: str) -> ExternalAgentRun | None:
        bridge = self.bridge_dir(run_key)
        if not (bridge / "request.json").exists():
            return None
        self._raise_bridge_error(bridge)
        self._raise_if_abandoned_claim(bridge)
        request = _load(bridge / "request.json")
        workdir = Path(str(request["workdir"])).resolve()
        if not (bridge / "launch.claim").exists() and not (bridge / "response.json").exists():
            return None
        return self._bridge_external(run_key, workdir)

    def _remote_id(self, run: AgentRunView) -> str | None:
        if run.external_run_id and not run.external_run_id.startswith("bridge:"):
            return run.external_run_id
        response = self.bridge_dir(run.id) / "response.json"
        if response.exists():
            return str(_load(response)["run_id"])
        return None

    async def poll(self, run: AgentRunView) -> AgentObservation:
        bridge = self.bridge_dir(run.id)
        self._raise_bridge_error(bridge)
        self._raise_if_abandoned_claim(bridge)
        remote_id = self._remote_id(run)
        if remote_id is None:
            return AgentObservation(
                run_key=run.id,
                external_run_id=run.external_run_id,
                observed_at=_now(),
                status=AgentRunStatus.RUNNING,
                raw_state="START_BRIDGE_ACTIVE",
            )
        if (bridge / "stop_requested.json").exists() and not (bridge / "stop.sent").exists():
            try:
                await self.client.stop_run(remote_id)
            except HermesApiError as exc:
                raise AgentAdapterError(exc.error_type, str(exc), block_task=exc.error_type == "REMOTE_RUN_NOT_FOUND") from exc
            _atomic_text(bridge / "stop.sent", _now().isoformat())
        try:
            remote = await self.client.get_run(remote_id)
        except HermesApiError as exc:
            raise AgentAdapterError(
                exc.error_type,
                str(exc),
                block_task=exc.error_type == "REMOTE_RUN_NOT_FOUND",
            ) from exc
        mapped = map_run_status(remote.status)
        metadata: dict[str, Any] = {"known_status": mapped.known}
        if remote.usage:
            metadata["usage"] = remote.usage
        if mapped.status is AgentRunStatus.SUCCEEDED:
            raw_path = Path(run.config["workdir"]).resolve() / "raw_response.txt"
            remote_path = Path(run.config["workdir"]).resolve() / "remote_response.json"
            _atomic_text(raw_path, remote.output or "")
            safe_remote = remote.model_dump(mode="json")
            if safe_remote.get("error"):
                safe_remote["error"] = redact_text(
                    safe_remote["error"], os.environ.get(self.config.api_key_env)
                )
            _atomic_json(remote_path, safe_remote)
            metadata.update(
                {"response_path": str(raw_path), "remote_response_path": str(remote_path)}
            )
        approval = {}
        if mapped.status is AgentRunStatus.WAITING_APPROVAL:
            approval = {
                "request_id": remote.run_id,
                "last_event": remote.last_event,
            }
            metadata["approval"] = approval
        return AgentObservation(
            run_key=run.id,
            external_run_id=remote_id,
            session_id=remote.session_id,
            observed_at=_now(),
            status=mapped.status,
            raw_state=remote.status,
            result_available=mapped.status is AgentRunStatus.SUCCEEDED,
            error_type=mapped.error_type or ("HERMES_RUN_FAILED" if mapped.status is AgentRunStatus.FAILED else None),
            error_message=redact_text(
                remote.error, os.environ.get(self.config.api_key_env)
            )
            if remote.error
            else None,
            metadata=metadata,
        )

    async def get_result(self, run: AgentRunView) -> AgentResult:
        workdir = Path(run.config["workdir"]).resolve()
        raw = (workdir / "raw_response.txt").read_text(encoding="utf-8")
        result = parse_dual_result(raw, workdir / "outputs" / "agent_result.json")
        _atomic_json(workdir / "parsed_result.json", result.model_dump(mode="json"))
        return result

    async def cancel(self, run: AgentRunView) -> None:
        bridge = self.bridge_dir(run.id)
        _atomic_json(bridge / "stop_requested.json", {"requested_at": _now().isoformat()})
        remote_id = self._remote_id(run)
        if remote_id is None or (bridge / "stop.sent").exists():
            return
        try:
            await self.client.stop_run(remote_id)
        except HermesApiError as exc:
            raise AgentAdapterError(exc.error_type, str(exc), block_task=exc.error_type == "REMOTE_RUN_NOT_FOUND") from exc
        _atomic_text(bridge / "stop.sent", _now().isoformat())

    async def respond_approval(self, run: AgentRunView, choice: str) -> dict[str, Any]:
        remote_id = self._remote_id(run)
        if remote_id is None:
            raise AgentAdapterError("REMOTE_RUN_ID_MISSING", "Hermes run id is not yet known")
        try:
            return await self.client.respond_approval(remote_id, choice)
        except HermesApiError as exc:
            raise AgentAdapterError(exc.error_type, str(exc)) from exc
