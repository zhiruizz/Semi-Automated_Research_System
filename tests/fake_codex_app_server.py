"""Durable fake Codex App Server used only by Phase 5 tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import sys
import time
import uuid


ROOT = Path(os.environ["SARS_FAKE_CODEX_STATE_DIR"]).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
BEHAVIOR_PATH = ROOT / "behavior.json"
THREADS = ROOT / "threads"
THREADS.mkdir(exist_ok=True)


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(path: Path, value) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def send(value) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def count(name: str) -> None:
    path = ROOT / "counts.json"
    value = load(path, {})
    value[name] = int(value.get(name, 0)) + 1
    save(path, value)


def thread_path(thread_id: str) -> Path:
    return THREADS / f"{thread_id}.json"


def user_text(turn) -> str:
    return turn["items"][0]["content"][0]["text"]


def result_for(prompt: str, behavior: dict) -> str:
    task_match = re.search(r"TASK ID\n([^\n]+)", prompt)
    task_id = task_match.group(1).strip() if task_match else "unknown"
    deliverable_match = re.search(r"- ([A-Za-z0-9._-]+): kind=([^,\n]+)", prompt)
    artifacts = []
    if deliverable_match:
        logical, kind = deliverable_match.groups()
        output = Path.cwd() / "outputs" / f"{logical}.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("Fake Codex evidence-grounded summary.\n", encoding="utf-8")
        artifacts.append(
            {
                "logical_name": logical,
                "path": str(output),
                "artifact_kind": kind,
                "evidence_candidate": False,
                "metadata_json": "{\"source\":\"fake-codex\"}",
            }
        )
    value = {
        "schema_version": "agent-result/v0.1",
        "task_id": task_id,
        "outcome": "completed",
        "summary": behavior.get("summary", "Fake Codex completed the scientific task."),
        "artifacts": artifacts,
        "warnings": [],
        "requested_tasks": [],
        "transition_request": None,
        "protocol_amendment_request": None,
        "needs_escalation": False,
        "escalation": None,
    }
    if behavior.get("invalid_result"):
        value["unexpected"] = True
    if isinstance(behavior.get("transition_request"), dict):
        value["transition_request"] = behavior["transition_request"]
    if isinstance(behavior.get("requested_tasks"), list):
        value["requested_tasks"] = behavior["requested_tasks"]
    if behavior.get("tamper_context"):
        staged = next((Path.cwd() / "inputs").iterdir())
        staged.chmod(0o644)
        staged.write_text("tampered\n", encoding="utf-8")
        manifest_path = Path.cwd() / "context_manifest.json"
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        import hashlib

        manifest["items"][0]["sha256"] = hashlib.sha256(staged.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return json.dumps(value, separators=(",", ":"))


def schema_is_strict(value) -> bool:
    if isinstance(value, dict):
        if (value.get("type") == "object" or "properties" in value) and value.get(
            "additionalProperties"
        ) is not False:
            return False
        properties = value.get("properties")
        if isinstance(properties, dict) and set(value.get("required", [])) != set(properties):
            return False
        return all(schema_is_strict(item) for item in value.values())
    if isinstance(value, list):
        return all(schema_is_strict(item) for item in value)
    return True


def complete(thread: dict, turn: dict, behavior: dict, status: str = "completed") -> None:
    if status == "completed":
        turn["items"].append(
            {"type": "agentMessage", "id": f"msg-{uuid.uuid4()}", "text": result_for(user_text(turn), behavior)}
        )
    turn["status"] = status
    if status == "failed":
        turn["error"] = {"message": behavior.get("failure_message", "fake turn failed")}
    turn["completedAt"] = int(time.time())
    save(thread_path(thread["id"]), thread)
    send(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": thread["id"],
                "turnId": turn["id"],
                "tokenUsage": {
                    "last": {
                        "inputTokens": 17,
                        "cachedInputTokens": 3,
                        "outputTokens": 11,
                        "reasoningOutputTokens": 2,
                        "totalTokens": 28,
                    },
                    "total": {
                        "inputTokens": 117,
                        "cachedInputTokens": 13,
                        "outputTokens": 41,
                        "reasoningOutputTokens": 4,
                        "totalTokens": 158,
                    },
                },
            },
        }
    )
    completed_turn = turn
    if behavior.get("summary_only_completion"):
        completed_turn = {
            key: turn.get(key)
            for key in ("id", "status", "startedAt", "completedAt", "error")
        }
    send(
        {
            "method": "turn/completed",
            "params": {"threadId": thread["id"], "turn": completed_turn},
        }
    )


def main() -> None:
    initialized = False
    pending = None
    pending_approval_id = None
    behavior = load(BEHAVIOR_PATH, {})
    while True:
        if pending and not pending_approval_id and time.monotonic() >= pending["complete_at"]:
            complete(pending["thread"], pending["turn"], behavior)
            pending = None
        ready, _, _ = select.select([sys.stdin], [], [], 0.03)
        if not ready:
            continue
        line = sys.stdin.readline()
        if not line:
            return
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            initialized = True
            send({"id": request_id, "result": {"userAgent": "fake-codex/1", "platformFamily": "unix", "platformOs": "linux"}})
        elif method == "initialized":
            continue
        elif not initialized:
            send({"id": request_id, "error": {"code": -32000, "message": "Not initialized"}})
        elif method == "account/read":
            account = None if behavior.get("auth_required") else {"type": "chatgpt"}
            send({"id": request_id, "result": {"account": account, "requiresOpenaiAuth": True}})
        elif method == "model/list":
            send(
                {
                    "id": request_id,
                    "result": {
                        "data": [
                            {
                                "id": "fake-supervisor",
                                "model": "fake-supervisor",
                                "isDefault": True,
                                "hidden": False,
                                "defaultReasoningEffort": "medium",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low"},
                                    {"reasoningEffort": "medium"},
                                ],
                            }
                        ]
                    },
                }
            )
        elif method == "thread/start":
            count("thread_start")
            save(ROOT / "last_thread_start_params.json", params)
            history = load(ROOT / "thread_start_params.json", [])
            history.append(params)
            save(ROOT / "thread_start_params.json", history)
            thread_id = f"thr-{uuid.uuid4()}"
            thread = {
                "id": thread_id,
                "sessionId": f"tree-{uuid.uuid4()}",
                "cwd": params.get("cwd"),
                "modelProvider": "fake",
                "status": "idle",
                "turns": [],
            }
            save(thread_path(thread_id), thread)
            if behavior.get("drop_thread_response"):
                return
            send({"id": request_id, "result": {"thread": thread}})
        elif method == "thread/resume":
            count("thread_resume")
            path = thread_path(params["threadId"])
            if not path.exists():
                send({"id": request_id, "error": {"code": -32000, "message": "thread not found"}})
                continue
            thread = load(path, {})
            thread["cwd"] = params.get("cwd", thread.get("cwd"))
            save(path, thread)
            send({"id": request_id, "result": {"thread": thread}})
        elif method == "thread/read":
            path = thread_path(params["threadId"])
            if not path.exists():
                send({"id": request_id, "error": {"code": -32000, "message": "thread not found"}})
                continue
            thread = load(path, {})
            if not params.get("includeTurns"):
                thread = {**thread, "turns": []}
            send({"id": request_id, "result": {"thread": thread}})
        elif method == "turn/start":
            count("turn_start")
            thread = load(thread_path(params["threadId"]), {})
            if not thread:
                send({"id": request_id, "error": {"code": -32000, "message": "thread not found"}})
                continue
            prompt = params["input"][0]["text"]
            save(ROOT / "last_turn_params.json", params)
            turn = {
                "id": f"turn-{uuid.uuid4()}",
                "status": "inProgress",
                "items": [
                    {
                        "type": "userMessage",
                        "id": f"user-{uuid.uuid4()}",
                        "content": [{"type": "text", "text": prompt}],
                    }
                ],
                "startedAt": int(time.time()),
            }
            thread["turns"].append(turn)
            save(thread_path(thread["id"]), thread)
            if behavior.get("drop_turn_response"):
                complete(thread, turn, behavior)
                return
            send({"id": request_id, "result": {"turn": turn}})
            if not schema_is_strict(params.get("outputSchema")):
                behavior = {**behavior, "failure_message": "invalid strict output schema"}
                complete(thread, turn, behavior, status="failed")
                continue
            if behavior.get("approval"):
                pending = {"thread": thread, "turn": turn, "complete_at": float("inf")}
                pending_approval_id = 9001
                send(
                    {
                        "id": pending_approval_id,
                        "method": "item/commandExecution/requestApproval",
                        "params": {
                            "threadId": thread["id"],
                            "turnId": turn["id"],
                            "itemId": "cmd-1",
                            "reason": "test command",
                            "command": "printf safe",
                            "cwd": str(Path.cwd()),
                        },
                    }
                )
            else:
                pending = {
                    "thread": thread,
                    "turn": turn,
                    "complete_at": time.monotonic() + float(behavior.get("delay_sec", 0)),
                }
                if behavior.get("turn_failed"):
                    complete(thread, turn, behavior, status="failed")
                    pending = None
        elif method == "turn/interrupt":
            count("turn_interrupt")
            thread = load(thread_path(params["threadId"]), {})
            turn = next(item for item in thread.get("turns", []) if item["id"] == params["turnId"])
            send({"id": request_id, "result": {}})
            complete(thread, turn, behavior, status="interrupted")
            pending = None
            pending_approval_id = None
        elif request_id == pending_approval_id and "result" in message:
            save(ROOT / "approval_response.json", message["result"])
            pending_approval_id = None
            pending["complete_at"] = time.monotonic()
        else:
            send({"id": request_id, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
