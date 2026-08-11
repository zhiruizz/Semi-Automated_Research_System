from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from research_controller.agents.codex.client import probe_codex
from research_controller.agents.codex.models import CodexConfig, CodexHealth
from research_controller.agents.codex.result_parser import parse_structured_result
from research_controller.agents.codex.schema import native_output_schema
from research_controller.protocols.agent import AgentResult, DecisionResult


def fake_config(tmp_path: Path, monkeypatch, **behavior) -> CodexConfig:
    state = tmp_path / "fake-state"
    state.mkdir()
    (state / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")
    monkeypatch.setenv("SARS_FAKE_CODEX_STATE_DIR", str(state))
    return CodexConfig(
        app_server_command=[sys.executable, str(Path(__file__).with_name("fake_codex_app_server.py"))],
        request_timeout_sec=1,
    )


@pytest.mark.asyncio
async def test_codex_environment_probe_and_model_discovery(tmp_path, monkeypatch):
    status = await probe_codex(fake_config(tmp_path, monkeypatch), tmp_path)
    assert status.health is CodexHealth.HEALTHY
    assert status.auth_type == "chatgpt"
    assert status.default_model == "fake-supervisor"
    assert status.default_effort == "medium"
    assert status.models[0]["supported_efforts"] == ["low", "medium"]


@pytest.mark.asyncio
async def test_codex_auth_required(tmp_path, monkeypatch):
    status = await probe_codex(fake_config(tmp_path, monkeypatch, auth_required=True), tmp_path)
    assert status.health is CodexHealth.AUTH_REQUIRED
    assert status.auth_type is None


def test_codex_native_output_schema_is_strict_and_generic():
    agent_schema = native_output_schema(AgentResult)
    decision_schema = native_output_schema(DecisionResult)
    assert agent_schema["additionalProperties"] is False
    assert set(agent_schema["required"]) >= {"schema_version", "task_id", "outcome", "summary"}
    assert decision_schema["additionalProperties"] is False
    assert set(decision_schema["required"]) >= {"decision", "confidence", "rationale"}

    def assert_closed(value):
        if isinstance(value, dict):
            assert "format" not in value
            assert "default" not in value
            assert "title" not in value
            if value.get("type") == "object" or "properties" in value:
                assert value.get("additionalProperties") is False
            for item in value.values():
                assert_closed(item)
        elif isinstance(value, list):
            for item in value:
                assert_closed(item)

    assert_closed(agent_schema)
    assert_closed(decision_schema)


def test_codex_structured_result_has_no_repair_path():
    valid = {
        "schema_version": "agent-result/v0.1",
        "task_id": "tsk-1",
        "outcome": "completed",
        "summary": "fact-bound",
        "artifacts": [],
        "warnings": [],
        "requested_tasks": [],
        "transition_request": None,
        "protocol_amendment_request": None,
        "needs_escalation": False,
        "escalation": None,
    }
    result = parse_structured_result(json.dumps(valid), AgentResult)
    assert result.task_id == "tsk-1"
    with pytest.raises(Exception):
        parse_structured_result("```json\n" + json.dumps(valid) + "\n```", AgentResult)
