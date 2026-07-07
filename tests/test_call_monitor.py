import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.redis_store import CallStore
from src.call_monitor import (
    process_telephony_event,
    _fetch_active_call_sids,
    _fetch_active_session_events,
    _load_ext_display_map,
    _reconcile_active_sessions,
    build_monitored_roster,
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
    assert _fetch_active_session_events(platform) == ([], set())


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
    events, _sids = _fetch_active_session_events(platform)
    assert len(events) == 2
    assert events[0]["body"]["telephonySessionId"] == "s-1"
    assert events[0]["body"]["parties"][0]["status"]["code"] == "Answered"
    assert events[1]["body"]["telephonySessionId"] == "s-2"


def test_fetch_active_session_events_skips_records_without_session_id():
    platform = _mock_platform({"/active-calls": {"records": [{"id": "no-sid"}]}})
    assert _fetch_active_session_events(platform) == ([], set())


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
    events, _sids = _fetch_active_session_events(platform)
    assert len(events) == 1
    assert events[0]["body"]["telephonySessionId"] == "s-good"


def test_fetch_active_session_events_returns_none_when_api_fails():
    """None (not empty) — a failed snapshot must skip the reconcile, or it
    would complete every live call in Redis."""
    platform = _mock_platform({"/active-calls": RuntimeError("RC down")})
    assert _fetch_active_session_events(platform) is None


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

    events, _sids = _fetch_active_session_events(platform)
    for ev in events:
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


def test_build_monitored_roster_filters_to_monitored():
    display = {"119": "Doug Stoker", "121": "Travis Watters", "200": "IVR"}
    numbers = {"119": "119", "121": "121", "200": "200"}
    roster = build_monitored_roster(display, numbers, ["119", "121"])
    assert roster == {
        "119": {"name": "Doug Stoker", "number": "119"},
        "121": {"name": "Travis Watters", "number": "121"},
    }


def test_build_monitored_roster_handles_missing_maps():
    roster = build_monitored_roster({}, {}, ["119"])
    assert roster == {"119": {"name": None, "number": None}}


def test_active_ext_ids_records_only_connected_parties(store):
    """A simulring session: 119 answered, 121 is only ringing (Proceeding).
    Only the connected rep's extension lands in activeExtIds."""
    event = {"body": {"telephonySessionId": "s-ring", "parties": [
        {"status": {"code": "Answered"}, "direction": "Inbound",
         "from": {"phoneNumber": "+12065551234"},
         "to": {"extensionId": "119"}},
        {"status": {"code": "Proceeding"}, "to": {"extensionId": "121"}},
    ]}}
    process_telephony_event(event, store)

    call = store.get_call("s-ring")
    assert call["activeExtIds"] == ["119"]
    # Both reps' pointers still resolve to the session (dashboard findability),
    # but only the answered rep is recorded as connected.
    rung = store.get_rep_current_call("121")
    assert rung is not None and rung["sessionId"] == "s-ring"


# ---------------------------------------------------------------- multi-leg end detection
def test_multileg_session_stays_active_while_any_leg_live(store, fake_redis):
    """A stale leg going Gone in parties[0] must not end the session while
    the rep is still talking. (Prod: overview flapped reps to idle mid-call.)"""
    parties = [
        {"status": {"code": "Gone"}, "direction": "Outbound",
         "extensionId": "999", "to": {}, "from": {}},
        {"status": {"code": "Answered"}, "direction": "Outbound",
         "extensionId": "576959052",
         "to": {"extensionNumber": "119"}, "from": {}},
    ]
    process_telephony_event(make_event("s-m", "Answered", parties=parties),
                            store, monitored_extensions=["576959052"])
    assert fake_redis.sismember("calls:active", "s-m")
    call = store.get_call("s-m")
    assert call["status"] == "Answered"      # rep leg preferred over parties[0]
    assert call["activeExtIds"] == ["576959052"]
    assert call.get("lastEventAt")           # reconcile age guard needs this


def test_session_completes_only_when_all_legs_ended(store, fake_redis):
    process_telephony_event(make_event("s-m", "Answered"), store)
    parties = [
        {"status": {"code": "Gone"}, "extensionId": "999", "to": {}, "from": {}},
        {"status": {"code": "Disconnected"}, "extensionId": "119", "to": {}, "from": {}},
    ]
    process_telephony_event(make_event("s-m", "Answered", parties=parties), store)
    assert not fake_redis.sismember("calls:active", "s-m")
    assert store.get_call("s-m")["status"] == "Disconnected"


# ---------------------------------------------------------------- ring-proof pointers
def test_ringing_session_does_not_steal_pointer_from_live_call(store, fake_redis):
    """A queue ring must not repoint a rep who is connected on another call —
    it flipped the overview to idle and switched the rep's live dashboard
    away from the in-progress transcript."""
    live = [{"status": {"code": "Answered"}, "direction": "Outbound",
             "extensionId": "576959052",
             "to": {"extensionNumber": "119"}, "from": {}}]
    process_telephony_event(make_event("s-live", "Answered", parties=live), store)
    assert fake_redis.get("rep:576959052:current") == "s-live"

    ring = [{"status": {"code": "Proceeding"}, "direction": "Inbound",
             "extensionId": "576959052", "to": {}, "from": {}}]
    process_telephony_event(make_event("s-ring", "Proceeding", parties=ring), store)
    assert fake_redis.get("rep:576959052:current") == "s-live"


def test_answering_a_new_call_does_take_pointer(store, fake_redis):
    live = [{"status": {"code": "Answered"}, "direction": "Outbound",
             "extensionId": "576959052",
             "to": {"extensionNumber": "119"}, "from": {}}]
    process_telephony_event(make_event("s-old", "Answered", parties=live), store)
    process_telephony_event(make_event("s-old", "Disconnected",
                                       parties=[{"status": {"code": "Disconnected"},
                                                 "extensionId": "576959052",
                                                 "to": {}, "from": {}}]), store)
    process_telephony_event(make_event("s-new", "Answered", parties=live), store)
    assert fake_redis.get("rep:576959052:current") == "s-new"


def test_connected_rep_pointer_moves_to_newly_answered_session(store, fake_redis):
    """Answering a second call (connected leg on the new session) DOES move
    the pointer even though the old session is still active (e.g. on hold)."""
    live_old = [{"status": {"code": "Hold"}, "extensionId": "576959052",
                 "to": {}, "from": {}}]
    process_telephony_event(make_event("s-old", "Hold", parties=live_old), store)
    live_new = [{"status": {"code": "Answered"}, "extensionId": "576959052",
                 "to": {"extensionNumber": "119"}, "from": {}}]
    process_telephony_event(make_event("s-new", "Answered", parties=live_new), store)
    assert fake_redis.get("rep:576959052:current") == "s-new"


# ---------------------------------------------------------------- periodic reconcile
def test_fetch_active_call_sids(store):
    platform = _mock_platform({"/active-calls": {"records": [
        {"telephonySessionId": "s-1"}, {"id": "no-sid"},
    ]}})
    assert _fetch_active_call_sids(platform) == {"s-1"}


def test_fetch_active_call_sids_none_on_failure(store):
    platform = _mock_platform({"/active-calls": RuntimeError("RC down")})
    assert _fetch_active_call_sids(platform) is None


def test_reconcile_skips_sessions_with_recent_events(store, fake_redis):
    """RC's active-calls listing can lag on brand-new calls; a session that
    produced an event in the last 30 seconds must survive the sweep."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 7, 3, 18, 0, tzinfo=timezone.utc)
    store.store_call("s-new", {"sessionId": "s-new", "status": "Answered",
                               "to": {"extensionId": "119"},
                               "lastEventAt": (now - timedelta(seconds=20)).isoformat()})
    store.store_call("s-old", {"sessionId": "s-old", "status": "Answered",
                               "to": {"extensionId": "120"},
                               "lastEventAt": (now - timedelta(seconds=300)).isoformat()})
    _reconcile_active_sessions(store, set(), now=now)
    assert fake_redis.sismember("calls:active", "s-new")
    assert not fake_redis.sismember("calls:active", "s-old")


# ---------------------------------------------------------------- supervision resilience
@pytest.mark.asyncio
async def test_supervision_survives_other_monitored_leg_ending(store):
    """A monitored colleague's ended leg must not stop supervision while
    another monitored rep is still Answered — START wins over STOP.
    (Prod incident 2026-07-03: an ended leg for rep A killed supervision of
    the whole session while rep B was mid-call.)"""
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    sidecar.stop_supervision = AsyncMock()
    parties = [
        {"status": {"code": "Disconnected"}, "direction": "Outbound",
         "extensionId": "731501052", "to": {}, "from": {}},
        {"status": {"code": "Answered"}, "direction": "Outbound",
         "extensionId": "576959052",
         "to": {"extensionNumber": "108"}, "from": {}},
    ]
    process_telephony_event(make_event("s-multi", "Answered", parties=parties),
                            store, sidecar=sidecar,
                            monitored_extensions=["731501052", "576959052"])
    await asyncio.sleep(0)
    sidecar.stop_supervision.assert_not_called()
    sidecar.start_supervision.assert_awaited_once_with("s-multi", "108")


@pytest.mark.asyncio
async def test_supervision_stops_only_when_no_monitored_leg_active(store):
    """STOP still fires when every monitored leg has ended, even if
    unmonitored parties remain on the session."""
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    sidecar.stop_supervision = AsyncMock()
    parties = [
        {"status": {"code": "Disconnected"}, "direction": "Outbound",
         "extensionId": "576959052", "to": {}, "from": {}},
        {"status": {"code": "Answered"}, "direction": "Outbound",
         "extensionId": "999", "to": {}, "from": {}},
    ]
    process_telephony_event(make_event("s-solo", "Answered", parties=parties),
                            store, sidecar=sidecar,
                            monitored_extensions=["576959052"])
    await asyncio.sleep(0)
    sidecar.start_supervision.assert_not_called()
    sidecar.stop_supervision.assert_awaited_once_with("s-solo")


# ---------------------------------------------------------------- session reconcile
def test_fetch_active_session_events_returns_events_and_sids():
    platform = _mock_platform({
        "/active-calls": {"records": [{"telephonySessionId": "s-1"}]},
        "/telephony/sessions/s-1": {
            "telephonySessionId": "s-1",
            "parties": [{"status": {"code": "Answered"},
                         "to": {"extensionId": "119"}, "from": {}}],
        },
    })
    events, sids = _fetch_active_session_events(platform)
    assert len(events) == 1
    assert sids == {"s-1"}


def test_fetch_active_sids_include_detail_fetch_failures():
    """A session whose detail fetch fails yields no hydration event but MUST
    still count as RC-active — otherwise the reconcile would complete a live
    call over a transient fetch error."""
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
    events, sids = _fetch_active_session_events(platform)
    assert [e["body"]["telephonySessionId"] for e in events] == ["s-good"]
    assert sids == {"s-bad", "s-good"}


def test_reconcile_completes_sessions_missing_from_rc(store, fake_redis):
    store.store_call("s-zombie", {"sessionId": "s-zombie", "status": "Answered",
                                  "to": {"extensionId": "119"}})
    store.store_call("s-live", {"sessionId": "s-live", "status": "Answered",
                                "to": {"extensionId": "120"}})
    _reconcile_active_sessions(store, {"s-live"})
    assert not fake_redis.sismember("calls:active", "s-zombie")
    assert fake_redis.sismember("calls:active", "s-live")
    assert store.get_call("s-zombie")["status"] == "Disconnected"
    # Completed via the normal path: enters the extraction grace window so
    # downstream extraction/sellometer finalization still runs.
    assert fake_redis.sismember("calls:recently-ended", "s-zombie")


def test_reconcile_noop_when_rc_and_redis_agree(store, fake_redis):
    store.store_call("s-live", {"sessionId": "s-live", "status": "Answered",
                                "to": {"extensionId": "120"}})
    _reconcile_active_sessions(store, {"s-live"})
    assert fake_redis.sismember("calls:active", "s-live")
    assert store.get_call("s-live")["status"] == "Answered"


def test_process_event_clears_rep_pointer_on_disconnected(store):
    """When all parties disconnect, the rep's pointer to that session is cleared."""
    store.store_call("s-1", {
        "sessionId": "s-1", "status": "Answered", "direction": "Inbound",
        "from": {"phoneNumber": "+15125551234"}, "to": {"extensionId": "119"},
        "activeExtIds": ["119"],
    })
    store.set_rep_pointer("119", "s-1")

    # Process a Disconnected event for the call
    event = {
        "body": {
            "telephonySessionId": "s-1",
            "parties": [
                {
                    "extensionId": "119",
                    "status": {"code": "Disconnected"},
                    "direction": "Inbound",
                    "from": {"phoneNumber": "+15125551234"},
                    "to": {"extensionId": "119"},
                }
            ],
        }
    }
    process_telephony_event(event, store)

    # Pointer should be cleared
    assert store.get_rep_current_call("119") is None


def test_reconcile_active_sessions_uses_30s_grace_by_default(store):
    """Stale calls are swept if they haven't fired an event in 30+ seconds."""
    import datetime
    from src.call_monitor import _reconcile_active_sessions

    # Session that fired an event 31 seconds ago
    old_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=31)
    store.store_call("s-old", {
        "sessionId": "s-old",
        "status": "Answered",
        "lastEventAt": old_time.isoformat(),
    })

    # Simulate RC says this session is no longer active
    rc_active_sids = set()

    # Should sweep the old call
    _reconcile_active_sessions(store, rc_active_sids, now=datetime.datetime.now(datetime.timezone.utc))

    assert store.get_call("s-old") is not None  # state key still exists with TTL
    assert "s-old" not in store.active_session_ids()  # removed from active set


def test_call_data_includes_rep_ext_id(fake_redis):
    from src.redis_store import CallStore
    from src.call_monitor import process_telephony_event
    store = CallStore(fake_redis)
    event = {"body": {"telephonySessionId": "s-rep", "parties": [
        {"direction": "Inbound", "extensionId": "119",
         "status": {"code": "Answered"},
         "from": {"phoneNumber": "+15551234567"}, "to": {}},
    ]}}
    process_telephony_event(event, store, monitored_extensions={"119"})
    call = store.get_call("s-rep")
    assert call["repExtId"] == "119"
