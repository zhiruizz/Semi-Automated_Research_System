from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import mimetypes
import os
from pathlib import Path
import shutil
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from research_controller.db.models import Artifact, TaskArtifact
from research_controller.domain.enums import ArtifactIntegrityStatus
from research_controller.services.event_log import EventLog


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStore:
    def __init__(self, workspace_root: Path | str, event_log: EventLog | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.events = event_log or EventLog()

    def ingest_file(
        self,
        session: Session,
        *,
        project_id: str,
        task_id: str | None,
        source: Path | str,
        logical_name: str,
        kind: str,
        producer_type: str,
        producer_ref_id: str,
        evidence_eligible: bool = False,
        schema_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> Artifact:
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        digest = sha256_file(source_path)
        existing = session.scalar(
            select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.producer_type == producer_type,
                Artifact.producer_ref_id == producer_ref_id,
                Artifact.logical_name == logical_name,
                Artifact.sha256 == digest,
            )
        )
        if existing is not None:
            return existing

        destination = (
            self.workspace_root
            / project_id
            / "artifacts"
            / "sha256"
            / digest[:2]
            / digest
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_name(f".{digest}.{os.getpid()}.tmp")
            with source_path.open("rb") as source_handle, temporary.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if sha256_file(temporary) != digest:
                temporary.unlink(missing_ok=True)
                raise IOError("artifact copy hash mismatch")
            try:
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            destination.chmod(0o444)
        elif sha256_file(destination) != digest:
            raise IOError(f"content-addressed artifact corrupted: {destination}")

        current_version = session.scalar(
            select(func.max(Artifact.version)).where(
                Artifact.project_id == project_id, Artifact.logical_name == logical_name
            )
        )
        artifact = Artifact(
            project_id=project_id,
            task_id=task_id,
            kind=kind,
            logical_name=logical_name,
            version=(current_version or 0) + 1,
            uri=str(destination),
            mime_type=mimetypes.guess_type(source_path.name)[0] or "application/octet-stream",
            size_bytes=destination.stat().st_size,
            sha256=digest,
            producer_type=producer_type,
            producer_ref_id=producer_ref_id,
            integrity_status=ArtifactIntegrityStatus.VERIFIED,
            schema_name=schema_name,
            evidence_eligible=evidence_eligible,
            metadata_json=metadata or {},
            verified_at=datetime.now(timezone.utc),
        )
        session.add(artifact)
        session.flush()
        if task_id is not None:
            session.add(TaskArtifact(task_id=task_id, artifact_id=artifact.id, role="output"))
        self.events.append(
            session,
            project_id=project_id,
            event_type="ARTIFACT_CREATED",
            entity_type="ARTIFACT",
            entity_id=artifact.id,
            correlation_id=correlation_id,
            dedupe_key=(
                f"artifact-created:{producer_type}:{producer_ref_id}:{logical_name}:{digest}"
            ),
            old_state=None,
            new_state=ArtifactIntegrityStatus.VERIFIED.value,
            payload={"logical_name": logical_name, "sha256": digest, "version": artifact.version},
        )
        self.events.append(
            session,
            project_id=project_id,
            event_type="ARTIFACT_VERIFIED",
            entity_type="ARTIFACT",
            entity_id=artifact.id,
            correlation_id=correlation_id,
            dedupe_key=f"artifact-verified:{artifact.id}",
            old_state=ArtifactIntegrityStatus.PENDING.value,
            new_state=ArtifactIntegrityStatus.VERIFIED.value,
        )
        return artifact
