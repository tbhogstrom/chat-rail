from datetime import datetime, timezone

import pytest

from src.metrics_worker import refresh_metrics_once, _resolve_tz
from src.redis_store import CallStore

UTC = timezone.utc


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


class _FakePlatform:
    def __init__(self, by_ext):
        self.by_ext = by_ext

    def get(self, path, params=None):
        ext = path.rsplit("/", 2)[1]
        recs = self.by_ext.get(ext, [])

        class _Resp:
            def json_dict(_self):
                return {"records": recs, "paging": {}}
        return _Resp()


class _BadPlatform:
    def get(self, path, params=None):
        raise RuntimeError("RC down")


def _out_connected(sid, start, other):
    return {"type": "Voice", "telephonySessionId": sid, "direction": "Outbound",
            "result": "Call connected", "startTime": start,
            "to": {"phoneNumber": other}}


def test_refresh_writes_metrics_and_recent(store):
    store.set_rep_roster({"119": {"name": "Doug"}})
    platform = _FakePlatform({
        "119": [_out_connected("s1", "2026-07-01T18:00:00.000Z", "+1678")],
    })
    n_m, n_r = refresh_metrics_once(
        store, platform, _resolve_tz("America/Los_Angeles"), ["119"],
        now=datetime(2026, 7, 1, 20, tzinfo=UTC))
    assert n_m == 1 and n_r == 1
    assert store.get_rep_metrics()["119"]["outboundToday"] == 1
    assert store.get_recent_calls()[0]["sessionId"] == "s1"


def test_refresh_retains_last_good_on_failure(store):
    """A failed RC cycle must not overwrite good data with empties."""
    good_metrics = {"119": {"inboundToday": 3, "inboundWeek": 9,
                            "outboundToday": 4, "outboundWeek": 12}}
    good_recent = [{"sessionId": "s-old", "repName": "Doug", "connected": True}]
    store.set_rep_metrics(good_metrics)
    store.set_recent_calls(good_recent)

    with pytest.raises(RuntimeError):
        refresh_metrics_once(store, _BadPlatform(),
                             _resolve_tz("America/Los_Angeles"), ["119"],
                             now=datetime(2026, 7, 1, 20, tzinfo=UTC))

    assert store.get_rep_metrics() == good_metrics
    assert store.get_recent_calls() == good_recent


def test_resolve_tz_falls_back_to_utc_on_bad_name():
    assert _resolve_tz("Not/AZone") is timezone.utc
    # a valid name still resolves normally
    assert _resolve_tz("America/Los_Angeles") is not timezone.utc
