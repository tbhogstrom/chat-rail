import json
import pytest
from src.redis_store import CallStore


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


def test_store_call_saves_state(store, fake_redis):
    call_data = {
        "sessionId": "s-abc123",
        "direction": "Inbound",
        "status": "Answered",
        "from": {"phoneNumber": "+12065551234", "name": "John Doe"},
        "to": {"extensionId": "119", "name": "Doug Stoker"},
        "startTime": "2026-04-16T10:00:00Z",
    }
    store.store_call("s-abc123", call_data)

    raw = fake_redis.get("call:s-abc123:state")
    assert raw is not None
    saved = json.loads(raw)
    assert saved["sessionId"] == "s-abc123"
    assert saved["direction"] == "Inbound"


def test_store_call_adds_to_active_set(store, fake_redis):
    store.store_call("s-abc123", {"sessionId": "s-abc123", "status": "Answered"})
    assert fake_redis.sismember("calls:active", "s-abc123")


def test_store_call_sets_rep_pointer(store, fake_redis):
    call_data = {
        "sessionId": "s-abc123",
        "status": "Answered",
        "to": {"extensionId": "119"},
    }
    store.store_call("s-abc123", call_data)
    assert fake_redis.get("rep:119:current") == "s-abc123"


def test_get_call_returns_stored_data(store):
    call_data = {"sessionId": "s-abc123", "status": "Answered"}
    store.store_call("s-abc123", call_data)

    result = store.get_call("s-abc123")
    assert result["sessionId"] == "s-abc123"


def test_get_call_returns_none_for_missing(store):
    assert store.get_call("nonexistent") is None


def test_list_active_calls(store):
    store.store_call("s-1", {"sessionId": "s-1", "status": "Answered"})
    store.store_call("s-2", {"sessionId": "s-2", "status": "Answered"})

    active = store.list_active_calls()
    assert len(active) == 2
    session_ids = {c["sessionId"] for c in active}
    assert session_ids == {"s-1", "s-2"}


def test_complete_call_removes_from_active(store, fake_redis):
    store.store_call("s-abc123", {"sessionId": "s-abc123", "status": "Answered"})
    store.complete_call("s-abc123")

    assert not fake_redis.sismember("calls:active", "s-abc123")


def test_complete_call_sets_status_disconnected(store):
    store.store_call("s-abc123", {"sessionId": "s-abc123", "status": "Answered"})
    store.complete_call("s-abc123")

    call = store.get_call("s-abc123")
    assert call["status"] == "Disconnected"


def test_get_rep_current_call(store):
    call_data = {
        "sessionId": "s-abc123",
        "status": "Answered",
        "to": {"extensionId": "119", "name": "Doug Stoker"},
    }
    store.store_call("s-abc123", call_data)

    result = store.get_rep_current_call("119")
    assert result["sessionId"] == "s-abc123"


def test_get_rep_current_call_returns_none(store):
    assert store.get_rep_current_call("999") is None


def test_store_and_get_transcript(store):
    store.store_transcript("s-abc123", "Hello, this is Doug from SFW Construction.")
    result = store.get_transcript("s-abc123")
    assert result == "Hello, this is Doug from SFW Construction."


def test_get_transcript_returns_none_for_missing(store):
    assert store.get_transcript("nonexistent") is None
