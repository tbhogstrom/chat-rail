import pytest
import respx
import httpx
from unittest.mock import patch
from fastapi.testclient import TestClient

from src.api.main import create_app

API_KEY = "test-key-123"


def auth():
    return {"x-api-key": API_KEY}


@pytest.fixture
def client():
    return TestClient(create_app(store=None))


WELL_FORMED = (
    "HEADER:\nACTION REQUIRED | SFW Construction Service Agreement | Jane Doe | 2026-Jun\n\n"
    "DEAL DESCRIPTION:\nRepair the active roof leak.\n\n"
    "SCOPE:\nSFW will inspect the roof and repair the leak.\n\n"
    "EMAIL:\nThank you. Thank you for choosing SFW Construction."
)


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.agreement_tool.generator.Config.ANTHROPIC_API_KEY", "sk-test")
@patch("src.agreement_tool.generator.Config.ANTHROPIC_MODEL", "claude-sonnet-4-6")
@respx.mock
def test_generate_endpoint_returns_sections(client):
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": WELL_FORMED}]})
    )
    resp = client.post("/api/agreement/generate", headers=auth(), json={
        "customer_name": "Jane Doe", "notes": "roof leak", "active_leak": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"].startswith("SFW will inspect")
    assert data["partial"] is False


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.agreement_tool.generator.Config.ANTHROPIC_API_KEY", "sk-test")
@respx.mock
def test_generate_endpoint_502_on_anthropic_error(client):
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(500, text="boom")
    )
    resp = client.post("/api/agreement/generate", headers=auth(), json={
        "customer_name": "X", "notes": "y",
    })
    assert resp.status_code == 502


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_generate_endpoint_requires_auth(client):
    resp = client.post("/api/agreement/generate", json={"customer_name": "X", "notes": "y"})
    assert resp.status_code == 401


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.agreement_tool.generator.Config.ANTHROPIC_API_KEY", "sk-test")
@respx.mock
def test_sow_summary_endpoint_returns_summary(client):
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": "Find and address second-story window leak."}],
        })
    )
    resp = client.post("/api/agreement/sow-summary", headers=auth(), json={
        "notes": "Caller says the upstairs window leaks when it rains.",
    })
    assert resp.status_code == 200
    assert resp.json() == {"summary": "Find and address second-story window leak."}


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_sow_summary_endpoint_requires_auth(client):
    resp = client.post("/api/agreement/sow-summary", json={"notes": "x"})
    assert resp.status_code == 401


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.agreement_tool.routes.Config.CALLRAIL_API_KEY", "cr-key")
@patch("src.agreement_tool.routes.Config.CALLRAIL_ACCOUNT_ID", "ACC123")
@respx.mock
def test_callrail_transcript_endpoint(client):
    respx.get("https://api.callrail.com/v3/a/ACC123/calls/CAL1.json").mock(
        return_value=httpx.Response(200, json={
            "id": "CAL1", "customer_name": "Jane", "customer_phone_number": "+1503",
            "source": "Google", "transcription": "leaky roof",
        })
    )
    resp = client.post("/api/callrail/transcript", headers=auth(), json={"call_id": "CAL1"})
    assert resp.status_code == 200
    assert resp.json()["transcript"] == "leaky roof"
