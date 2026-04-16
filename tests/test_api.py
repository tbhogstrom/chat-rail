import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
from src.redis_store import CallStore
from src.api.main import create_app


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


@pytest.fixture
def client(store):
    app = create_app(store)
    return TestClient(app)


API_KEY = "test-key-123"


def auth_header():
    return {"x-api-key": API_KEY}


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_missing_api_key_returns_401(client):
    resp = client.get("/api/calls/active")
    assert resp.status_code == 401


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_wrong_api_key_returns_401(client):
    resp = client.get("/api/calls/active", headers={"x-api-key": "wrong"})
    assert resp.status_code == 401


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_valid_api_key_passes(client, store):
    resp = client.get("/api/calls/active", headers=auth_header())
    assert resp.status_code == 200


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_active_calls_empty(client):
    resp = client.get("/api/calls/active", headers=auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data["calls"] == []
    assert data["count"] == 0


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_active_calls_with_data(client, store):
    store.store_call("s-1", {"sessionId": "s-1", "status": "Answered", "to": {"extensionId": "119"}})
    store.store_call("s-2", {"sessionId": "s-2", "status": "Answered", "to": {"extensionId": "118"}})

    resp = client.get("/api/calls/active", headers=auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_call_context(client, store):
    store.store_call("s-abc", {"sessionId": "s-abc", "status": "Answered"})
    store.store_transcript("s-abc", "Hello from SFW.")

    resp = client.get("/api/calls/s-abc/context", headers=auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data["call"]["sessionId"] == "s-abc"
    assert data["transcript"] == "Hello from SFW."


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_call_context_not_found(client):
    resp = client.get("/api/calls/nonexistent/context", headers=auth_header())
    assert resp.status_code == 404


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_transcript(client, store):
    store.store_call("s-abc", {"sessionId": "s-abc", "status": "Answered"})
    store.store_transcript("s-abc", "Hello from SFW.")

    resp = client.get("/api/calls/s-abc/transcript", headers=auth_header())
    assert resp.status_code == 200
    assert resp.json()["transcript"] == "Hello from SFW."


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_latest_call_by_rep(client, store):
    store.store_call("s-abc", {
        "sessionId": "s-abc",
        "status": "Answered",
        "to": {"extensionId": "119", "name": "Doug Stoker"},
    })

    resp = client.get("/api/calls/latest?rep=119", headers=auth_header())
    assert resp.status_code == 200
    assert resp.json()["call"]["sessionId"] == "s-abc"


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_latest_call_rep_not_found(client):
    resp = client.get("/api/calls/latest?rep=999", headers=auth_header())
    assert resp.status_code == 404
