from __future__ import annotations

from enum import StrEnum
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
import yaml

from research_controller.domain.enums import AgentRunStatus


class HermesModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ModelTier(HermesModel):
    provider: str | None = None
    model: str | None = None


class HermesConfig(HermesModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8642"
    api_key_env: str = "SARS_HERMES_API_KEY"
    connect_timeout_sec: float = Field(default=5, gt=0)
    request_timeout_sec: float = Field(default=30, gt=0)
    capability_cache_ttl_sec: float = Field(default=60, gt=0)
    poll_interval_sec: float = Field(default=1, gt=0)
    model_tiers: dict[str, ModelTier] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str | None = None) -> HermesConfig:
        selected = Path(path) if path else Path(__file__).resolve().parents[4] / "config" / "hermes.yaml"
        value: dict[str, Any] = {}
        if selected.is_file():
            with selected.open("r", encoding="utf-8") as handle:
                value = yaml.safe_load(handle) or {}
        if os.environ.get("SARS_HERMES_BASE_URL"):
            value["base_url"] = os.environ["SARS_HERMES_BASE_URL"]
        return cls.model_validate(value)

    def api_key(self) -> str:
        value = os.environ.get(self.api_key_env, "")
        if not value:
            raise HermesConfigurationError(
                "HERMES_AUTH_REQUIRED",
                f"Hermes bearer key environment variable {self.api_key_env} is not set",
            )
        return value

    def redacted(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class HermesConfigurationError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


class HermesHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    AUTH_REQUIRED = "auth_required"
    INCOMPATIBLE = "incompatible"


class HermesCapabilities(HermesModel):
    run_submission: bool = False
    run_status: bool = False
    run_events_sse: bool = False
    run_stop: bool = False
    run_approval: bool = False
    sessions: bool = False
    session_fork: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> HermesCapabilities:
        features = payload.get("features", payload)

        def enabled(name: str) -> bool:
            value = features.get(name, False) if isinstance(features, dict) else False
            if isinstance(value, dict):
                return bool(value.get("enabled", True))
            return bool(value)

        return cls(
            run_submission=enabled("run_submission"),
            run_status=enabled("run_status"),
            run_events_sse=enabled("run_events_sse") or enabled("run_events"),
            run_stop=enabled("run_stop"),
            run_approval=enabled("run_approval") or enabled("run_approval_response"),
            sessions=enabled("session_resources") or enabled("sessions"),
            session_fork=enabled("session_fork"),
            raw=payload,
        )


class HermesRun(HermesModel):
    run_id: str
    status: str
    session_id: str | None = None
    output: str | None = None
    error: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    last_event: str | None = None


class MappedRunStatus(HermesModel):
    status: AgentRunStatus
    known: bool = True
    terminal: bool = False
    error_type: str | None = None


def map_run_status(raw_status: str) -> MappedRunStatus:
    value = raw_status.strip().lower()
    if value in {"queued", "started", "running", "stopping"}:
        return MappedRunStatus(status=AgentRunStatus.RUNNING)
    if value in {"waiting_for_approval", "waiting approval", "waiting_approval"}:
        return MappedRunStatus(status=AgentRunStatus.WAITING_APPROVAL)
    if value == "completed":
        return MappedRunStatus(status=AgentRunStatus.SUCCEEDED, terminal=True)
    if value == "failed":
        return MappedRunStatus(status=AgentRunStatus.FAILED, terminal=True)
    if value == "cancelled":
        return MappedRunStatus(status=AgentRunStatus.CANCELLED, terminal=True)
    return MappedRunStatus(
        status=AgentRunStatus.RUNNING,
        known=False,
        error_type="UNKNOWN_REMOTE_STATUS",
    )
