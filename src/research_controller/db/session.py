from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from research_controller.db.base import Base
from research_controller.db import models as _models  # noqa: F401  # register mappings
from research_controller.db.models import AgentRun, ComputeJob, Event, Task


def create_sqlite_engine(database_path: Path | str, *, echo: bool = False) -> Engine:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", echo=echo, future=True)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def initialize_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)


@event.listens_for(Session, "before_flush")
def enforce_audited_state_changes(
    session: Session, _flush_context: object, _instances: object
) -> None:
    """Reject status mutations that do not have a matching new audit event."""
    pending_events = [item for item in session.new if isinstance(item, Event)]
    for entity, entity_type, attribute in (
        *((item, "TASK", "status") for item in session.dirty if isinstance(item, Task)),
        *((item, "COMPUTE_JOB", "execution_status") for item in session.dirty if isinstance(item, ComputeJob)),
        *((item, "AGENT_RUN", "status") for item in session.dirty if isinstance(item, AgentRun)),
    ):
        history = inspect(entity).attrs[attribute].history
        if not history.has_changes():
            continue
        new_value = getattr(entity, attribute).value
        if not any(
            event_row.entity_type == entity_type
            and event_row.entity_id == entity.id
            and event_row.new_state == new_value
            for event_row in pending_events
        ):
            raise ValueError(
                f"{entity_type}.{attribute} must be changed through TransitionService"
            )


@event.listens_for(Event, "before_update")
@event.listens_for(Event, "before_delete")
def prevent_event_mutation(_mapper: object, _connection: object, _target: Event) -> None:
    raise ValueError("Event rows are append-only")
