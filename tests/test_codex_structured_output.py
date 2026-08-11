from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import select

from research_controller.agents.codex.adapter import CodexAdapter
from research_controller.agents.codex.models import CodexConfig, CodexModelTier
from research_controller.agents.codex.schema import (
    SCHEMA_ADAPTER_VERSION,
    CodexSchemaCompatibilityError,
    CodexStructuredOutputAdapter,
    CodexStructuredResultError,
    validate_codex_output_schema,
    walk_schema,
)
from research_controller.agents.registry import AgentAdapterRegistry
from research_controller.controller import ResearchController
from research_controller.db.models import AgentRun, Task
from research_controller.domain.enums import TaskStatus
from research_controller.protocols.agent import AgentResult, AgentTaskSpec, DecisionResult
from research_controller.services.agent_validation import AgentResultValidator
from tests.test_codex_vertical_slice import make_codex_task


def valid_agent_wire(**overrides):
    value = {
        "schema_version": "agent-result/v0.1",
        "task_id": "tsk-wire",
        "outcome": "completed",
        "summary": "Method A satisfies the supplied threshold; Method B does not.",
        "artifacts": [
            {
                "logical_name": "summary",
                "path": "/tmp/run/outputs/summary.md",
                "artifact_kind": "RESULT_SUMMARY",
                "evidence_candidate": False,
                "metadata_json": "{\"source\":\"fixture\"}",
            }
        ],
        "warnings": [],
        "requested_tasks": [],
        "transition_request": None,
        "protocol_amendment_request": None,
        "needs_escalation": False,
        "escalation": None,
    }
    value.update(overrides)
    return value


def test_codex_wire_schema_agent_result():
    contract = CodexStructuredOutputAdapter().for_agent_result()
    assert contract.domain_model == "AgentResult"
    assert contract.wire_model == "CodexAgentResultWire"
    assert contract.compatibility_report.compatible is True
    assert contract.schema_adapter_version == SCHEMA_ADAPTER_VERSION


def test_codex_wire_schema_decision_result():
    adapter = CodexStructuredOutputAdapter()
    contract = adapter.for_decision_result()
    assert contract.domain_model == "DecisionResult"
    assert contract.wire_model == "CodexDecisionResultWire"
    assert contract.compatibility_report.compatible is True
    result = adapter.parse_decision_result(
        {
            "schema_version": "decision-result/v0.1",
            "decision": "accept",
            "confidence": 0.8,
            "rationale": "fixture evidence",
            "evidence_used": ["facts"],
            "missing_information": [],
            "requested_tasks": [],
            "transition_request": None,
        }
    )
    assert isinstance(result, DecisionResult)


def test_codex_schema_has_no_path_or_other_format_keywords():
    contract = CodexStructuredOutputAdapter().for_agent_result()
    assert all("format" not in node.value for node in walk_schema(contract.codex_schema))
    artifact = contract.codex_schema["$defs"]["CodexProducedArtifactWire"]
    assert artifact["properties"]["path"] == {"type": "string"}


def test_codex_schema_objects_are_closed_and_have_deterministic_required_fields():
    for contract in (
        CodexStructuredOutputAdapter().for_agent_result(),
        CodexStructuredOutputAdapter().for_decision_result(),
    ):
        for node in walk_schema(contract.codex_schema):
            value = node.value
            if value.get("type") == "object" or "properties" in value:
                assert value["additionalProperties"] is False, node.pointer
                assert set(value["required"]) == set(value["properties"]), node.pointer


