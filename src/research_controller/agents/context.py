from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from research_controller.artifacts.store import sha256_file
from research_controller.db.models import Artifact
from research_controller.domain.enums import ArtifactIntegrityStatus
from research_controller.protocols.agent import (
    AgentTaskSpec,
    ContextItem,
    ContextMode,
    ContextPack,
)


class ContextMissingError(ValueError):
    pass


class ContextBuilder:
    def build(self, session: Session, spec: AgentTaskSpec) -> ContextPack:
        items: list[ContextItem] = []
        for reference in spec.context:
            if reference.mode is ContextMode.OMIT:
                continue
            artifact = session.get(Artifact, reference.artifact_id)
            valid = (
                artifact is not None
                and artifact.project_id == spec.project_id
                and artifact.integrity_status is ArtifactIntegrityStatus.VERIFIED
                and Path(artifact.uri).is_file()
                and sha256_file(Path(artifact.uri)) == artifact.sha256
            )
            if not valid:
                if reference.required:
                    raise ContextMissingError(
                        f"required context unavailable: {reference.artifact_id}"
                    )
                continue
            content: str | None = None
            if reference.mode is ContextMode.FULL:
                try:
                    content = Path(artifact.uri).read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    content = None
            items.append(
                ContextItem(
                    artifact_id=artifact.id,
                    purpose=reference.purpose,
                    mode=reference.mode,
                    logical_name=artifact.logical_name,
                    artifact_kind=artifact.kind,
                    uri=artifact.uri,
                    sha256=artifact.sha256,
                    content=content,
                    metadata=artifact.metadata_json,
                )
            )
        return ContextPack(project_id=spec.project_id, task_id=spec.task_id, items=items)
