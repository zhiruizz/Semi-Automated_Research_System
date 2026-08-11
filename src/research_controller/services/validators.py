from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from research_controller.db.models import AgentRun, Artifact, ComputeJob, Task
from research_controller.domain.enums import (
    AgentRunStatus,
    ArtifactIntegrityStatus,
    ComputeExecutionStatus,
)
from research_controller.protocols.compute import ComputeTaskSpec
from research_controller.protocols.agent import AgentOutcome, AgentTaskSpec


class ValidationResult:
    def __init__(self, passed: bool, reasons: list[str] | None = None) -> None:
        self.passed = passed
        self.reasons = reasons or []


def _contains_nonfinite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_nonfinite(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_nonfinite(item) for item in value)
    return False


class TaskValidator:
    def validate_compute_task(self, session: Session, task: Task) -> ValidationResult:
        spec = ComputeTaskSpec.model_validate(task.spec_json)
        job = session.scalar(
            select(ComputeJob)
            .where(ComputeJob.task_id == task.id)
            .order_by(ComputeJob.attempt_no.desc())
        )
        if job is None:
            return ValidationResult(False, ["compute job missing"])

        reasons: list[str] = []
        if spec.success.require_zero_exit_code and job.exit_code != 0:
            reasons.append(f"exit code is {job.exit_code!r}, expected 0")
        if job.execution_status is not ComputeExecutionStatus.SUCCEEDED:
            reasons.append(f"compute job terminal status is {job.execution_status.value}")

        artifacts = session.scalars(select(Artifact).where(Artifact.task_id == task.id)).all()
        by_name = {artifact.logical_name: artifact for artifact in artifacts}
        required_names = set(task.acceptance_policy_json.get("required_artifacts", []))
        required_names.update({output.logical_name for output in spec.outputs if output.required})
        required_names.update({"run.out", "run.error", "exit.json"})
        for logical_name in sorted(required_names):
            matching = [
                artifact
                for name, artifact in by_name.items()
                if name == logical_name or name.startswith(f"{logical_name}:")
            ]
            if not matching:
                reasons.append(f"required artifact missing: {logical_name}")
            elif any(
                artifact.integrity_status is not ArtifactIntegrityStatus.VERIFIED
                for artifact in matching
            ):
                reasons.append(f"required artifact is not verified: {logical_name}")

        validators = set(spec.success.required_validators)
        validators.update(task.acceptance_policy_json.get("validators", []))
        json_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.kind == "METRICS" or artifact.logical_name.startswith("metrics")
        ]
        if "metrics_json" in validators or "no_nan" in validators:
            if not json_artifacts:
                reasons.append("metrics validator requested but no metrics artifact exists")
            for artifact in json_artifacts:
                try:
                    with Path(artifact.uri).open("r", encoding="utf-8") as handle:
                        value = json.load(handle)
                except (OSError, json.JSONDecodeError) as exc:
                    reasons.append(f"invalid metrics JSON {artifact.logical_name}: {exc}")
                    continue
                if "no_nan" in validators and _contains_nonfinite(value):
                    reasons.append(f"non-finite metric in {artifact.logical_name}")

        if not reasons:
            evidence_names = {
                output.logical_name for output in spec.outputs if output.evidence_candidate
            }
            for artifact in artifacts:
                if artifact.logical_name in evidence_names or any(
                    artifact.logical_name.startswith(f"{name}:") for name in evidence_names
                ):
                    artifact.evidence_eligible = True
        return ValidationResult(not reasons, reasons)

    def validate_agent_task(self, session: Session, task: Task) -> ValidationResult:
        spec = AgentTaskSpec.model_validate(task.spec_json)
        run = session.scalar(
            select(AgentRun)
            .where(AgentRun.task_id == task.id)
            .order_by(AgentRun.attempt_no.desc())
        )
        if run is None:
            return ValidationResult(False, ["AgentRun missing"])
        reasons: list[str] = []
        if run.status is not AgentRunStatus.SUCCEEDED:
            reasons.append(f"AgentRun status is {run.status.value}")
        outcome = task.result_summary_json.get("agent_outcome")
        if outcome != AgentOutcome.COMPLETED.value:
            reasons.append(f"Agent outcome is {outcome!r}, expected 'completed'")
        reasons.extend(task.result_summary_json.get("validation_reasons", []))
        reasons.extend(task.result_summary_json.get("policy_violations", []))
        if run.request_artifact_id is None:
            reasons.append("Agent request Artifact missing")
        if run.response_artifact_id is None:
            reasons.append("Agent response Artifact missing")
        artifacts = session.scalars(select(Artifact).where(Artifact.task_id == task.id)).all()
        for deliverable in spec.deliverables:
            matching = [
                artifact
                for artifact in artifacts
                if artifact.logical_name == deliverable.logical_name
                and artifact.kind == deliverable.artifact_kind
            ]
            if deliverable.required and not matching:
                reasons.append(f"required deliverable missing: {deliverable.logical_name}")
            elif matching and any(
                artifact.integrity_status is not ArtifactIntegrityStatus.VERIFIED
                for artifact in matching
            ):
                reasons.append(
                    f"required deliverable is not verified: {deliverable.logical_name}"
                )
        if not reasons:
            evidence_names = {
                item.logical_name for item in spec.deliverables if item.evidence_candidate
            }
            for artifact in artifacts:
                if artifact.logical_name in evidence_names:
                    artifact.evidence_eligible = True
        return ValidationResult(not reasons, reasons)
