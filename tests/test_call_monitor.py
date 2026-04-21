import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

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


@pytest.mark.asyncio
async def test_answered_triggers_sidecar_when_monitored(store):
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    event = make_event("s-200", "Answered",
                       to_info={"extensionId": "119", "extensionNumber": "101",
                                "name": "Doug Stoker"})
    process_telephony_event(event, store, sidecar=sidecar, monitored_extensions=["119"])
    await asyncio.sleep(0)
    sidecar.start_supervision.assert_awaited_once_with("s-200", "101")


@pytest.mark.asyncio
async def test_answered_skipped_when_not_monitored(store):
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    event = make_event("s-200", "Answered",
                       to_info={"extensionId": "999", "extensionNumber": "999"})
    process_telephony_event(event, store, sidecar=sidecar, monitored_extensions=["119"])
    await asyncio.sleep(0)
    sidecar.start_supervision.assert_not_called()


@pytest.mark.asyncio
async def test_disconnected_stops_sidecar(store):
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    sidecar.stop_supervision = AsyncMock()
    process_telephony_event(make_event("s-200", "Answered",
                                       to_info={"extensionId": "119",
                                                "extensionNumber": "101"}),
                            store, sidecar=sidecar, monitored_extensions=["119"])
    await asyncio.sleep(0)
    process_telephony_event(make_event("s-200", "Disconnected",
                                       to_info={"extensionId": "119",
                                                "extensionNumber": "101"}),
                            store, sidecar=sidecar, monitored_extensions=["119"])
    await asyncio.sleep(0)
    sidecar.stop_supervision.assert_awaited_once_with("s-200")
