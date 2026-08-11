import pytest
from pydantic import ValidationError

from research_controller.protocols.compute import ComputeTaskSpec


def test_compute_spec_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ComputeTaskSpec.model_validate(
            {
                "schema_version": "compute-task-spec/v0.1",
                "project_id": "p",
                "task_id": "t",
                "submission_key": "k",
                "execution": {"command": ["true"], "surprise": True},
                "resources": {},
                "unknown": "not allowed",
            }
        )
