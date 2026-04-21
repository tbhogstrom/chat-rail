import pytest, respx, httpx
from src.sidecar_client import SidecarClient

@pytest.mark.asyncio
@respx.mock
async def test_start_supervision_posts_expected_payload():
    route = respx.post("http://bridge.local/sessions").mock(
        return_value=httpx.Response(202, json={"sessionId": "s-1", "status": "supervising"})
    )
    client = SidecarClient("http://bridge.local", api_key="k")
    await client.start_supervision("s-1", "101")
    assert route.called
    assert route.calls.last.request.headers["x-bridge-key"] == "k"

@pytest.mark.asyncio
@respx.mock
async def test_stop_supervision_deletes():
    route = respx.delete("http://bridge.local/sessions/s-1").mock(
        return_value=httpx.Response(204)
    )
    client = SidecarClient("http://bridge.local", api_key="k")
    await client.stop_supervision("s-1")
    assert route.called
