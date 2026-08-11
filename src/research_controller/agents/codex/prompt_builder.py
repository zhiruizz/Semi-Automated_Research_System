from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from typing import Any

from research_controller.agents.base import AgentAdapterError
from research_controller.agents.codex.util import atomic_json, sha256_file
from research_controller.protocols.agent import AgentExecutionRequest, ContextPack


SYSTEM_INSTRUCTIONS = """You are a scientific Agent inside the Semi-Automated Research System.
The Controller's task specification, permissions, evidence boundaries, and output schema are
authoritative. Do not claim that a task, experiment, gate, or project succeeded. Do not invent
evidence, citations, measurements, files, or completed work. Distinguish supplied facts from
inferences and uncertainty. Use only the staged context and work inside the assigned AgentRun
workspace. If evidence is missing or instructions conflict, return a blocked or partial result.
Return only the JSON value required by the native output schema. Do not reveal hidden reasoning
or chain-of-thought; concise conclusions and evidence references are sufficient."""


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return normalized[:120] or "context"


def _target_name(index: int, logical_name: str, source: Path) -> str:
    safe = _safe_name(logical_name)
    suffix = source.suffix[:20]
    return f"{index:03d}-{safe}{suffix if suffix and not safe.endswith(suffix) else ''}"


def build_context_manifest(request: AgentExecutionRequest) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for index, item in enumerate(request.context_pack.items):
        source = Path(item.uri).resolve()
        items.append(
            {
                "artifact_id": item.artifact_id,
                "purpose": item.purpose,
                "mode": item.mode.value,
                "path": str(Path("inputs") / _target_name(index, item.logical_name, source)),
                "sha256": item.sha256,
                "logical_name": item.logical_name,
                "artifact_kind": item.artifact_kind,
            }
        )
    return {
        "schema_version": "codex-context-manifest/v0.1",
        "project_id": request.task_spec.project_id,
        "task_id": request.task_spec.task_id,
        "copy_strategy": "verified-disposable-copy",
        "items": items,
    }


def stage_context(request: AgentExecutionRequest) -> dict[str, Any]:
    run_root = request.workdir.resolve()
    inputs = run_root / "inputs"
    outputs = run_root / "outputs"
    result_dir = run_root / "result"
    inputs.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_context_manifest(request)
    for index, item in enumerate(request.context_pack.items):
        source = Path(item.uri).resolve()
        if not source.is_file():
            raise AgentAdapterError("CODEX_CONTEXT_MISSING", f"context source is missing: {item.logical_name}")
        if sha256_file(source) != item.sha256:
            raise AgentAdapterError("CODEX_CONTEXT_HASH_MISMATCH", f"context hash mismatch: {item.logical_name}")
        target = inputs / _target_name(index, item.logical_name, source)
        if target.exists():
            if sha256_file(target) != item.sha256:
                raise AgentAdapterError("CODEX_STAGED_CONTEXT_CHANGED", f"staged context changed: {target.name}", block_task=True)
        else:
            shutil.copy2(source, target)
        target.chmod(0o444)
    atomic_json(run_root / "context_manifest.json", manifest)
    (run_root / "context_manifest.json").chmod(0o444)
    return manifest


def build_prompt(request: AgentExecutionRequest, manifest: dict[str, Any]) -> str:
    spec = request.task_spec
    instructions = "\n".join(f"- {item}" for item in spec.instructions) or "- None"
    deliverables = "\n".join(
        f"- {item.logical_name}: kind={item.artifact_kind}, required={item.required}, evidence_candidate={item.evidence_candidate}"
        for item in spec.deliverables
    ) or "- None"
    return f"""SARS_AGENT_RUN_ID={request.run_key}

ROLE
{spec.role}

TASK ID
{spec.task_id}

OBJECTIVE
{spec.objective}

CONTROLLER INSTRUCTIONS
{instructions}

STAGED CONTEXT
Manifest: context_manifest.json
Items: {json.dumps(manifest['items'], ensure_ascii=False, sort_keys=True)}
Treat staged files as read-only evidence. Never modify or replace them.

DELIVERABLES
{deliverables}
Write declared deliverable files only below outputs/. Paths returned in AgentResult must resolve
inside this AgentRun workspace. Do not write to the Controller source repository.

PERMISSIONS
{json.dumps(spec.permissions.model_dump(mode='json'), sort_keys=True)}
Filesystem and network enforcement is supplied separately by the App Server sandbox policy.

RESULT AUTHORITY
Return exactly one JSON object matching the supplied Codex wire schema. Keep schema_version and
task_id exact. Wire artifact paths are ordinary strings. Wire metadata fields named metadata_json
or proposed_changes_json must be either null or a JSON-encoded object string. Do not wrap the
result in Markdown, add commentary, or attempt JSON repair.
Controller validation, not this Agent, determines acceptance and project state.
"""


def sandbox_policy(request: AgentExecutionRequest) -> dict[str, Any]:
    if request.adapter_config.get("sandbox") in {"danger-full-access", "dangerFullAccess"}:
        raise AgentAdapterError(
            "CODEX_FULL_ACCESS_FORBIDDEN",
            "danger-full-access is forbidden for Codex AgentRuns",
            block_task=True,
        )
    root = request.workdir.resolve()
    if not request.task_spec.permissions.filesystem_write:
        return {
            "type": "readOnly",
            "networkAccess": bool(request.task_spec.permissions.network),
        }
    # The source CAS is not in any writable root. Context inside this root is a
    # disposable verified copy and is hash-checked again after the turn.
    return {
        "type": "workspaceWrite",
        "writableRoots": [str(root / "outputs"), str(root / "result")],
        "networkAccess": bool(request.task_spec.permissions.network),
        "excludeSlashTmp": True,
        "excludeTmpdirEnvVar": True,
    }


def validate_staged_context(run_root: Path, context_pack: ContextPack) -> None:
    expected_items: list[dict[str, Any]] = []
    for index, item in enumerate(context_pack.items):
        source = Path(item.uri).resolve()
        relative = Path("inputs") / _target_name(index, item.logical_name, source)
        expected_items.append(
            {
                "artifact_id": item.artifact_id,
                "purpose": item.purpose,
                "mode": item.mode.value,
                "path": str(relative),
                "sha256": item.sha256,
                "logical_name": item.logical_name,
                "artifact_kind": item.artifact_kind,
            }
        )
        target = (run_root / relative).resolve()
        if not target.is_relative_to(run_root / "inputs") or not target.is_file():
            raise AgentAdapterError("CODEX_STAGED_CONTEXT_CHANGED", "staged context was removed", block_task=True)
        if sha256_file(target) != item.sha256:
            raise AgentAdapterError("CODEX_STAGED_CONTEXT_CHANGED", f"staged context changed: {target.name}", block_task=True)
    expected_manifest = {
        "schema_version": "codex-context-manifest/v0.1",
        "project_id": context_pack.project_id,
        "task_id": context_pack.task_id,
        "copy_strategy": "verified-disposable-copy",
        "items": expected_items,
    }
    manifest = json.loads((run_root / "context_manifest.json").read_text(encoding="utf-8"))
    if manifest != expected_manifest:
        raise AgentAdapterError(
            "CODEX_STAGED_CONTEXT_CHANGED",
            "staged context manifest was modified",
            block_task=True,
        )