def test_codex_schema_has_no_open_mapping_or_pydantic_annotations():
    prohibited = {
        "format",
        "title",
        "examples",
        "default",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
    schema = CodexStructuredOutputAdapter().for_agent_result().codex_schema
    for node in walk_schema(schema):
        assert not (prohibited & node.value.keys()), node.pointer
        assert node.value.get("additionalProperties") in {None, False}, node.pointer


def test_codex_agent_wire_to_domain_round_trip():
    result = CodexStructuredOutputAdapter().parse_agent_result(
        valid_agent_wire(), expected_task_id="tsk-wire"
    )
    assert isinstance(result, AgentResult)
    assert isinstance(result.artifacts[0].path, Path)
    assert result.artifacts[0].metadata == {"source": "fixture"}


@pytest.mark.parametrize("metadata_json", ["not-json", "[]", "42", '"text"'])
def test_codex_wire_invalid_metadata_json(metadata_json):
    value = valid_agent_wire()
    value["artifacts"][0]["metadata_json"] = metadata_json
    with pytest.raises(CodexStructuredResultError) as error:
        CodexStructuredOutputAdapter().parse_agent_result(value)
    assert error.value.error_type == "INVALID_CODEX_WIRE_METADATA"


def test_codex_wire_invalid_outcome_and_transition_are_rejected():
    with pytest.raises(CodexStructuredResultError) as outcome:
        CodexStructuredOutputAdapter().parse_agent_result(
            valid_agent_wire(outcome="unknown")
        )
    assert outcome.value.error_type == "INVALID_CODEX_WIRE_RESULT"
    with pytest.raises(CodexStructuredResultError) as transition:
        CodexStructuredOutputAdapter().parse_agent_result(
            valid_agent_wire(transition_request={"reason": "missing fields"})
        )
    assert transition.value.error_type == "INVALID_CODEX_WIRE_RESULT"


def test_codex_wire_task_id_mismatch_is_rejected():
    with pytest.raises(CodexStructuredResultError) as error:
        CodexStructuredOutputAdapter().parse_agent_result(
            valid_agent_wire(), expected_task_id="different-task"
        )
    assert error.value.error_type == "CODEX_RESULT_TASK_MISMATCH"


def test_codex_wire_path_and_requested_task_still_use_domain_security_validator(tmp_path):
    wire = valid_agent_wire(
        requested_tasks=[
            {
                "action": "unauthorized",
                "role": "experiment_planner",
                "objective": "new work",
                "reason": "not permitted",
                "inputs": [],
            }
        ]
    )
    result = CodexStructuredOutputAdapter().parse_agent_result(wire)
    spec = AgentTaskSpec.model_validate(
        {
            "schema_version": "agent-task/v0.1",
            "project_id": "prj-wire",
            "task_id": "tsk-wire",
            "role": "scientific_supervisor",
            "objective": "validate fixture",
            "deliverables": [
                {
                    "logical_name": "summary",
                    "artifact_kind": "RESULT_SUMMARY",
                    "required": True,
                }
            ],
        }
    )
    validation = AgentResultValidator().validate(result, spec, allowed_root=tmp_path / "run")
    assert "artifact path escapes AgentRun workdir: summary" in validation.policy_violations
    assert "requested_tasks permission denied" in validation.policy_violations


def test_codex_old_open_dict_regression_reports_exact_pointer():
    old = {
        "type": "object",
        "properties": {"metadata": {"type": "object", "additionalProperties": True}},
        "required": ["metadata"],
        "additionalProperties": False,
    }
    with pytest.raises(CodexSchemaCompatibilityError) as error:
        validate_codex_output_schema(old)
    pointers = {item.pointer for item in error.value.issues}
    assert "$.properties.metadata.additionalProperties" in pointers
    assert CodexStructuredOutputAdapter().for_agent_result().compatibility_report.compatible


def test_codex_old_path_format_regression_reports_exact_pointer():
    old = {
        "type": "object",
        "properties": {"path": {"type": "string", "format": "path"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    with pytest.raises(CodexSchemaCompatibilityError) as error:
        validate_codex_output_schema(old)
    assert any(item.pointer == "$.properties.path.format" for item in error.value.issues)
    assert (
        CodexStructuredOutputAdapter()
        .for_agent_result()
        .codex_schema["$defs"]["CodexProducedArtifactWire"]["properties"]["path"]
        == {"type": "string"}
    )


class InvalidStructuredOutput(CodexStructuredOutputAdapter):
    def for_agent_result(self):
        validate_codex_output_schema(
            {
                "type": "object",
                "properties": {"metadata": {"type": "object"}},
                "required": ["metadata"],
                "additionalProperties": False,
            }
        )
        raise AssertionError("validator should have failed")


@pytest.mark.asyncio
async def test_invalid_schema_fails_before_bridge_or_remote_thread(runtime, tmp_path, monkeypatch):
    _engine, factory, workspace = runtime
    _project, task = make_codex_task(factory, tmp_path)
    state = tmp_path / "invalid-schema-state"
    state.mkdir()
    (state / "behavior.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("SARS_FAKE_CODEX_STATE_DIR", str(state))
    config = CodexConfig(
        app_server_command=[
            sys.executable,
            str(Path(__file__).with_name("fake_codex_app_server.py")),
        ],
        request_timeout_sec=1,
        model_tiers={"supervisor": CodexModelTier()},
    )
    adapter = CodexAdapter(
        workspace, config, structured_output=InvalidStructuredOutput()
    )
    controller = ResearchController(
        factory,
        workspace,
        agent_registry=AgentAdapterRegistry([adapter]),
        poll_interval_seconds=0,
    )
    await controller.run_until_idle(timeout_seconds=5, sleep_seconds=0.02)
    with factory() as session:
        run = session.scalar(select(AgentRun).where(AgentRun.task_id == task.id))
        assert session.get(Task, task.id).status is TaskStatus.BLOCKED
        assert run.error_type == "CODEX_OUTPUT_SCHEMA_INVALID"
        assert not adapter.bridge_dir(run.id).exists()
    assert not (state / "counts.json").exists()
