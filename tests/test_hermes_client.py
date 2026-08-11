from __future__ import annotations

import pytest
import socket

from research_controller.agents.hermes.client import HermesApiClient, HermesApiError
from research_controller.agents.hermes.models import HermesConfig, map_run_status
from research_controller.domain.enums import AgentRunStatus
from tests.fake_hermes import FakeHermesApiServer


@pytest.mark.asyncio
async def test_hermes_client_capabilities_models_run_and_approval():
    with FakeHermesApiServer() as fake:
        client = HermesApiClient(HermesConfig(base_url=fake.base_url), api_key=fake.token)
        assert (await client.capabilities()).run_approval is True
        assert (await client.model_options())["default"] == "fake-model"
        started = await client.start_run({"input": "tiny"})
        assert started.run_id == "run_fake_1"
        assert (await client.get_run(started.run_id)).status == "completed"
        await client.respond_approval(started.run_id, "once")
        assert fake.approvals == ["once"]
        await client.stop_run(started.run_id)
        assert fake.stop_count == 1


@pytest.mark.asyncio
async def test_hermes_client_auth_and_not_found_are_classified():
    with FakeHermesApiServer() as fake:
        bad = HermesApiClient(HermesConfig(base_url=fake.base_url), api_key="wrong")
        with pytest.raises(HermesApiError, match="authentication") as auth:
            await bad.capabilities()
        assert auth.value.error_type == "HERMES_AUTH_REQUIRED"
        good = HermesApiClient(HermesConfig(base_url=fake.base_url), api_key=fake.token)
        with pytest.raises(HermesApiError) as missing:
            await good.get_run("missing")
        assert missing.value.error_type == "REMOTE_RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_hermes_403_429_500_timeout_and_refused_are_classified():
    with FakeHermesApiServer() as fake:
        client = HermesApiClient(HermesConfig(base_url=fake.base_url), api_key=fake.token)
        fake.forbidden = True
        with pytest.raises(HermesApiError) as forbidden:
            await client.model_options()
        assert forbidden.value.error_type == "HERMES_AUTH_REQUIRED"
        fake.forbidden = False
        for status, expected in [(429, "HERMES_RATE_LIMITED"), (500, "HERMES_SERVER_ERROR")]:
            fake.error_status = status
            fake.error_message = f"forced error accidentally containing {fake.token}"
            with pytest.raises(HermesApiError) as error:
                await client.model_options()
            assert error.value.error_type == expected
            assert fake.token not in str(error.value)
        fake.error_status = None
        fake.delay_sec = 0.1
        timeout_client = HermesApiClient(
            HermesConfig(base_url=fake.base_url, request_timeout_sec=0.01),
            api_key=fake.token,
        )
        with pytest.raises(HermesApiError) as timeout:
            await timeout_client.model_options()
        assert timeout.value.error_type == "HERMES_TIMEOUT"

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    refused = HermesApiClient(
        HermesConfig(base_url=f"http://127.0.0.1:{port}", connect_timeout_sec=0.1),
        api_key="fake",
    )
    with pytest.raises(HermesApiError) as unavailable:
        await refused.capabilities()
    assert unavailable.value.error_type == "HERMES_UNAVAILABLE"


def test_hermes_status_mapping_is_central_and_unknown_is_nonterminal():
    assert map_run_status("waiting_for_approval").status is AgentRunStatus.WAITING_APPROVAL
    assert map_run_status("stopping").terminal is False
    unknown = map_run_status("future-state")
    assert unknown.status is AgentRunStatus.RUNNING
    assert unknown.known is False
    assert unknown.error_type == "UNKNOWN_REMOTE_STATUS"
    assert map_run_status("failed").terminal is True
    assert map_run_status("cancelled").status is AgentRunStatus.CANCELLED
