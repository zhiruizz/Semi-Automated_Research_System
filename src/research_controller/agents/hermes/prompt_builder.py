from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_controller.protocols.agent import AgentExecutionRequest


RESULT_START = "<<<SARS_AGENT_RESULT_V1>>>"
RESULT_END = "<<<END_SARS_AGENT_RESULT_V1>>>"

SYSTEM_INSTRUCTIONS = f"""You are a SARS execution worker. Controller protocol is authoritative.
Never modify Controller state or success criteria. Work only inside the assigned run workspace.
The final response MUST contain exactly one {RESULT_START} marker and exactly one
{RESULT_END} marker. The closing marker is {RESULT_END}; never repeat the start marker
as a closing marker. Emit no text after the closing marker. JSON must use the exact keys
and values specified by the user prompt, without aliases, additions, Markdown, or repair."""


def build_context_manifest(request: AgentExecutionRequest) -> dict[str, Any]:
    return {
        "schema_version": "context-manifest/v0.1",
        "project_id": request.task_spec.project_id,
        "task_id": request.task_spec.task_id,
        "items": [
            {
                "artifact_id": item.artifact_id,
                "purpose": item.purpose,
                "mode": item.mode.value,
                "path": item.uri,
                "sha256": item.sha256,
                "logical_name": item.logical_name,
                "artifact_kind": item.artifact_kind,
            }
            for item in request.context_pack.items
        ],
    }


def build_prompt(request: AgentExecutionRequest, manifest_path: Path) -> str:
    spec = request.task_spec
    outputs = request.workdir / "outputs"
    instructions = "\n".join(f"- {item}" for item in spec.instructions) or "- None"
    deliverables = "\n".join(
        f"- {item.logical_name}: kind={item.artifact_kind}, required={item.required}"
        for item in spec.deliverables
    ) or "- None"
    permissions = json.dumps(spec.permissions.model_dump(mode="json"), sort_keys=True)
    result_template = {
        "schema_version": "agent-result/v0.1",
        "task_id": spec.task_id,
        "outcome": "completed",
        "summary": "short factual summary",
        "artifacts": [
            {
                "logical_name": item.logical_name,
                "path": str(outputs / f"{item.logical_name}.md"),
                "artifact_kind": item.artifact_kind,
                "evidence_candidate": item.evidence_candidate,
                "metadata": {},
            }
            for item in spec.deliverables
        ],
        "warnings": [],
        "requested_tasks": [],
        "transition_request": None,
        "protocol_amendment_request": None,
        "needs_escalation": False,
        "escalation": None,
    }
    exact_template = json.dumps(result_template, ensure_ascii=False, indent=2)
    return f"""ROLE
You are an execution worker acting as {spec.role}.

OBJECTIVE
{spec.objective}

INSTRUCTIONS
{instructions}

WORKSPACE
Run workspace: {request.workdir}
Outputs directory: {outputs}
All deliverables MUST be written inside the outputs directory.

CONTEXT MANIFEST
Read only the files you need from: {manifest_path}
Do not assume that large context files are inlined in this prompt.

DELIVERABLE CONTRACT
{deliverables}
Also write the exact final AgentResult JSON to: {outputs / 'agent_result.json'}

PERMISSIONS
{permissions}
You cannot modify Research Controller state, declare the Task successful, change experiment
success criteria, create unauthorized scientific objectives, or invoke a ComputeProvider.
If scientific definitions are ambiguous, return outcome=blocked and explain the ambiguity.

FINAL RESULT CONTRACT
The only permitted top-level keys are the keys shown in this exact template.
Copy every key name exactly; in particular use schema_version (not agent_result_version),
logical_name (not name), and artifact_kind (not kind). Do not add run_id or errors.
Valid outcome values are completed, partial, blocked, and failed.
Use this exact structural template, changing only summary, outcome, warnings, escalation,
and artifact metadata as needed. Keep task_id and schema_version unchanged:
{exact_template}

End your final answer with exactly one envelope and no text after its end marker:
{RESULT_START}
{{COPY OF THE EXACT VALID JSON WRITTEN TO outputs/agent_result.json}}
{RESULT_END}
The envelope JSON and outputs/agent_result.json must be canonically identical.
Do not wrap JSON in Markdown and do not emit approximate or repaired JSON.
Before sending, verify that the very last non-whitespace text is exactly:
{RESULT_END}
Never use {RESULT_START} as the closing marker.
"""
