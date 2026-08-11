from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from research_controller.protocols.agent import (
    AgentResult,
    AgentTaskSpec,
    DecisionRequest,
    DecisionResult,
)
from research_controller.services.agent_validation import AgentResultValidator


def spec_value(**overrides):
    value = {
        "schema_version": "agent-task/v0.1",
        "project_id": "p1",
        "task_id": "t1",
        "role": "implementation_worker",
        "objective": "test",
        "deliverables": [
            {
                "logical_name": "summary",
                "artifact_kind": "RESULT_SUMMARY",
                "required": True,
            }
        ],
    }
    value.update(overrides)
    return value


def result_value(path: Path, **overrides):
    value = {
        "schema_version": "agent-result/v0.1",
        "task_id": "t1",
        "outcome": "completed",
        "summary": "done",
        "artifacts": [
            {
                "logical_name": "summary",
                "path": str(path),
                "artifact_kind": "RESULT_SUMMARY",
            }
        ],
    }
    value.update(overrides)
    return value


def test_agent_protocol_strict_rejects_unknown_fields_and_model_name():
    with pytest.raises(ValidationError):
        AgentTaskSpec.model_validate(spec_value(model="forbidden-model"))
    with pytest.raises(ValidationError):
        AgentResult.model_validate(result_value(Path("x"), surprise=True))


def test_agent_result_task_id_must_match(tmp_path):
    output = tmp_path / "summary.txt"
    output.write_text("ok", encoding="utf-8")
    validation = AgentResultValidator().validate(
        AgentResult.model_validate(result_value(output, task_id="another")),
        AgentTaskSpec.model_validate(spec_value()),
        allowed_root=tmp_path,
    )
    assert "AgentResult.task_id does not match Task" in validation.policy_violations
    assert not validation.passed


def test_agent_required_deliverable(tmp_path):
    result = AgentResult.model_validate(result_value(tmp_path / "missing", artifacts=[]))
    validation = AgentResultValidator().validate(
        result, AgentTaskSpec.model_validate(spec_value()), allowed_root=tmp_path
    )
    assert validation.reasons == ["required deliverable missing: summary"]


def test_agent_permission_request_tasks(tmp_path):
    output = tmp_path / "summary.txt"
    output.write_text("ok", encoding="utf-8")
    result = AgentResult.model_validate(
        result_value(
            output,
            requested_tasks=[
                {
                    "action": "more",
                    "role": "implementation_worker",
                    "objective": "more tests",
                    "reason": "evidence",
                }
            ],
        )
    )
    validation = AgentResultValidator().validate(
        result, AgentTaskSpec.model_validate(spec_value()), allowed_root=tmp_path
    )
    assert "requested_tasks permission denied" in validation.policy_violations


def test_agent_permission_transition(tmp_path):
    output = tmp_path / "summary.txt"
    output.write_text("ok", encoding="utf-8")
    result = AgentResult.model_validate(
        result_value(
            output,
            transition_request={
                "project_id": "p1",
                "from_stage": "TOY_GATE",
                "to_stage": "FULL_IMPLEMENT",
                "reason": "passed",
            },
        )
    )
    validation = AgentResultValidator().validate(
        result, AgentTaskSpec.model_validate(spec_value()), allowed_root=tmp_path
    )
    assert "transition_request permission denied" in validation.policy_violations


def test_agent_permission_protocol_amendment(tmp_path):
    output = tmp_path / "summary.txt"
    output.write_text("ok", encoding="utf-8")
    result = AgentResult.model_validate(
        result_value(
            output,
            protocol_amendment_request={
                "reason": "change success criteria",
                "proposed_changes": {"threshold": 0},
            },
        )
    )
    validation = AgentResultValidator().validate(
        result, AgentTaskSpec.model_validate(spec_value()), allowed_root=tmp_path
    )
    assert "protocol_amendment_request permission denied" in validation.policy_violations


def test_agent_artifact_path_escape(tmp_path):
    validation = AgentResultValidator().validate(
        AgentResult.model_validate(result_value(Path("/etc/passwd"))),
        AgentTaskSpec.model_validate(spec_value()),
        allowed_root=tmp_path,
    )
    assert any("escapes" in item for item in validation.policy_violations)
    assert validation.safe_artifacts == []


def test_decision_result_must_be_allowed():
    request = DecisionRequest(
        project_id="p1",
        decision_type="toy_effectiveness",
        question="continue?",
        allowed_decisions=["CONTINUE", "REJECT_IDEA"],
    )
    DecisionResult(
        decision="CONTINUE", confidence=0.8, rationale="evidence"
    ).validate_for(request)
    with pytest.raises(ValueError, match="not one of"):
        DecisionResult(
            decision="UNKNOWN", confidence=0.2, rationale="none"
        ).validate_for(request)
