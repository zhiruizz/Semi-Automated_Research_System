from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def run(workdir: Path, command: list[str]) -> int:
    workdir.mkdir(parents=True, exist_ok=True)
    atomic_json(
        workdir / "manifest.json",
        {"schema_version": "runner-manifest/v0.1", "command": command, "started_at": utc_iso()},
    )
    atomic_json(workdir / "runner_pid.json", {"pid": os.getpid(), "started_at": utc_iso()})
    exit_code = 127
    signal_number: int | None = None
    try:
        with (workdir / "run.out").open("ab", buffering=0) as stdout, (
            workdir / "run.error"
        ).open("ab", buffering=0) as stderr:
            completed = subprocess.run(command, cwd=workdir, stdout=stdout, stderr=stderr, check=False)
            exit_code = completed.returncode
            if exit_code < 0:
                signal_number = -exit_code
    except Exception as exc:
        with (workdir / "run.error").open("a", encoding="utf-8") as stderr:
            stderr.write(f"research-runner failed to start command: {type(exc).__name__}: {exc}\n")
        exit_code = 127
    atomic_json(
        workdir / "exit.json",
        {
            "schema_version": "runner-exit/v0.1",
            "exit_code": exit_code,
            "finished_at": utc_iso(),
            "signal": signal_number,
        },
    )
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-runner")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("research-runner requires a command after --")
    raise SystemExit(run(args.workdir.resolve(), command))


if __name__ == "__main__":
    main()
