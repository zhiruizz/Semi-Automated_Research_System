from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_controller.agents.base import AgentAdapterError
from research_controller.agents.hermes.prompt_builder import RESULT_END, RESULT_START
from research_controller.protocols.agent import AgentResult


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_envelope(raw_output: str) -> dict[str, Any]:
    start = raw_output.rfind(RESULT_START)
    end = raw_output.rfind(RESULT_END)
    if start < 0 or end < 0 or end < start:
        raise AgentAdapterError("INVALID_AGENT_RESULT_ENVELOPE", "AgentResult envelope is missing")
    if raw_output[end + len(RESULT_END) :].strip():
        raise AgentAdapterError(
            "INVALID_AGENT_RESULT_ENVELOPE", "text is present after AgentResult end marker"
        )
    if raw_output.count(RESULT_START) != 1 or raw_output.count(RESULT_END) != 1:
        raise AgentAdapterError(
            "INVALID_AGENT_RESULT_ENVELOPE", "expected exactly one AgentResult envelope"
        )
    body = raw_output[start + len(RESULT_START) : end].strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AgentAdapterError("INVALID_AGENT_RESULT_JSON", str(exc)) from exc
    if not isinstance(value, dict):
        raise AgentAdapterError("INVALID_AGENT_RESULT_JSON", "AgentResult must be a JSON object")
    return value


def parse_dual_result(raw_output: str, file_path: Path) -> AgentResult:
    envelope_value = parse_envelope(raw_output)
    try:
        file_value = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AgentAdapterError("AGENT_RESULT_FILE_MISSING", str(file_path)) from exc
    except json.JSONDecodeError as exc:
        raise AgentAdapterError("INVALID_AGENT_RESULT_JSON", str(exc)) from exc
    if not isinstance(file_value, dict):
        raise AgentAdapterError("INVALID_AGENT_RESULT_JSON", "result file must be a JSON object")
    if _canonical(envelope_value) != _canonical(file_value):
        raise AgentAdapterError(
            "AGENT_RESULT_MISMATCH",
            "final envelope and outputs/agent_result.json are not canonically identical",
        )
    return AgentResult.model_validate(envelope_value)
