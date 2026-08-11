from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from research_controller.agents.hermes.client import HermesApiClient, HermesApiError
from research_controller.agents.hermes.models import HermesConfig


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object in {path}")
    return value


def claim(bridge_dir: Path) -> bool:
    try:
        descriptor = os.open(
            bridge_dir / "launch.claim",
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {"pid": os.getpid(), "claimed_at": datetime.now(timezone.utc).isoformat()},
            handle,
        )
        handle.flush()
        os.fsync(handle.fileno())
    return True


async def launch(bridge_dir: Path) -> None:
    if not claim(bridge_dir):
        return
    try:
        request = _load(bridge_dir / "request.json")
        config = HermesConfig.model_validate(request["config"])
        result = await HermesApiClient(config).start_run(dict(request["payload"]))
        _atomic_json(
            bridge_dir / "response.json",
            {
                "run_key": request["run_key"],
                "run_id": result.run_id,
                "status": result.status,
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except HermesApiError as exc:
        _atomic_json(
            bridge_dir / "error.json",
            {
                "error_type": exc.error_type,
                "message": str(exc),
                "uncertain": exc.uncertain,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        _atomic_json(
            bridge_dir / "error.json",
            {
                "error_type": "START_BRIDGE_FAILED",
                "message": f"{type(exc).__name__}: {str(exc)[:800]}",
                "uncertain": False,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(launch(args.bridge_dir.resolve()))


if __name__ == "__main__":
    main()
