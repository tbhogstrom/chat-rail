import pytest
from src.redis_store import CallStore
from src.call_monitor import process_telephony_event


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


def make_event(session_id, status, from_info=None, to_info=None, parties=None):
    event = {
        "body": {
            "telephonySessionId": session_id,
            "parties": parties or [
                {
                    "direction": "Inbound",
                    "status": {"code": status},
                    "from": from_info or {"phoneNumber": "+12065551234", "name": "John Doe"},
                    "to": to_info or {"phoneNumber": "+12065559999", "extensionId": "119", "name": "Doug Stoker"},
                }
            ],
        }
    }
    return event


def test_answered_call_stored(store):
    event = make_event("s-100", "Answered")
    process_telephony_event(event, store)

    call = store.get_call("s-100")
    assert call is not None
    assert call["status"] == "Answered"
    assert call["from"]["phoneNumber"] == "+12065551234"


def test_answered_call_added_to_active(store, fake_redis):
    event = make_event("s-100", "Answered")
    process_telephony_event(event, store)

    assert fake_redis.sismember("calls:active", "s-100")


def test_disconnected_call_completed(store, fake_redis):
    process_telephony_event(make_event("s-100", "Answered"), store)
    process_telephony_event(make_event("s-100", "Disconnected"), store)

    assert not fake_redis.sismember("calls:active", "s-100")
    call = store.get_call("s-100")
    assert call["status"] == "Disconnected"


def test_proceeding_call_stored_as_ringing(store):
    event = make_event("s-100", "Proceeding")
    process_telephony_event(event, store)

    call = store.get_call("s-100")
    assert call is not None
    assert call["status"] == "Proceeding"


def test_rep_pointer_set_on_answered(store, fake_redis):
    event = make_event("s-100", "Answered",
                       to_info={"extensionId": "119", "name": "Doug Stoker"})
    process_telephony_event(event, store)

    assert fake_redis.get("rep:119:current") == "s-100"


def test_multiple_calls_tracked(store):
    process_telephony_event(make_event("s-100", "Answered"), store)
    process_telephony_event(make_event("s-200", "Answered",
                                       to_info={"extensionId": "118", "name": "Jacob Hair"}), store)

    active = store.list_active_calls()
    assert len(active) == 2
