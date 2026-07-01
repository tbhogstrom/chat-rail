import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.redis_store import CallStore
from src.call_monitor import (
    process_telephony_event,
    _fetch_active_session_events,
    _load_ext_display_map,
)


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


def test_snapshot_shape_with_id_key_stored(store):
    """REST GET /telephony/sessions/{sid} returns the session id as `id`,
    not `telephonySessionId`. The hydration path must accept it."""
    event = {
        "body": {
            "id": "s-snap",
            "parties": [
                {
                    "direction": "Inbound",
                    "status": {"code": "Answered"},
                    "from": {"phoneNumber": "+12065551234", "name": "John Doe"},
                    "to": {"phoneNumber": "+12065559999", "extensionId": "119", "name": "Doug Stoker"},
                }
            ],
        }
    }
    process_telephony_event(event, store)

    call = store.get_call("s-snap")
    assert call is not None
    assert call["status"] == "Answered"


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


@pytest.mark.asyncio
async def test_queue_routed_call_triggers_supervision_on_any_party(store):
    """Queue/IVR calls have the rep in parties[1+], not parties[0]."""
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    event = {
        "body": {
            "telephonySessionId": "s-queue",
            "parties": [
                {
                    "status": {"code": "Answered"},
                    "direction": "Inbound",
                    "to": {"extensionId": "302988053", "name": "Sales Queue"},
                    "from": {"phoneNumber": "+15551234567"},
                },
                {
                    "status": {"code": "Answered"},
                    "direction": "Inbound",
                    "to": {"extensionId": "576959052", "extensionNumber": "119",
                           "name": "Doug Stoker"},
                    "from": {"phoneNumber": "+15551234567"},
                },
            ],
        }
    }
    process_telephony_event(event, store, sidecar=sidecar,
                            monitored_extensions=["576959052"])
    await asyncio.sleep(0)
    sidecar.start_supervision.assert_awaited_once_with("s-queue", "119")


# ---------------------------------------------------------------- snapshot hydration
def _mock_platform(routes: dict):
    """Build a MagicMock platform whose .get(url) returns a mock with .json_dict()
    keyed by exact-suffix URL match. A route value may also be an Exception
    instance — in which case .get() raises it.
    """
    platform = MagicMock()

    def get(url, params=None):
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                resp = MagicMock()
                resp.json_dict.return_value = payload
                return resp
        raise AssertionError(f"unexpected URL: {url}")

    platform.get.side_effect = get
    return platform


def test_fetch_active_session_events_empty_when_no_active_calls():
    platform = _mock_platform({"/active-calls": {"records": []}})
    assert _fetch_active_session_events(platform) == []


def test_fetch_active_session_events_returns_ws_shaped_events():
    platform = _mock_platform({
        "/active-calls": {"records": [
            {"telephonySessionId": "s-1"},
            {"telephonySessionId": "s-2"},
        ]},
        "/telephony/sessions/s-1": {
            "telephonySessionId": "s-1",
            "parties": [{"status": {"code": "Answered"}, "direction": "Inbound",
                         "to": {"extensionId": "119"},
                         "from": {"phoneNumber": "+15551111111"}}],
        },
        "/telephony/sessions/s-2": {
            "telephonySessionId": "s-2",
            "parties": [{"status": {"code": "Hold"}, "direction": "Outbound",
                         "to": {"phoneNumber": "+15552222222"},
                         "from": {"extensionId": "120"}}],
        },
    })
    events = _fetch_active_session_events(platform)
    assert len(events) == 2
    assert events[0]["body"]["telephonySessionId"] == "s-1"
    assert events[0]["body"]["parties"][0]["status"]["code"] == "Answered"
    assert events[1]["body"]["telephonySessionId"] == "s-2"


def test_fetch_active_session_events_skips_records_without_session_id():
    platform = _mock_platform({"/active-calls": {"records": [{"id": "no-sid"}]}})
    assert _fetch_active_session_events(platform) == []


def test_fetch_active_session_events_continues_when_one_session_fetch_fails():
    platform = _mock_platform({
        "/active-calls": {"records": [
            {"telephonySessionId": "s-bad"},
            {"telephonySessionId": "s-good"},
        ]},
        "/telephony/sessions/s-bad": RuntimeError("boom"),
        "/telephony/sessions/s-good": {
            "telephonySessionId": "s-good",
            "parties": [{"status": {"code": "Answered"},
                         "to": {"extensionId": "119"}, "from": {}}],
        },
    })
    events = _fetch_active_session_events(platform)
    assert len(events) == 1
    assert events[0]["body"]["telephonySessionId"] == "s-good"


def test_fetch_active_session_events_returns_empty_when_api_fails():
    platform = _mock_platform({"/active-calls": RuntimeError("RC down")})
    assert _fetch_active_session_events(platform) == []


@pytest.mark.asyncio
async def test_snapshot_hydration_triggers_supervision_for_monitored_rep(store):
    """End-to-end: pull snapshot, feed through process_telephony_event,
    confirm the store is hydrated and supervision fires for a monitored rep."""
    platform = _mock_platform({
        "/active-calls": {"records": [{"telephonySessionId": "s-live"}]},
        "/telephony/sessions/s-live": {
            "telephonySessionId": "s-live",
            "parties": [{
                "status": {"code": "Answered"},
                "direction": "Inbound",
                "to": {"extensionId": "576959052", "extensionNumber": "119",
                       "name": "Doug Stoker"},
                "from": {"phoneNumber": "+15551234567"},
            }],
        },
    })
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()

    for ev in _fetch_active_session_events(platform):
        process_telephony_event(ev, store, sidecar=sidecar,
                                monitored_extensions=["576959052"])
    await asyncio.sleep(0)

    assert store.get_call("s-live")["status"] == "Answered"
    sidecar.start_supervision.assert_awaited_once_with("s-live", "119")


