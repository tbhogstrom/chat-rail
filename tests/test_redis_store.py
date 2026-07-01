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


def test_list_active_sessions(store):
    store.store_call("s-1", {"sessionId": "s-1", "status": "Answered",
                              "from": {}, "to": {"extensionId": "119"}})
    store.store_call("s-2", {"sessionId": "s-2", "status": "Answered",
                              "from": {}, "to": {"extensionId": "120"}})
    active = store.list_active_sessions()
    assert set(active) == {"s-1", "s-2"}


def test_set_and_get_extracted(store):
    data = {"firstname": "Sebastian", "lastname": None, "email": None}
    store.set_extracted("s-1", data)
    got = store.get_extracted("s-1")
    assert got == data


def test_get_extracted_missing_returns_none(store):
    assert store.get_extracted("s-does-not-exist") is None


def test_complete_call_keeps_session_extractable_during_grace(store, fake_redis):
    store.store_call("s-end", {"sessionId": "s-end", "status": "Answered"})
    store.complete_call("s-end", grace=60)

    # Regular "active" view no longer includes it — dashboard still sees Disconnected
    assert not fake_redis.sismember("calls:active", "s-end")
    # But the extraction worker should still process it while within grace
    assert "s-end" in store.list_active_sessions()


def test_list_active_sessions_drops_recently_ended_after_grace_expires(store, fake_redis):
    store.store_call("s-stale", {"sessionId": "s-stale", "status": "Answered"})
    store.complete_call("s-stale", grace=60)
    # Simulate grace expiry by deleting the marker key
    fake_redis.delete("call:s-stale:extract-grace")

    assert "s-stale" not in store.list_active_sessions()
    # Should also cleanly remove from the recently-ended set as cleanup
    assert not fake_redis.sismember("calls:recently-ended", "s-stale")


def test_list_active_sessions_unions_active_plus_recent(store):
    store.store_call("s-live", {"sessionId": "s-live", "status": "Answered"})
    store.store_call("s-just-ended", {"sessionId": "s-just-ended", "status": "Answered"})
    store.complete_call("s-just-ended", grace=60)

    assert set(store.list_active_sessions()) == {"s-live", "s-just-ended"}


def test_set_and_get_rep_roster(store):
    roster = {"119": {"name": "Doug Stoker", "number": "119"}}
    store.set_rep_roster(roster)
    assert store.get_rep_roster() == roster


def test_get_rep_roster_empty_returns_empty_dict(store):
    assert store.get_rep_roster() == {}


def test_set_and_get_rep_metrics(store):
    m = {"119": {"inboundToday": 1, "inboundWeek": 2,
                 "outboundToday": 3, "outboundWeek": 4}}
    store.set_rep_metrics(m)
    assert store.get_rep_metrics() == m


def test_get_rep_metrics_empty_returns_empty_dict(store):
    assert store.get_rep_metrics() == {}


def test_set_and_get_recent_calls(store):
    calls = [{"sessionId": "s1", "repName": "Doug", "connected": True}]
    store.set_recent_calls(calls)
    assert store.get_recent_calls() == calls


def test_get_recent_calls_empty_returns_empty_list(store):
    assert store.get_recent_calls() == []
