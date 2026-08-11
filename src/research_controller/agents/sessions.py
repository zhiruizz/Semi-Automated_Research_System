from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from research_controller.db.models import AgentRun
from research_controller.domain.enums import AgentRunStatus
from research_controller.domain.ids import new_id
from research_controller.protocols.agent import SessionPolicy


class SessionManager:
    def select(
        self,
        session: Session,
        *,
        project_id: str,
        role: str,
        backend: str,
        policy: SessionPolicy,
        model_tier: str,
    ) -> str | None:
        if policy is SessionPolicy.RESUME_ROLE:
            candidates = session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.project_id == project_id,
                    AgentRun.role == role,
                    AgentRun.backend == backend,
                    AgentRun.status == AgentRunStatus.SUCCEEDED,
                    AgentRun.session_id.is_not(None),
                    AgentRun.mode != SessionPolicy.EPHEMERAL.value,
                )
                .order_by(AgentRun.finished_at.desc(), AgentRun.id.desc())
            ).all()
            for prior in candidates:
                if (
                    prior.session_id
                    and prior.config_json.get("model_tier") == model_tier
                ):
                    return prior.session_id
        if backend != "mock":
            # Hermes creates and returns the authoritative session identity.
            return None
        prefix = "mock-ephemeral-session" if policy is SessionPolicy.EPHEMERAL else "mock-session"
        return new_id(prefix)
