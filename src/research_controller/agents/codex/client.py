from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
import subprocess
from typing import Any

from research_controller.agents.codex.models import (
    CodexConfig,
    CodexHealth,
    CodexRpcError,
    CodexStatus,
)


ServerRequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
NotificationHandler = Callable[[dict[str, Any]], Awaitable[None]]


class CodexAppServerClient:
    """Minimal bidirectional JSONL client for one Codex App Server process."""

    def __init__(
        self,
        config: CodexConfig,
        *,
        cwd: Path,
        server_request_handler: ServerRequestHandler | None = None,
        notification_handler: NotificationHandler | None = None,
    ) -> None:
        self.config = config
        self.cwd = cwd.resolve()
        self.server_request_handler = server_request_handler
        self.notification_handler = notification_handler
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self.stderr_tail: list[str] = []

    async def connect(self) -> None:
        if self.process is not None:
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.config.app_server_command,
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            raise CodexRpcError("CODEX_APP_SERVER_UNAVAILABLE", str(exc)) from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "semi_automated_research_system",
                    "title": "Semi-Automated Research System",
                    "version": "0.1.0",
                }
            },
        )
        await self.notify("initialized", {})
        # Yield once after the acknowledgement so stdio peers can consume the
        # notification before the first ordinary request. This also avoids
        # relying on transport buffering details in small protocol fakes.
        await asyncio.sleep(0.01)

    async def _send(self, value: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise CodexRpcError("CODEX_TRANSPORT_CLOSED", "App Server stdin is unavailable")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            try:
                self.process.stdin.write(encoded.encode())
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise CodexRpcError("CODEX_TRANSPORT_CLOSED", str(exc), uncertain=True) from exc

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"method": method, "id": request_id, "params": params})
            return await asyncio.wait_for(
                future, timeout=timeout or self.config.request_timeout_sec
            )
        except asyncio.TimeoutError as exc:
            raise CodexRpcError(
                "CODEX_RPC_TIMEOUT", f"{method} timed out", uncertain=True
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise CodexRpcError(
                        "CODEX_TRANSPORT_CLOSED",
                        "App Server stdout closed",
                        uncertain=True,
                    )
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message.get("error") or {}
                        text = str(error.get("message", "Codex RPC failed"))
                        lowered = text.lower()
                        code = error.get("code")
                        if "thread" in lowered and "not found" in lowered:
                            kind = "CODEX_THREAD_NOT_FOUND"
                        elif code == 429 or "rate limit" in lowered or "too many requests" in lowered:
                            kind = "CODEX_RATE_LIMITED"
                        elif code in {401, 403} or "unauthorized" in lowered or "authentication" in lowered:
                            kind = "CODEX_AUTH_REQUIRED"
                        elif "invalid_json_schema" in lowered or "invalid schema" in lowered:
                            kind = "CODEX_INVALID_OUTPUT_SCHEMA"
                        else:
                            kind = "CODEX_RPC_ERROR"
                        future.set_exception(
                            CodexRpcError(kind, text, data=error.get("data"))
                        )
                    else:
                        future.set_result(message.get("result"))
                elif "id" in message and "method" in message:
                    asyncio.create_task(self._handle_server_request(message))
                elif "method" in message and self.notification_handler is not None:
                    asyncio.create_task(self.notification_handler(message))
        except BaseException as exc:
            for future in list(self._pending.values()):
                if not future.done():
                    if isinstance(exc, CodexRpcError):
                        future.set_exception(exc)
                    else:
                        future.set_exception(
                            CodexRpcError("CODEX_TRANSPORT_CLOSED", str(exc), uncertain=True)
                        )

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        if self.server_request_handler is None:
            result = {"decision": "decline"}
        else:
            try:
                result = await self.server_request_handler(message)
            except Exception:
                result = {"decision": "decline"}
        try:
            await self._send({"id": message["id"], "result": result})
        except CodexRpcError:
            return

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            self.stderr_tail.append(line.decode(errors="replace")[:1000])
            del self.stderr_tail[:-20]

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self.process = None

    async def __aenter__(self) -> CodexAppServerClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def cli_version(command: list[str]) -> str | None:
    executable = command[0]
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip() or completed.stderr.strip()
    return value[:200] or None


async def probe_codex(config: CodexConfig, cwd: Path) -> CodexStatus:
    version = cli_version(config.app_server_command)
    client = CodexAppServerClient(config, cwd=cwd)
    try:
        await client.connect()
        account = await client.request("account/read", {"refreshToken": False})
        listing = await client.request(
            "model/list", {"limit": 100, "includeHidden": False}
        )
        raw_models = listing.get("data", []) if isinstance(listing, dict) else []
        models = [
            {
                "id": item.get("id"),
                "model": item.get("model"),
                "is_default": bool(item.get("isDefault")),
                "default_effort": item.get("defaultReasoningEffort"),
                "supported_efforts": [
                    effort.get("reasoningEffort")
                    for effort in item.get("supportedReasoningEfforts", [])
                ],
            }
            for item in raw_models
            if isinstance(item, dict) and not item.get("hidden", False)
        ]
        default = next((item for item in models if item["is_default"]), None)
        account_value = account.get("account") if isinstance(account, dict) else None
        auth_type = account_value.get("type") if isinstance(account_value, dict) else None
        requires = account.get("requiresOpenaiAuth") if isinstance(account, dict) else None
        health = CodexHealth.HEALTHY if account_value is not None else CodexHealth.AUTH_REQUIRED
        return CodexStatus(
            health=health,
            cli_version=version,
            auth_type=auth_type,
            requires_openai_auth=requires,
            default_model=default.get("model") if default else None,
            default_effort=default.get("default_effort") if default else None,
            models=models,
        )
    except CodexRpcError as exc:
        return CodexStatus(
            health=CodexHealth.UNAVAILABLE,
            cli_version=version,
            error_type=exc.error_type,
            message=str(exc)[:1000],
        )
    finally:
        await client.close()
