from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
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
        raise ValueError("mock request must be an object")
    return value


def _claim_once(run_dir: Path) -> bool:
    try:
        descriptor = os.open(run_dir / "launch.claim", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
    return True


def run(run_dir: Path) -> int:
    run_dir.mkdir(parents=True, exist_ok=True)
    if not _claim_once(run_dir):
        return 0
    request = _load(run_dir / "request.json")
    config = request.get("adapter_config", {})
    with (run_dir / "launch_count.txt").open("a", encoding="utf-8") as handle:
        handle.write("launch\n")
        handle.flush()
        os.fsync(handle.fileno())
    _atomic_json(
        run_dir / "started.json",
        {"pid": os.getpid(), "started_at": _now(), "run_key": request["run_key"]},
    )
    time.sleep(float(config.get("delay_sec", 0.02)))
    if config.get("external_crash"):
        _atomic_json(
            run_dir / "exit.json",
            {"exit_code": 70, "finished_at": _now(), "error": "mock external crash"},
        )
        return 70

    task_spec = request["task_spec"]
    output_dir = run_dir / "output"
    output_dir.mkdir(exist_ok=True)
    omitted = set(config.get("omit_deliverables", []))
    artifacts: list[dict[str, Any]] = []
    for deliverable in task_spec.get("deliverables", []):
        logical_name = str(deliverable["logical_name"])
        if logical_name in omitted:
            continue
        safe_name = logical_name.replace("/", "_")
        path = output_dir / f"{safe_name}.txt"
        path.write_text(
            f"mock output for {logical_name}\n",
            encoding="utf-8",
        )
        artifacts.append(
            {
                "logical_name": logical_name,
                "path": str(path),
                "artifact_kind": deliverable["artifact_kind"],
                "evidence_candidate": bool(deliverable.get("evidence_candidate", False)),
                "metadata": {"mock": True},
            }
        )
    if config.get("path_escape"):
        artifacts = [
            {
                "logical_name": task_spec.get("deliverables", [{}])[0].get(
                    "logical_name", "escaped"
                ),
                "path": "/etc/passwd",
                "artifact_kind": task_spec.get("deliverables", [{}])[0].get(
                    "artifact_kind", "RESULT_SUMMARY"
                ),
                "evidence_candidate": False,
                "metadata": {},
            }
        ]
    result: dict[str, Any] = {
        "schema_version": "agent-result/v0.1",
        "task_id": task_spec["task_id"],
        "outcome": config.get("outcome", "completed"),
        "summary": config.get("summary", "mock agent completed"),
        "artifacts": artifacts,
        "warnings": config.get("warnings", []),
        "requested_tasks": config.get("requested_tasks", []),
        "transition_request": config.get("transition_request"),
        "protocol_amendment_request": config.get("protocol_amendment_request"),
        "needs_escalation": bool(config.get("needs_escalation", False)),
        "escalation": config.get("escalation"),
    }
    if "raw_result" in config:
        result = config["raw_result"]
    if config.get("wrong_task_id"):
        result["task_id"] = "wrong-task"
    if config.get("invalid_result"):
        result["unexpected_field"] = True
    _atomic_json(run_dir / "raw_response.json", result)
    _atomic_json(
        run_dir / "exit.json",
        {"exit_code": 0, "finished_at": _now()},
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="mock-agent-runner")
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.run_dir.resolve()))


if __name__ == "__main__":
    main()
