import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.redis_store import CallStore
from src.sellometer_worker import run_sellometer_cycle, run_sellometer_worker

T0 = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)


def _t(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


def _start_call(store, sid="s-1", ext="576959052"):
    store.store_call(sid, {"sessionId": sid, "status": "Answered",
                           "to": {"extensionId": ext},
                           "activeExtIds": [ext]})


def test_cycle_writes_sellometer_with_started_at(store):
    _start_call(store)
    store.set_extracted("s-1", {"firstname": "Jim"})
    tracked = set()
    run_sellometer_cycle(store, tracked, now=_t(0))
    sm = store.get_sellometer("s-1")
    assert sm["score"] == 10
    assert sm["max"] == 100
    assert sm["startedAt"] == _t(0).isoformat()
    assert sm["timeline"] == []          # first minute still in progress
    assert sm["updatedAt"] == _t(0).isoformat()
    assert "s-1" in tracked


def test_cycle_preserves_started_at_and_grows_timeline(store):
    _start_call(store)
    store.set_extracted("s-1", {"firstname": "Jim"})
    tracked = set()
    run_sellometer_cycle(store, tracked, now=_t(0))
    store.add_call_event("s-1", "sales-script-opened", _t(1).isoformat())
    run_sellometer_cycle(store, tracked, now=_t(1.1))
    sm = store.get_sellometer("s-1")
    assert sm["startedAt"] == _t(0).isoformat()
    assert sm["score"] == 35             # name 10 + script 25
    assert sm["timeline"] == [35]        # end of minute 0
    by_id = {cp["id"]: cp for cp in sm["checkpoints"]}
    assert by_id["sales-script"]["ts"] == _t(1).isoformat()


def test_cycle_finalizes_when_session_leaves_active_set(store, fake_redis):
    _start_call(store)
    store.set_extracted("s-1", {"firstname": "Jim", "phone": "5034441123"})
    tracked = set()
    run_sellometer_cycle(store, tracked, now=_t(0))
    run_sellometer_cycle(store, tracked, now=_t(2.1))

    # Call ends; grace window expires (simulate by deleting the marker).
    store.complete_call("s-1")
    fake_redis.delete("call:s-1:extract-grace")

    run_sellometer_cycle(store, tracked, now=_t(3))
    records = store.get_sellometer_history("576959052")
    assert len(records) == 1
    rec = records[0]
    assert rec["sessionId"] == "s-1"
    assert rec["repExtId"] == "576959052"
    assert rec["score"] == 25
    assert rec["timeline"] == [25, 25, 25]   # minutes 0,1 + final append
    assert rec["startedAt"] == _t(0).isoformat()
    assert rec["endedAt"] == _t(3).isoformat()
    assert "s-1" not in tracked
    # Not finalized twice on the next cycle.
    run_sellometer_cycle(store, tracked, now=_t(3.1))
    assert len(store.get_sellometer_history("576959052")) == 1


def test_cycle_refreshes_events_ttl(store, fake_redis):
    """A checkpoint event's TTL must be renewed each live cycle, or a
    checkpoint clicked early in a >1h call could expire mid-call."""
    _start_call(store)
    store.add_call_event("s-1", "sales-script-opened", _t(0).isoformat())
    tracked = set()
    run_sellometer_cycle(store, tracked, now=_t(0))
    assert fake_redis.ttl("call:s-1:events") > 0
    run_sellometer_cycle(store, tracked, now=_t(1))
    assert fake_redis.ttl("call:s-1:events") > 0


def test_finalize_prefers_monitored_active_ext(store, fake_redis):
    with patch("src.sellometer_worker.Config") as MockConfig:
        MockConfig.MONITORED_EXTENSIONS = ["119"]
        store.store_call("s-q", {"sessionId": "s-q", "status": "Answered",
                                 "to": {"extensionId": "999"},   # queue ext
                                 "activeExtIds": ["119", "999"]})
        store.set_extracted("s-q", {"firstname": "Ann"})
        tracked = set()
        run_sellometer_cycle(store, tracked, now=_t(0))
        store.complete_call("s-q")
        fake_redis.delete("call:s-q:extract-grace")
        run_sellometer_cycle(store, tracked, now=_t(1))
        assert store.get_sellometer_history("119")[0]["repExtId"] == "119"


def test_finalize_without_sellometer_is_skipped(store, fake_redis):
    """A session that vanishes before any sellometer was written is dropped
    quietly (nothing to record)."""
    _start_call(store, sid="s-ghost")
    tracked = {"s-ghost"}
    store.complete_call("s-ghost")
    fake_redis.delete("call:s-ghost:extract-grace")
    fake_redis.delete("call:s-ghost:sellometer")
    run_sellometer_cycle(store, tracked, now=_t(1))
    assert store.get_sellometer_history("576959052") == []
    assert "s-ghost" not in tracked


@pytest.mark.asyncio
async def test_worker_loop_swallows_per_iteration_errors():
    store = MagicMock()
    calls = []

    def flaky_list():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return []

    store.list_active_sessions.side_effect = flaky_list
    task = asyncio.create_task(run_sellometer_worker(store, interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(calls) >= 2
