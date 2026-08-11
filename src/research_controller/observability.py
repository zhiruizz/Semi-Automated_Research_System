from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any


CONTEXT_FIELDS = (
    "project_id",
    "task_id",
    "agent_run_id",
    "compute_job_id",
    "correlation_id",
    "event_type",
    "reason",
    "resource_class",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        value: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in CONTEXT_FIELDS:
            value[field] = getattr(record, field, None)
        return json.dumps(value, separators=(",", ":"), default=str)


def configure_structured_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
