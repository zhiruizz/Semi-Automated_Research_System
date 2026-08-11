from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from research_controller.protocols.agent import AgentResult, AgentTaskSpec, ProducedArtifact


@dataclass
class AgentResultValidation:
    reasons: list[str] = field(default_factory=list)
    policy_violations: list[str] = field(default_factory=list)
    safe_artifacts: list[ProducedArtifact] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons and not self.policy_violations


class AgentResultValidator:
    def validate(
        self,
        result: AgentResult,
        spec: AgentTaskSpec,
        *,
        allowed_root: Path | str,
    ) -> AgentResultValidation:
        validation = AgentResultValidation()
        root = Path(allowed_root).resolve()
        if result.task_id != spec.task_id:
            validation.policy_violations.append("AgentResult.task_id does not match Task")
        names = [item.logical_name for item in result.artifacts]
        if len(names) != len(set(names)):
            validation.policy_violations.append("duplicate produced artifact logical_name")
        deliverables = {item.logical_name: item for item in spec.deliverables}
        produced_names: set[str] = set()
        for artifact in result.artifacts:
            declared = deliverables.get(artifact.logical_name)
            if declared is None:
                validation.policy_violations.append(
                    f"undeclared produced artifact: {artifact.logical_name}"
                )
                continue
            if artifact.artifact_kind != declared.artifact_kind:
                validation.policy_violations.append(
                    f"artifact kind mismatch for {artifact.logical_name}"
                )
                continue
            candidate = artifact.path
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                validation.policy_violations.append(
                    f"artifact path escapes AgentRun workdir: {artifact.logical_name}"
                )
                continue
            if not resolved.is_file():
                validation.reasons.append(
                    f"produced artifact file missing: {artifact.logical_name}"
                )
                continue
            produced_names.add(artifact.logical_name)
            validation.safe_artifacts.append(artifact.model_copy(update={"path": resolved}))
        for logical_name, deliverable in deliverables.items():
            if deliverable.required and logical_name not in produced_names:
                validation.reasons.append(f"required deliverable missing: {logical_name}")
        if result.requested_tasks and not spec.permissions.request_tasks:
            validation.policy_violations.append("requested_tasks permission denied")
        if result.transition_request is not None:
            if not spec.permissions.request_transition:
                validation.policy_violations.append("transition_request permission denied")
            if result.transition_request.project_id != spec.project_id:
                validation.policy_violations.append(
                    "transition_request project_id does not match Project"
                )
        if (
            result.protocol_amendment_request is not None
            and not spec.permissions.request_protocol_amendment
        ):
            validation.policy_violations.append(
                "protocol_amendment_request permission denied"
            )
        return validation
