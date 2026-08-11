from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from research_controller.agents.base import AgentAdapterError
from research_controller.agents.codex.schema import (
    CodexStructuredOutputAdapter,
    CodexStructuredResultError,
)
from research_controller.protocols.agent import AgentResult, DecisionResult


def parse_structured_result(
    raw: str,
    model: type[BaseModel],
    *,
    expected_task_id: str | None = None,
    structured_output: CodexStructuredOutputAdapter | None = None,
) -> BaseModel:
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentAdapterError("INVALID_CODEX_RESULT_JSON", str(exc)) from exc
    adapter = structured_output or CodexStructuredOutputAdapter()
    try:
        if model is AgentResult:
            return adapter.parse_agent_result(value, expected_task_id=expected_task_id)
        if model is DecisionResult:
            return adapter.parse_decision_result(value)
        raise TypeError(f"unsupported Codex structured result model: {model.__name__}")
    except CodexStructuredResultError as exc:
        raise AgentAdapterError(exc.error_type, str(exc)) from exc


def write_canonical_result(path: Path, result: BaseModel) -> None:
    from research_controller.agents.codex.util import atomic_json

    atomic_json(path, result.model_dump(mode="json"))
