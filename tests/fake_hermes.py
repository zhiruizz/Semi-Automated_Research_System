from __future__ import annotations

from contextlib import AbstractContextManager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
import time
from typing import Any


class FakeHermesApiServer(AbstractContextManager["FakeHermesApiServer"]):
    def __init__(self, *, token: str = "fake-secret") -> None:
        self.token = token
        self.post_count = 0
        self.approvals: list[str] = []
        self.stop_count = 0
        self.runs: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.next_status = "completed"
        self.invalid_result = False
        self.mismatch_result = False
        self.drop_start_response = False
        self.unknown_status = False
        self.forbidden = False
        self.error_status: int | None = None
        self.error_message = "forced error"
        self.delay_sec = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args: object) -> None:
                return

            def _json(self, status: int, value: dict[str, Any]) -> None:
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _authorized(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self._json(401, {"error": {"message": "authentication required"}})
                    return False
                if outer.forbidden:
                    self._json(403, {"error": {"message": "forbidden"}})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if outer.delay_sec:
                    time.sleep(outer.delay_sec)
                if outer.error_status:
                    self._json(outer.error_status, {"error": {"message": outer.error_message}})
                    return
                if self.path == "/health/detailed":
                    self._json(200, {"status": "ok"})
                elif self.path == "/v1/capabilities":
                    self._json(
                        200,
                        {
                            "features": {
                                "run_submission": True,
                                "run_status": True,
                                "run_stop": True,
                                "run_approval_response": True,
                                "session_resources": True,
                                "session_fork": True,
                            }
                        },
                    )
                elif self.path == "/api/model/options":
                    self._json(200, {"models": ["fake-model"], "default": "fake-model"})
                elif self.path.startswith("/api/sessions/"):
                    self._json(200, {"id": self.path.rsplit("/", 1)[-1]})
                elif self.path.startswith("/v1/runs/"):
                    run_id = self.path.rsplit("/", 1)[-1]
                    run = outer.runs.get(run_id)
                    if run is None:
                        self._json(404, {"error": {"code": "run_not_found"}})
                    else:
                        value = dict(run)
                        if outer.unknown_status:
                            value["status"] = "future_state"
                        self._json(200, value)
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if outer.delay_sec:
                    time.sleep(outer.delay_sec)
                if outer.error_status:
                    self._json(outer.error_status, {"error": {"message": outer.error_message}})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/v1/runs":
                    outer.post_count += 1
                    outer.requests.append(body)
                    run_id = f"run_fake_{outer.post_count}"
                    prompt = str(body.get("input", ""))
                    task_match = re.search(r'"task_id":\s*"([A-Za-z0-9_.:-]+)"', prompt)
                    output_match = re.search(r"exact final AgentResult JSON to: (.+)", prompt)
                    task_id = task_match.group(1) if task_match else "unknown"
                    result = {
                        "schema_version": "agent-result/v0.1",
                        "task_id": task_id,
                        "outcome": "completed",
                        "summary": "fake Hermes completed",
                        "artifacts": [],
                    }
                    file_value = dict(result)
                    if outer.mismatch_result:
                        file_value["summary"] = "mismatch"
                    if output_match:
                        path = Path(output_match.group(1).strip())
                        path.parent.mkdir(parents=True, exist_ok=True)
                        deliverable = path.parent / "implementation_summary.txt"
                        deliverable.write_text("fake implementation summary\n", encoding="utf-8")
                        result["artifacts"] = [
                            {
                                "logical_name": "implementation_summary",
                                "path": str(deliverable),
                                "artifact_kind": "RESULT_SUMMARY",
                            }
                        ]
                        file_value = dict(result)
                        if outer.mismatch_result:
                            file_value["summary"] = "mismatch"
                        path.write_text(json.dumps(file_value), encoding="utf-8")
                    output = (
                        "not-json" if outer.invalid_result else
                        "<<<SARS_AGENT_RESULT_V1>>>\n"
                        + json.dumps(result)
                        + "\n<<<END_SARS_AGENT_RESULT_V1>>>"
                    )
                    outer.runs[run_id] = {
                        "run_id": run_id,
                        "status": outer.next_status,
                        "session_id": body.get("session_id") or f"session-{run_id}",
                        "output": output,
                        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                    }
                    if outer.drop_start_response:
                        self.connection.shutdown(2)
                        self.connection.close()
                        return
                    self._json(202, {"run_id": run_id, "status": "started"})
                elif self.path.endswith("/stop"):
                    run_id = self.path.split("/")[-2]
                    outer.stop_count += 1
                    if run_id not in outer.runs:
                        self._json(404, {"error": {"code": "run_not_found"}})
                        return
                    outer.runs[run_id]["status"] = "cancelled"
                    self._json(200, {"run_id": run_id, "status": "stopping"})
                elif self.path.endswith("/approval"):
                    run_id = self.path.split("/")[-2]
                    choice = str(body.get("choice"))
                    outer.approvals.append(choice)
                    outer.runs[run_id]["status"] = "running" if choice == "once" else "failed"
                    self._json(200, {"run_id": run_id, "choice": choice, "resolved": 1})
                elif self.path.endswith("/fork"):
                    self._json(200, {"id": "forked-session"})
                else:
                    self._json(404, {"error": {"message": "not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeHermesApiServer:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
