from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from research_controller.db.models import Event, Project
from research_controller.domain.ids import new_id


class EventLog:
    """Append-only, per-project sequenced audit log.

    Callers own the transaction. Mutating a truth row and calling ``append`` in
    the same transaction makes the state/event pair atomic.
    """

    def append(
        self,
        session: Session,
        *,
        project_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        actor_type: str = "CONTROLLER",
        actor_id: str | None = None,
        correlation_id: str | None = None,
        causation_event_id: str | None = None,
        dedupe_key: str | None = None,
        old_state: str | None = None,
        new_state: str | None = None,
        severity: str = "INFO",
        payload: dict[str, Any] | None = None,
    ) -> Event:
        # The caller may already have mutated a guarded state field. Do not let
        # loading Project trigger an autoflush before the matching Event exists.
        with session.no_autoflush:
            project = session.get(Project, project_id)
        if project is None:
            raise LookupError(f"project not found: {project_id}")
        project.event_seq += 1
        project.lock_version += 1
        event = Event(
            project_id=project_id,
            seq=project.event_seq,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id or new_id("corr"),
            causation_event_id=causation_event_id,
            dedupe_key=dedupe_key,
            old_state=old_state,
            new_state=new_state,
            severity=severity,
            payload_json=payload or {},
        )
        session.add(event)
        return event
