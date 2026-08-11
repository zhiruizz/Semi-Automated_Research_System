from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from research_controller.artifacts.store import ArtifactStore, sha256_file
from research_controller.db.models import Artifact
from tests.conftest import create_compute_task


def test_artifact_hash_and_versioned_immutability(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = create_compute_task(factory, tmp_path)
    store = ArtifactStore(workspace)
    first_source = tmp_path / "first.txt"
    second_source = tmp_path / "second.txt"
    first_source.write_text("first", encoding="utf-8")
    second_source.write_text("second", encoding="utf-8")
    with factory.begin() as session:
        first = store.ingest_file(
            session,
            project_id=project.id,
            task_id=task.id,
            source=first_source,
            logical_name="result",
            kind="TEST",
            producer_type="TEST",
            producer_ref_id="run-1",
        )
        first_id, first_uri = first.id, first.uri
    with factory.begin() as session:
        second = store.ingest_file(
            session,
            project_id=project.id,
            task_id=task.id,
            source=second_source,
            logical_name="result",
            kind="TEST",
            producer_type="TEST",
            producer_ref_id="run-2",
        )
        assert second.version == 2
        assert second.uri != first_uri
    assert Path(first_uri).read_text(encoding="utf-8") == "first"
    assert sha256_file(Path(first_uri)) == sha256_file(first_source)
    with pytest.raises(ValueError, match="Artifact is immutable"):
        with factory.begin() as session:
            artifact = session.get(Artifact, first_id)
            artifact.uri = "/tmp/overwrite"
            session.flush()


def test_artifact_ingestion_is_idempotent(runtime, tmp_path):
    _engine, factory, workspace = runtime
    project, task = create_compute_task(factory, tmp_path)
    store = ArtifactStore(workspace)
    source = tmp_path / "value.txt"
    source.write_text("same", encoding="utf-8")
    with factory.begin() as session:
        first = store.ingest_file(
            session,
            project_id=project.id,
            task_id=task.id,
            source=source,
            logical_name="same",
            kind="TEST",
            producer_type="TEST",
            producer_ref_id="one",
        )
        first_id = first.id
    with factory.begin() as session:
        second = store.ingest_file(
            session,
            project_id=project.id,
            task_id=task.id,
            source=source,
            logical_name="same",
            kind="TEST",
            producer_type="TEST",
            producer_ref_id="one",
        )
        assert second.id == first_id
