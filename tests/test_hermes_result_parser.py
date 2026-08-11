from __future__ import annotations

import json

import pytest

from research_controller.agents.base import AgentAdapterError
from research_controller.agents.hermes.result_parser import parse_dual_result


def result(task_id: str = "tsk-1") -> dict:
    return {
        "schema_version": "agent-result/v0.1",
        "task_id": task_id,
        "outcome": "completed",
        "summary": "done",
        "artifacts": [],
    }


def envelope(value: dict) -> str:
    return "<<<SARS_AGENT_RESULT_V1>>>\n" + json.dumps(value) + "\n<<<END_SARS_AGENT_RESULT_V1>>>"


def test_dual_agent_result_is_strict_and_canonical(tmp_path):
    value = result()
    path = tmp_path / "agent_result.json"
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    assert parse_dual_result(envelope(value), path).task_id == "tsk-1"


def test_dual_agent_result_mismatch_is_not_repaired(tmp_path):
    path = tmp_path / "agent_result.json"
    path.write_text(json.dumps(result("other")), encoding="utf-8")
    with pytest.raises(AgentAdapterError) as mismatch:
        parse_dual_result(envelope(result()), path)
    assert mismatch.value.error_type == "AGENT_RESULT_MISMATCH"


def test_markdown_or_trailing_text_is_rejected(tmp_path):
    path = tmp_path / "agent_result.json"
    path.write_text(json.dumps(result()), encoding="utf-8")
    with pytest.raises(AgentAdapterError):
        parse_dual_result("```json\n" + json.dumps(result()) + "\n```", path)
    with pytest.raises(AgentAdapterError):
        parse_dual_result(envelope(result()) + " trailing", path)
