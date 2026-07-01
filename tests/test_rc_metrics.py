from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.rc_metrics import day_week_bounds, summarize_call_counts

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def test_day_week_bounds_summer_pdt():
    # 2026-07-01 (Wed) 18:00Z -> LA is PDT (-7); Monday of that week is Jun 29.
    now = datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
    start_today, start_week = day_week_bounds(now, LA)
    assert start_today == datetime(2026, 7, 1, 7, 0, tzinfo=UTC)   # LA midnight = 07:00Z
    assert start_week == datetime(2026, 6, 29, 7, 0, tzinfo=UTC)   # Mon 00:00 LA


def test_day_week_bounds_winter_pst_dst():
    # 2026-01-15 (Thu) 20:00Z -> LA is PST (-8); Monday of that week is Jan 12.
    now = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
    start_today, start_week = day_week_bounds(now, LA)
    assert start_today == datetime(2026, 1, 15, 8, 0, tzinfo=UTC)  # LA midnight = 08:00Z
    assert start_week == datetime(2026, 1, 12, 8, 0, tzinfo=UTC)


def test_summarize_call_counts_splits_and_filters():
    start_today = datetime(2026, 7, 1, 7, 0, tzinfo=UTC)
    start_week = datetime(2026, 6, 29, 7, 0, tzinfo=UTC)
    records = [
        {"type": "Voice", "direction": "Outbound", "result": "Call connected",
         "startTime": "2026-07-01T18:00:00.000Z"},   # today out
        {"type": "Voice", "direction": "Inbound", "result": "Accepted",
         "startTime": "2026-07-01T09:00:00.000Z"},    # today in
        {"type": "Voice", "direction": "Inbound", "result": "Accepted",
         "startTime": "2026-06-30T20:00:00.000Z"},    # this week, not today
        {"type": "Voice", "direction": "Inbound", "result": "Missed",
         "startTime": "2026-07-01T10:00:00.000Z"},    # excluded (missed)
        {"type": "Voice", "direction": "Inbound", "result": "Answered Elsewhere",
         "startTime": "2026-07-01T11:00:00.000Z"},    # excluded
        {"type": "Voice", "direction": "Outbound", "result": "Hang Up",
         "startTime": "2026-07-01T12:00:00.000Z"},    # excluded
        {"type": "Fax", "direction": "Inbound", "result": "Accepted",
         "startTime": "2026-07-01T13:00:00.000Z"},    # excluded (non-voice)
        {"type": "Voice", "direction": "Outbound", "result": "Call connected",
         "startTime": "2026-06-20T10:00:00.000Z"},    # before week, excluded
    ]
    counts = summarize_call_counts(records, start_today, start_week)
    assert counts == {"inboundToday": 1, "inboundWeek": 2,
                      "outboundToday": 1, "outboundWeek": 1}


def test_summarize_call_counts_empty():
    z = datetime(2026, 7, 1, 7, 0, tzinfo=UTC)
    assert summarize_call_counts([], z, z) == {
        "inboundToday": 0, "inboundWeek": 0, "outboundToday": 0, "outboundWeek": 0}


from src.rc_metrics import build_recent_calls, compute_metrics_and_recent


def _voice(sid, direction, result, start, other):
    side = "to" if direction == "Outbound" else "from"
    return {"type": "Voice", "telephonySessionId": sid, "direction": direction,
            "result": result, "startTime": start, side: {"phoneNumber": other}}


def test_build_recent_calls_dedups_sorts_and_maps_rep():
    per_ext = {
        "119": [
            _voice("s1", "Outbound", "Call connected", "2026-07-01T18:00:00.000Z", "+1678"),
            _voice("s2", "Inbound", "Answered Elsewhere", "2026-07-01T17:00:00.000Z", "+1206"),
        ],
        "121": [
            _voice("s2", "Inbound", "Accepted", "2026-07-01T17:00:00.000Z", "+1206"),
            _voice("s3", "Inbound", "Missed", "2026-07-01T16:00:00.000Z", "+1214"),
        ],
    }
    roster = {"119": {"name": "Doug Stoker"}, "121": {"name": "Travis Watters"}}
    rows = build_recent_calls(per_ext, roster, limit=15)

    assert [r["sessionId"] for r in rows] == ["s1", "s2", "s3"]   # newest first
    assert rows[0]["repName"] == "Doug Stoker"
    assert rows[0]["otherNumber"] == "+1678"      # outbound -> to
    assert rows[0]["connected"] is True
    # s2 deduped to the handling rep (Accepted beats Answered Elsewhere)
    assert rows[1]["repName"] == "Travis Watters"
    assert rows[1]["result"] == "Accepted"
    assert rows[1]["otherNumber"] == "+1206"      # inbound -> from
    # missed call still shown, flagged not-connected
    assert rows[2]["result"] == "Missed"
    assert rows[2]["connected"] is False


def test_build_recent_calls_respects_limit():
    per_ext = {"119": [
        _voice("s1", "Outbound", "Call connected", "2026-07-01T18:00:00.000Z", "+1"),
        _voice("s2", "Outbound", "Call connected", "2026-07-01T17:00:00.000Z", "+2"),
        _voice("s3", "Outbound", "Call connected", "2026-07-01T16:00:00.000Z", "+3"),
    ]}
    rows = build_recent_calls(per_ext, {"119": {"name": "Doug"}}, limit=2)
    assert [r["sessionId"] for r in rows] == ["s1", "s2"]


class _FakePlatform:
    def __init__(self, by_ext):
        self.by_ext = by_ext

    def get(self, path, params=None):
        ext = path.rsplit("/", 2)[1]   # .../extension/{ext}/call-log
        recs = self.by_ext.get(ext, [])

        class _Resp:
            def json_dict(_self):
                return {"records": recs, "paging": {}}
        return _Resp()


def test_compute_metrics_and_recent_end_to_end():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    by_ext = {
        "119": [
            _voice("s1", "Outbound", "Call connected", "2026-07-01T18:00:00.000Z", "+1678"),
            _voice("s9", "Inbound", "Missed", "2026-07-01T15:00:00.000Z", "+1206"),
        ],
        "121": [
            _voice("s2", "Inbound", "Accepted", "2026-07-01T17:00:00.000Z", "+1206"),
        ],
    }
    roster = {"119": {"name": "Doug"}, "121": {"name": "Travis"}}
    now = datetime(2026, 7, 1, 20, tzinfo=timezone.utc)
    metrics, recent = compute_metrics_and_recent(
        _FakePlatform(by_ext), ["119", "121"], roster, now,
        ZoneInfo("America/Los_Angeles"), limit=15)

    assert metrics["119"]["outboundToday"] == 1
    assert metrics["121"]["inboundToday"] == 1
    assert [r["sessionId"] for r in recent] == ["s1", "s2", "s9"]  # newest first
