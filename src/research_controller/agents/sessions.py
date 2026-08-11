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
    ) -> str:
        if policy is SessionPolicy.RESUME_ROLE:
            prior = session.scalar(
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
            )
            if prior is not None and prior.session_id:
                return prior.session_id
        prefix = "mock-ephemeral-session" if policy is SessionPolicy.EPHEMERAL else "mock-session"
        return new_id(prefix)
