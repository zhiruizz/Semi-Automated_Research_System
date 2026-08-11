from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

import httpx
from pydantic import ValidationError

from research_controller.agents.hermes.models import (
    HermesCapabilities,
    HermesConfig,
    HermesHealth,
    HermesRun,
    HermesConfigurationError,
)


class HermesApiError(RuntimeError):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        status_code: int | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message[:1000])
        self.error_type = error_type
        self.status_code = status_code
        self.uncertain = uncertain


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "Hermes API error")[:1000]
    except (ValueError, TypeError):
        pass
    return f"Hermes API returned HTTP {response.status_code}"


def redact_text(value: object, secret: str | None = None) -> str:
    text = str(value)[:1000]
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    return text


class HermesApiClient:
    def __init__(self, config: HermesConfig, *, api_key: str | None = None) -> None:
        self.config = config
        self._api_key = api_key
        self._cached_capabilities: tuple[float, HermesCapabilities] | None = None

    def _headers(self) -> dict[str, str]:
        key = self._api_key or self.config.api_key()
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    def _client(self) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            self.config.request_timeout_sec,
            connect=self.config.connect_timeout_sec,
        )
        return httpx.AsyncClient(
            base_url=self.config.base_url.rstrip("/"),
            headers=self._headers(),
            timeout=timeout,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        start_request: bool = False,
    ) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.request(method, path, json=json_body)
        except HermesConfigurationError as exc:
            raise HermesApiError(exc.error_type, str(exc)) from exc
        except httpx.TransportError as exc:
            kind = "HERMES_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "HERMES_UNAVAILABLE"
            if start_request:
                kind = "START_STATE_UNCERTAIN"
            raise HermesApiError(
                kind,
                f"{type(exc).__name__} while calling Hermes {method} {path}",
                uncertain=start_request,
            ) from exc
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                kind = "HERMES_AUTH_REQUIRED"
            elif response.status_code == 404 and path.startswith("/v1/runs/"):
                kind = "REMOTE_RUN_NOT_FOUND"
            elif response.status_code == 429:
                kind = "HERMES_RATE_LIMITED"
            elif response.status_code >= 500:
                kind = "HERMES_SERVER_ERROR"
            else:
                kind = "HERMES_REQUEST_REJECTED"
            secret = self._api_key or os.environ.get(self.config.api_key_env)
            raise HermesApiError(
                kind,
                redact_text(_error_message(response), secret),
                status_code=response.status_code,
                uncertain=False,
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise HermesApiError("HERMES_INVALID_RESPONSE", "Hermes returned non-JSON data") from exc
        if not isinstance(value, dict):
            raise HermesApiError("HERMES_INVALID_RESPONSE", "Hermes response must be a JSON object")
        return value

    async def probe(self) -> HermesHealth:
        try:
            value = await self._request("GET", "/health/detailed")
        except HermesApiError as exc:
            if exc.error_type == "HERMES_AUTH_REQUIRED":
                return HermesHealth.AUTH_REQUIRED
            if exc.error_type == "HERMES_UNAVAILABLE":
                return HermesHealth.UNAVAILABLE
            return HermesHealth.DEGRADED
        status = str(value.get("status", "")).lower()
        return HermesHealth.HEALTHY if status in {"ok", "healthy", "ready"} else HermesHealth.DEGRADED

    async def capabilities(self, *, force: bool = False) -> HermesCapabilities:
        now = time.monotonic()
        if (
            not force
            and self._cached_capabilities is not None
            and now - self._cached_capabilities[0] < self.config.capability_cache_ttl_sec
        ):
            return self._cached_capabilities[1]
        payload = await self._request("GET", "/v1/capabilities")
        value = HermesCapabilities.from_payload(payload)
        self._cached_capabilities = (now, value)
        return value

    async def model_options(self) -> dict[str, Any]:
        return await self._request("GET", "/api/model/options")

    async def start_run(self, payload: dict[str, Any]) -> HermesRun:
        value = await self._request("POST", "/v1/runs", json_body=payload, start_request=True)
        try:
            return HermesRun.model_validate(value)
        except ValidationError as exc:
            raise HermesApiError(
                "HERMES_INVALID_RESPONSE", "Hermes start response did not match the Runs protocol"
            ) from exc

    async def get_run(self, run_id: str) -> HermesRun:
        value = await self._request("GET", f"/v1/runs/{run_id}")
        value.setdefault("run_id", run_id)
        try:
            return HermesRun.model_validate(value)
        except ValidationError as exc:
            raise HermesApiError(
                "HERMES_INVALID_RESPONSE", "Hermes run status did not match the Runs protocol"
            ) from exc

    async def stop_run(self, run_id: str) -> HermesRun:
        value = await self._request("POST", f"/v1/runs/{run_id}/stop", json_body={})
        value.setdefault("run_id", run_id)
        try:
            return HermesRun.model_validate(value)
        except ValidationError as exc:
            raise HermesApiError(
                "HERMES_INVALID_RESPONSE", "Hermes stop response did not match the Runs protocol"
            ) from exc

    async def respond_approval(self, run_id: str, choice: str) -> dict[str, Any]:
        if choice not in {"once", "deny"}:
            raise ValueError("Controller only permits approval choices 'once' and 'deny'")
        return await self._request(
            "POST", f"/v1/runs/{run_id}/approval", json_body={"choice": choice}
        )

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/sessions/{session_id}")

    async def fork_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/api/sessions/{session_id}/fork", json_body={})
