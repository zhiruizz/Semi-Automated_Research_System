from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any

from research_controller.agents.base import AgentAdapter
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
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class MockAgentAdapter(AgentAdapter):
    adapter_id = "mock"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_key: str) -> Path:
        return self.runs_root / run_key

    def _external(self, run_key: str, session_id: str) -> ExternalAgentRun:
        run_dir = self.run_dir(run_key)
        started = _load(run_dir / "started.json") if (run_dir / "started.json").exists() else {}
        if (run_dir / "raw_response.json").exists():
            status = AgentRunStatus.SUCCEEDED
        elif (run_dir / "exit.json").exists():
            status = AgentRunStatus.FAILED
        elif (run_dir / "launch.claim").exists():
            status = AgentRunStatus.RUNNING
        else:
            status = AgentRunStatus.STARTING
        launched_at = None
        if started.get("started_at"):
            launched_at = datetime.fromisoformat(str(started["started_at"]))
        return ExternalAgentRun(
            run_key=run_key,
            external_run_id=f"mock:{run_key}",
            session_id=session_id,
            status=status,
            workdir=run_dir,
            launched_at=launched_at,
            metadata={"durable_run_key": run_key},
        )

    async def start(self, request: AgentExecutionRequest) -> ExternalAgentRun:
        run_dir = self.run_dir(request.run_key)
        run_dir.mkdir(parents=True, exist_ok=True)
        request_path = run_dir / "request.json"
        normalized = request.model_dump(mode="json")
        if request_path.exists():
            existing = _load(request_path)
            if existing != normalized:
                raise ValueError(f"run key {request.run_key} was reused with another request")
        else:
            _atomic_json(request_path, normalized)
        _atomic_json(
            run_dir / "reservation.json",
            {
                "run_key": request.run_key,
                "session_id": request.session_id,
                "reserved_at": _now().isoformat(),
            },
        )
        if not (run_dir / "launch.claim").exists():
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "research_controller.agents.mock_runner",
                    "--run-dir",
                    str(run_dir),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            _atomic_json(
                run_dir / "process.json",
                {"pid": process.pid, "spawned_at": _now().isoformat()},
            )
        return self._external(request.run_key, request.session_id)

    async def reconcile(self, run_key: str) -> ExternalAgentRun | None:
        run_dir = self.run_dir(run_key)
        request_path = run_dir / "request.json"
        if not request_path.exists():
            return None
        request = _load(request_path)
        if not any(
            (run_dir / name).exists()
            for name in ("launch.claim", "started.json", "raw_response.json", "exit.json")
        ):
            return None
        return self._external(run_key, str(request["session_id"]))

    async def poll(self, run: AgentRunView) -> AgentObservation:
        run_dir = self.run_dir(run.id)
        external_id = run.external_run_id or f"mock:{run.id}"
        response = run_dir / "raw_response.json"
        exit_path = run_dir / "exit.json"
        process_path = run_dir / "process.json"
        if response.exists():
            status = AgentRunStatus.SUCCEEDED
            raw_state = "RESULT_WRITTEN"
        elif exit_path.exists():
            record = _load(exit_path)
            status = AgentRunStatus.SUCCEEDED if int(record.get("exit_code", 1)) == 0 else AgentRunStatus.FAILED
            raw_state = "PROCESS_EXITED"
        elif process_path.exists() and _pid_alive(int(_load(process_path).get("pid", 0))):
            status = AgentRunStatus.RUNNING
            raw_state = "PROCESS_ALIVE"
        elif (run_dir / "launch.claim").exists():
            status = AgentRunStatus.FAILED
            raw_state = "PROCESS_MISSING_WITHOUT_RESULT"
        else:
            status = AgentRunStatus.STARTING
            raw_state = "RESERVED"
        return AgentObservation(
            run_key=run.id,
            external_run_id=external_id,
            observed_at=_now(),
            status=status,
            raw_state=raw_state,
            result_available=response.exists(),
            error_type="MOCK_EXTERNAL_FAILURE" if status is AgentRunStatus.FAILED else None,
            metadata={"response_path": str(response)},
        )

    async def get_result(self, run: AgentRunView) -> AgentResult:
        return AgentResult.model_validate(_load(self.run_dir(run.id) / "raw_response.json"))

    async def cancel(self, run: AgentRunView) -> None:
        process_path = self.run_dir(run.id) / "process.json"
        if not process_path.exists():
            return
        pid = int(_load(process_path).get("pid", 0))
        if pid <= 0:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
