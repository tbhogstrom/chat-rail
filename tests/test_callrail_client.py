import pytest
import respx
import httpx

from src.agreement_tool.callrail import CallRailClient, CallRailError

ACCOUNT = "ACC123"
BASE = "https://api.callrail.com/v3"


def _client():
    return CallRailClient(api_key="cr-key", account_id=ACCOUNT)


@pytest.mark.asyncio
@respx.mock
async def test_get_call_transcript_maps_fields():
    url = f"{BASE}/a/{ACCOUNT}/calls/CAL1.json"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "id": "CAL1",
        "customer_name": "Jane Doe",
        "customer_phone_number": "+15035551212",
        "source": "Google Ads",
        "transcription": {"sentences": [
            {"speaker": "Agent", "text": "Thanks for calling SFW."},
            {"speaker": "Customer", "text": "My roof is leaking."},
        ]},
    }))
    out = await _client().get_call_transcript("CAL1")
    assert out["customer_name"] == "Jane Doe"
    assert out["customer_phone_number"] == "+15035551212"
    assert out["source"] == "Google Ads"
    assert "Agent: Thanks for calling SFW." in out["transcript"]
    assert "Customer: My roof is leaking." in out["transcript"]
    # auth header
    assert respx.calls.last.request.headers["authorization"] == "Token token=cr-key"


@pytest.mark.asyncio
@respx.mock
async def test_get_call_transcript_handles_plain_string_transcription():
    url = f"{BASE}/a/{ACCOUNT}/calls/CAL2.json"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "id": "CAL2", "customer_name": "Bob", "customer_phone_number": "+15035550000",
        "source": None, "transcription": "Plain transcript text.",
    }))
    out = await _client().get_call_transcript("CAL2")
    assert out["transcript"] == "Plain transcript text."


@pytest.mark.asyncio
@respx.mock
async def test_get_recent_calls_returns_summaries():
    url = f"{BASE}/a/{ACCOUNT}/calls.json"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "calls": [
            {"id": "CAL1", "customer_name": "Jane", "customer_phone_number": "+1503", "source": "Google", "start_time": "2026-06-03T10:00:00Z"},
        ]
    }))
    out = await _client().get_recent_calls(limit=5)
    assert out[0]["id"] == "CAL1"
    assert out[0]["customer_name"] == "Jane"


@pytest.mark.asyncio
@respx.mock
async def test_callrail_error_on_non_2xx():
    url = f"{BASE}/a/{ACCOUNT}/calls/BAD.json"
    respx.get(url).mock(return_value=httpx.Response(404, text="not found"))
    with pytest.raises(CallRailError, match="404"):
        await _client().get_call_transcript("BAD")
