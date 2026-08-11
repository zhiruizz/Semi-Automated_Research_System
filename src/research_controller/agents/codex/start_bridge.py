from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from research_controller.agents.codex.client import CodexAppServerClient
from research_controller.agents.codex.models import CodexConfig, CodexRpcError
from research_controller.agents.codex.util import (
    atomic_json,
    atomic_text,
    load_object,
    redact_text,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claim(path: Path, *, recover: bool) -> bool:
    name = "recovery.claim" if recover else "launch.claim"
    try:
        descriptor = os.open(path / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "claimed_at": _now()}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _safe_thread(value: dict[str, Any], response: dict[str, Any] | None = None) -> dict[str, Any]:
    response = response or {}
    return {
        "id": value.get("id"),
        "session_tree_id": value.get("sessionId"),
        "cwd": value.get("cwd"),
        "model_provider": value.get("modelProvider"),
        "status": value.get("status"),
        "model": response.get("model"),
        "reasoning_effort": response.get("reasoningEffort"),
        "sandbox": response.get("sandbox"),
        "approval_policy": response.get("approvalPolicy"),
        "instruction_sources": [str(item) for item in response.get("instructionSources", [])][:100],
        "recorded_at": _now(),
    }


def _safe_turn(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value.get("id"),
        "status": value.get("status"),
        "started_at": value.get("startedAt"),
        "completed_at": value.get("completedAt"),
        "recorded_at": _now(),
    }


def _turn_contains_marker(turn: dict[str, Any], marker: str) -> bool:
    for item in turn.get("items", []):
        if not isinstance(item, dict) or item.get("type") != "userMessage":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and marker in str(content.get("text", "")):
                return True
    return False


def _agent_message(turn: dict[str, Any]) -> str | None:
    messages = [
        str(item.get("text", ""))
        for item in turn.get("items", [])
        if isinstance(item, dict)
        and item.get("type") == "agentMessage"
        and item.get("text") is not None
    ]
    return messages[-1] if messages else None


class BridgeSession:
    def __init__(self, bridge_dir: Path, request: dict[str, Any]) -> None:
        self.bridge_dir = bridge_dir
        self.request = request
        self.config = CodexConfig.model_validate(request["config"])
        self.workdir = Path(request["workdir"]).resolve()
        self.thread_id: str | None = request.get("session_id")
        self.turn_id: str | None = None
        self.terminal: dict[str, Any] | None = None
        self.terminal_event = asyncio.Event()
        self.trace: list[dict[str, Any]] = []
        self.client: CodexAppServerClient | None = None

    def _trace(self, message: dict[str, Any]) -> None:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        self.trace.append(
            {
                "at": _now(),
                "method": message.get("method"),
                "thread_id": params.get("threadId"),
                "turn_id": params.get("turnId") or turn.get("id"),
                "status": turn.get("status"),
            }
        )
        self.trace = self.trace[-self.config.max_trace_events :]
        atomic_json(self.bridge_dir / "trace.json", {"events": self.trace})

    async def notification(self, message: dict[str, Any]) -> None:
        self._trace(message)
        method = str(message.get("method", ""))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "thread/tokenUsage/updated" or method.endswith("tokenUsage/updated"):
            usage = params.get("tokenUsage")
            if isinstance(usage, dict):
                atomic_json(
                    self.bridge_dir / "usage.json",
                    {
                        "thread_id": params.get("threadId"),
                        "turn_id": params.get("turnId"),
                        "last": usage.get("last", {}),
                        "total": usage.get("total", {}),
                        "recorded_at": _now(),
                    },
                )
        if method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, dict) and (
                self.turn_id is None or turn.get("id") == self.turn_id
            ):
                self.terminal = turn
                self.terminal_event.set()

    async def approval(self, message: dict[str, Any]) -> dict[str, Any]:
        method = str(message.get("method", ""))
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "decline"}
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        record = {
            "request_id": message.get("id"),
            "method": method,
            "thread_id": params.get("threadId"),
            "turn_id": params.get("turnId"),
            "item_id": params.get("itemId"),
            "reason": redact_text(str(params.get("reason", ""))),
            "command": redact_text(str(params.get("command", ""))),
            "cwd": str(params.get("cwd", ""))[:1000],
            "grant_root": str(params.get("grantRoot", ""))[:1000],
            "requested_at": _now(),
        }
        atomic_json(self.bridge_dir / "approval.json", record)
        decision_path = self.bridge_dir / "approval-decision.json"
        while True:
            if decision_path.exists():
                decision = load_object(decision_path)
                choice = decision.get("choice")
                if choice not in {"once", "deny"}:
                    choice = "deny"
                native = "accept" if choice == "once" else "decline"
                atomic_json(
                    self.bridge_dir / "approval-resolved.json",
                    {**record, "choice": choice, "native_decision": native, "resolved_at": _now()},
                )
                decision_path.unlink(missing_ok=True)
                (self.bridge_dir / "approval.json").unlink(missing_ok=True)
                return {"decision": native}
            if (self.bridge_dir / "interrupt-request.json").exists():
                (self.bridge_dir / "approval.json").unlink(missing_ok=True)
                return {"decision": "decline"}
            await asyncio.sleep(self.config.approval_poll_interval_sec)

    async def _connect(self) -> CodexAppServerClient:
        client = CodexAppServerClient(
            self.config,
            cwd=self.workdir,
            server_request_handler=self.approval,
            notification_handler=self.notification,
        )
        await client.connect()
        self.client = client
        return client

    async def _read_thread(self, client: CodexAppServerClient) -> dict[str, Any]:
        assert self.thread_id
        response = await client.request(
            "thread/read", {"threadId": self.thread_id, "includeTurns": True}
        )
        thread = response.get("thread") if isinstance(response, dict) else None
        if not isinstance(thread, dict):
            raise CodexRpcError("CODEX_PROTOCOL_ERROR", "thread/read omitted thread")
        return thread

    async def _recover_turn(self, client: CodexAppServerClient) -> dict[str, Any]:
        thread = await self._read_thread(client)
        marker = f"SARS_AGENT_RUN_ID={self.request['run_key']}"
        turns = [item for item in thread.get("turns", []) if isinstance(item, dict)]
        if self.turn_id:
            matches = [turn for turn in turns if turn.get("id") == self.turn_id]
        else:
            matches = [turn for turn in turns if _turn_contains_marker(turn, marker)]
        if len(matches) != 1:
            raise CodexRpcError(
                "CODEX_START_STATE_UNCERTAIN",
                "known thread does not contain exactly one turn with the AgentRun marker",
                uncertain=True,
            )
        turn = matches[0]
        self.turn_id = str(turn["id"])
        atomic_json(self.bridge_dir / "turn.json", _safe_turn(turn))
        return turn

    async def _start_or_resume_thread(self, client: CodexAppServerClient) -> dict[str, Any]:
        params = dict(self.request["thread_params"])
        if self.thread_id:
            params["threadId"] = self.thread_id
            result = await client.request("thread/resume", params)
        else:
            try:
                result = await client.request("thread/start", params)
            except CodexRpcError as exc:
                raise CodexRpcError(
                    "CODEX_START_STATE_UNCERTAIN" if exc.uncertain else exc.error_type,
                    str(exc),
                    uncertain=exc.uncertain,
                ) from exc
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or not thread.get("id"):
            raise CodexRpcError("CODEX_PROTOCOL_ERROR", "thread operation omitted thread id")
        self.thread_id = str(thread["id"])
        atomic_json(self.bridge_dir / "thread.json", _safe_thread(thread, result))
        return thread

    async def _start_turn(self, client: CodexAppServerClient) -> dict[str, Any]:
        assert self.thread_id
        params = dict(self.request["turn_params"])
        params["threadId"] = self.thread_id
        try:
            result = await client.request("turn/start", params)
        except CodexRpcError as exc:
            if not exc.uncertain:
                raise
            await client.close()
            recovery = await self._connect()
            return await self._recover_turn(recovery)
        turn = result.get("turn") if isinstance(result, dict) else None
        if not isinstance(turn, dict) or not turn.get("id"):
            raise CodexRpcError("CODEX_PROTOCOL_ERROR", "turn/start omitted turn id")
        self.turn_id = str(turn["id"])
        atomic_json(self.bridge_dir / "turn.json", _safe_turn(turn))
        return turn

    async def _interrupt_once(self) -> None:
        if not (self.bridge_dir / "interrupt-request.json").exists():
            return
        if (self.bridge_dir / "interrupt.sent.json").exists():
            return
        if self.client is None or self.thread_id is None or self.turn_id is None:
            return
        atomic_json(self.bridge_dir / "interrupt.sent.json", {"sent_at": _now()})
        await self.client.request(
            "turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}
        )

    async def _wait_terminal(self, initial: dict[str, Any]) -> dict[str, Any]:
        if initial.get("status") in {"completed", "failed", "interrupted"}:
            return initial
        while True:
            try:
                await asyncio.wait_for(
                    self.terminal_event.wait(), timeout=self.config.poll_interval_sec
                )
            except asyncio.TimeoutError:
                try:
                    await self._interrupt_once()
                except CodexRpcError:
                    pass
                if self.client and self.client.process and self.client.process.returncode is not None:
                    fresh = await self._connect()
                    current = await self._recover_turn(fresh)
                    if current.get("status") in {"completed", "failed", "interrupted"}:
                        return current
                continue
            assert self.terminal is not None
            return self.terminal

    def _record_terminal(self, turn: dict[str, Any]) -> None:
        status = str(turn.get("status", "unknown"))
        message = _agent_message(turn)
        if status == "completed" and message is None:
            raise CodexRpcError(
                "CODEX_RESULT_MISSING", "completed turn has no final agent message"
            )
        if message is not None:
            atomic_text(self.workdir / "raw_response.txt", message)
        error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
        terminal = {
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "status": status,
            "error": redact_text(str(error.get("message", error))) if error else None,
            "response_path": str(self.workdir / "raw_response.txt") if message is not None else None,
            "completed_at": _now(),
        }
        atomic_json(self.bridge_dir / "terminal.json", terminal)

    async def run(self, *, recover: bool) -> None:
        client = await self._connect()
        try:
            if recover:
                thread_record = load_object(self.bridge_dir / "thread.json")
                self.thread_id = str(thread_record["id"])
                if (self.bridge_dir / "turn.json").exists():
                    turn_record = load_object(self.bridge_dir / "turn.json")
                    self.turn_id = str(turn_record["id"])
                initial = await self._recover_turn(client)
            else:
                await self._start_or_resume_thread(client)
                initial = await self._start_turn(client)
            terminal = await self._wait_terminal(initial)
            # Current App Server versions may emit turn/completed with a
            # summary-only Turn that omits items. Hydrate that same durable
            # turn through thread/read; this is observation, never a new turn.
            if terminal.get("status") == "completed" and _agent_message(terminal) is None:
                assert self.client is not None
                terminal = await self._recover_turn(self.client)
            self._record_terminal(terminal)
        finally:
            if self.client is not None:
                await self.client.close()


async def launch(bridge_dir: Path, *, recover: bool = False) -> None:
    if not _claim(bridge_dir, recover=recover):
        return
    request = load_object(bridge_dir / "request.json")
    try:
        await BridgeSession(bridge_dir, request).run(recover=recover)
    except CodexRpcError as exc:
        atomic_json(
            bridge_dir / "error.json",
            {
                "error_type": exc.error_type,
                "message": redact_text(str(exc)),
                "uncertain": exc.uncertain,
                "recorded_at": _now(),
            },
        )
    except Exception as exc:
        atomic_json(
            bridge_dir / "error.json",
            {
                "error_type": "CODEX_BRIDGE_FAILED",
                "message": redact_text(f"{type(exc).__name__}: {exc}"),
                "uncertain": False,
                "recorded_at": _now(),
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", type=Path, required=True)
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    asyncio.run(launch(args.bridge_dir.resolve(), recover=args.recover))


if __name__ == "__main__":
    main()
