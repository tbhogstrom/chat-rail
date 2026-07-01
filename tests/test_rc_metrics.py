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
