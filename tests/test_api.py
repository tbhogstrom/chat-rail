import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from src.redis_store import CallStore
from src.api.main import create_app
import respx
import httpx


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


# -- Phase 3 dashboard endpoints -------------------------------------------


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_extracted_happy(client, store):
    store.set_extracted("s-1", {"firstname": "Seb", "email": None})
    r = client.get("/api/calls/s-1/extracted", headers=auth_header())
    assert r.status_code == 200
    assert r.json() == {"firstname": "Seb", "email": None}


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_extracted_missing_returns_404(client):
    r = client.get("/api/calls/s-missing/extracted", headers=auth_header())
    assert r.status_code == 404


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_post_scope_summary(client, store):
    store.store_transcript("s-1", "caller wants electrical work at 123 Oak")
    with patch("src.api.routes.summarize_scope",
               AsyncMock(return_value="Customer wants electrical work.")):
        r = client.post("/api/calls/s-1/scope-summary", headers=auth_header())
    assert r.status_code == 200
    assert r.json() == {"summary": "Customer wants electrical work."}


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_post_scope_summary_no_transcript_returns_404(client):
    r = client.post("/api/calls/s-nope/scope-summary", headers=auth_header())
    assert r.status_code == 404


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_post_hubspot_lookup_match(client):
    fake = AsyncMock(return_value={"id": "42", "properties": {}})
    with patch("src.api.routes._hs_client") as hs:
        hs.return_value.lookup_contact = fake
        r = client.post("/api/hubspot/contacts/lookup",
                        json={"email": "a@b.com"},
                        headers=auth_header())
    assert r.status_code == 200
    assert r.json() == {"id": "42", "properties": {}}


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_post_hubspot_lookup_no_match_returns_404(client):
    fake = AsyncMock(return_value=None)
    with patch("src.api.routes._hs_client") as hs:
        hs.return_value.lookup_contact = fake
        r = client.post("/api/hubspot/contacts/lookup",
                        json={"email": "nope@nope.com"},
                        headers=auth_header())
    assert r.status_code == 404


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.HUBSPOT_PORTAL_ID", "")
def test_post_hubspot_contacts_upsert(client):
    fake = AsyncMock(return_value={"id": "42"})
    with patch("src.api.routes._hs_client") as hs:
        hs.return_value.upsert_contact = fake
        r = client.post("/api/hubspot/contacts",
                        json={"email": "a@b.com", "firstname": "A"},
                        headers=auth_header())
    assert r.status_code == 200
    assert r.json() == {"contactId": "42", "url": None}


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.HUBSPOT_PORTAL_ID", "")
def test_post_hubspot_deal(client):
    fake = AsyncMock(return_value={"id": "d-7"})
    with patch("src.api.routes._hs_client") as hs:
        hs.return_value.create_deal = fake
        r = client.post("/api/hubspot/deals",
                        json={"contactId": "42", "dealname": "Test",
                              "description": "scope here"},
                        headers=auth_header())
    assert r.status_code == 200
    assert r.json() == {"dealId": "d-7", "url": None}


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.HUBSPOT_PRIVATE_APP_TOKEN", "pat-test")
@patch("src.api.routes.Config.HUBSPOT_PORTAL_ID", "8210108")
@patch("src.api.routes.Config.HUBSPOT_SCOPE_PROPERTY", "scope_of_work")
@respx.mock
def test_create_deal_passes_scope_and_returns_url(client):
    respx.post("https://api.hubapi.com/crm/v3/objects/deals").mock(
        return_value=httpx.Response(201, json={"id": "D9"})
    )
    # clear the lru_cache so the patched token is used
    from src.api.routes import _hs_client
    _hs_client.cache_clear()
    resp = client.post("/api/hubspot/deals", headers=auth_header(), json={
        "contactId": "42", "dealname": "ACTION", "description": "Repair.",
        "scope": "SFW will inspect.",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["dealId"] == "D9"
    assert data["url"] == "https://app.hubspot.com/contacts/8210108/record/0-3/D9"
