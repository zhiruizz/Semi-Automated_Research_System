from __future__ import annotations

from enum import StrEnum
import os
from pathlib import Path
import shlex
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
import yaml

from research_controller.domain.enums import AgentRunStatus


class CodexModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodexModelTier(CodexModel):
    model: str | None = None
    effort: str | None = None


class CodexConfig(CodexModel):
    enabled: bool = True
    app_server_command: list[str] = Field(default_factory=lambda: ["codex", "app-server"])
    poll_interval_sec: float = Field(default=0.1, gt=0)
    request_timeout_sec: float = Field(default=30, gt=0)
    approval_poll_interval_sec: float = Field(default=0.1, gt=0)
    max_trace_events: int = Field(default=200, ge=10, le=2000)
    network_default: bool = False
    model_tiers: dict[str, CodexModelTier] = Field(default_factory=dict)

    @field_validator("app_server_command")
    @classmethod
    def command_is_nonempty(cls, value: list[str]) -> list[str]:
        if not value or any(not item for item in value):
            raise ValueError("app_server_command must contain nonempty arguments")
        return value

    @classmethod
    def load(cls, path: Path | str | None = None) -> CodexConfig:
        selected = Path(path) if path else Path(__file__).resolve().parents[4] / "config" / "codex.yaml"
        value: dict[str, Any] = {}
        if selected.is_file():
            with selected.open("r", encoding="utf-8") as handle:
                value = yaml.safe_load(handle) or {}
        # A fake command is useful for process-crash tests, but accepting an arbitrary
        # command from ordinary task routing would be an unsafe code-execution surface.
        if os.environ.get("SARS_CODEX_TEST_MODE") == "1" and os.environ.get(
            "SARS_CODEX_APP_SERVER_COMMAND"
        ):
            value["app_server_command"] = shlex.split(
                os.environ["SARS_CODEX_APP_SERVER_COMMAND"]
            )
        return cls.model_validate(value)


class CodexHealth(StrEnum):
    HEALTHY = "healthy"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"
    INCOMPATIBLE = "incompatible"


class CodexStatus(CodexModel):
    health: CodexHealth
    cli_version: str | None = None
    auth_type: str | None = None
    requires_openai_auth: bool | None = None
    default_model: str | None = None
    default_effort: str | None = None
    models: list[dict[str, Any]] = Field(default_factory=list)
    error_type: str | None = None
    message: str | None = None


class MappedTurnStatus(CodexModel):
    status: AgentRunStatus
    known: bool = True
    terminal: bool = False
    error_type: str | None = None


def map_turn_status(raw_status: str) -> MappedTurnStatus:
    if raw_status == "inProgress":
        return MappedTurnStatus(status=AgentRunStatus.RUNNING)
    if raw_status == "completed":
        return MappedTurnStatus(status=AgentRunStatus.SUCCEEDED, terminal=True)
    if raw_status == "failed":
        return MappedTurnStatus(status=AgentRunStatus.FAILED, terminal=True)
    if raw_status == "interrupted":
        return MappedTurnStatus(status=AgentRunStatus.CANCELLED, terminal=True)
    return MappedTurnStatus(
        status=AgentRunStatus.RUNNING,
        known=False,
        error_type="UNKNOWN_CODEX_TURN_STATUS",
    )


class CodexRpcError(RuntimeError):
    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        uncertain: bool = False,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.uncertain = uncertain
        self.data = data