@pytest.mark.asyncio
async def test_ext_number_map_resolves_missing_extensionnumber(store):
    """Party records often omit extensionNumber — fall back to the map."""
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    event = {
        "body": {
            "telephonySessionId": "s-map",
            "parties": [
                {
                    "status": {"code": "Answered"},
                    "direction": "Inbound",
                    "to": {"extensionId": "576959052", "name": "Doug Stoker"},
                    "from": {"phoneNumber": "+15551234567"},
                },
            ],
        }
    }
    process_telephony_event(event, store, sidecar=sidecar,
                            monitored_extensions=["576959052"],
                            ext_number_map={"576959052": "119"})
    await asyncio.sleep(0)
    sidecar.start_supervision.assert_awaited_once_with("s-map", "119")


# ---------------------------------------------------------------- rep_first_name
def test_rep_first_name_set_from_party_to_name(store):
    """Inbound call: party.to.name='Doug Stoker' → rep_first_name='Doug'."""
    event = make_event("s-rep", "Answered",
                       to_info={"extensionId": "576959052", "name": "Doug Stoker"})
    process_telephony_event(event, store, monitored_extensions=["576959052"])
    call = store.get_call("s-rep")
    assert call["rep_first_name"] == "Doug"


def test_rep_first_name_falls_back_to_display_map(store):
    """Outbound call where the party carries no name field — resolve from map."""
    event = {
        "body": {
            "telephonySessionId": "s-outbound",
            "parties": [{
                "status": {"code": "Answered"},
                "direction": "Outbound",
                "from": {"extensionId": "576959052"},
                "to": {"phoneNumber": "+14036160460"},
            }],
        }
    }
    process_telephony_event(event, store,
                            monitored_extensions=["576959052"],
                            ext_display_map={"576959052": "Doug Stoker"})
    call = store.get_call("s-outbound")
    assert call["rep_first_name"] == "Doug"


def test_rep_first_name_none_when_no_monitored_match(store):
    event = make_event("s-other", "Answered",
                       to_info={"extensionId": "999", "name": "Someone Else"})
    process_telephony_event(event, store, monitored_extensions=["576959052"])
    call = store.get_call("s-other")
    assert call.get("rep_first_name") is None


def test_load_ext_display_map_returns_id_to_name():
    platform = MagicMock()
    resp = MagicMock()
    resp.json_dict.return_value = {
        "records": [
            {"id": 576959052, "name": "Doug Stoker", "extensionNumber": "119"},
            {"id": 442845052, "name": "Jacob Hair",  "extensionNumber": "120"},
            {"id": 1, "extensionNumber": "000"},  # no name — skipped
        ]
    }
    platform.get.return_value = resp
    assert _load_ext_display_map(platform) == {
        "576959052": "Doug Stoker",
        "442845052": "Jacob Hair",
    }


def test_load_ext_display_map_returns_empty_on_api_failure():
    platform = MagicMock()
    platform.get.side_effect = RuntimeError("RC down")
    assert _load_ext_display_map(platform) == {}


# ---------------------------------------------------------------- multi-party rep pointers
def test_multi_party_sets_rep_pointer_for_every_party(store, fake_redis):
    """Queue-routed inbound: primary party is the queue, rep appears in parties[1].
    Both extensions should map to this session so any dashboard URL finds it."""
    event = {
        "body": {
            "telephonySessionId": "s-queue",
            "parties": [
                {
                    "status": {"code": "Answered"},
                    "direction": "Inbound",
                    "to": {"extensionId": "302988053", "name": "Sales Queue"},
                    "from": {"phoneNumber": "+15551234567"},
                },
                {
                    "status": {"code": "Answered"},
                    "direction": "Inbound",
                    "to": {"extensionId": "576959052", "name": "Doug Stoker"},
                    "from": {"phoneNumber": "+15551234567"},
                },
            ],
        }
    }
    process_telephony_event(event, store, monitored_extensions=["576959052"])
    assert fake_redis.get("rep:302988053:current") == "s-queue"
    assert fake_redis.get("rep:576959052:current") == "s-queue"


def test_inbound_to_phonenumber_still_points_the_rep(store, fake_redis):
    """Inbound where primary party carries only to.phoneNumber='119' (no
    extensionId). A second party with the rep's extensionId should still set
    rep:576959052:current."""
    event = {
        "body": {
            "telephonySessionId": "s-119",
            "parties": [
                {
                    "status": {"code": "Proceeding"},
                    "direction": "Inbound",
                    "to": {"phoneNumber": "119"},
                    "from": {"phoneNumber": "+15039059040", "extensionId": "313690053"},
                },
                {
                    "status": {"code": "Proceeding"},
                    "direction": "Inbound",
                    "to": {"extensionId": "576959052"},
                    "from": {"phoneNumber": "+15039059040"},
                },
            ],
        }
    }
    process_telephony_event(event, store, monitored_extensions=["576959052"])
    assert fake_redis.get("rep:576959052:current") == "s-119"
