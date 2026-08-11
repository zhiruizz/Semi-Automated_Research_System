from __future__ import annotations

import json

import pytest

from research_controller.agents.base import AgentAdapterError
from research_controller.agents.hermes.adapter import HermesAdapter
from research_controller.agents.hermes.models import HermesConfig


@pytest.mark.asyncio
async def test_abandoned_bridge_claim_is_uncertain_and_never_relaunched(tmp_path):
    adapter = HermesAdapter(tmp_path, HermesConfig(base_url="http://127.0.0.1:1"))
    bridge = adapter.bridge_dir("arun-dead")
    bridge.mkdir(parents=True)
    (bridge / "request.json").write_text(
        json.dumps(
            {
                "run_key": "arun-dead",
                "session_id": None,
                "workdir": str(tmp_path / "run"),
                "config": HermesConfig(base_url="http://127.0.0.1:1").model_dump(
                    mode="json"
                ),
                "payload": {"input": "tiny"},
            }
        ),
        encoding="utf-8",
    )
    (bridge / "launch.claim").write_text(
        json.dumps({"pid": 999_999_999}), encoding="utf-8"
    )
    with pytest.raises(AgentAdapterError) as uncertain:
        await adapter.reconcile("arun-dead")
    assert uncertain.value.error_type == "START_STATE_UNCERTAIN"
    assert uncertain.value.block_task is True
    assert not (bridge / "response.json").exists()
