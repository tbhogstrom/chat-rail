"""RingCentral call-log → per-rep metrics and a recent-calls feed.

Pure functions (day_week_bounds / summarize_call_counts / build_recent_calls)
plus thin RC glue (fetch_ext_call_log / compute_metrics_and_recent).
"""
from datetime import datetime, timedelta, timezone

# A call "counts" only if it actually connected. Grounded against real account
# data: outbound answered = "Call connected", inbound answered = "Accepted".
# "Answered Elsewhere"/"Missed"/"Rejected"/"Hang Up"/"Voicemail" do not count.
CONNECTED_RESULTS = {"Accepted", "Call connected"}


def day_week_bounds(now: datetime, tz):
    """(start_of_today_utc, start_of_week_utc) for the local day/week in `tz`.

    Week starts Monday. `now` must be tz-aware; returns tz-aware UTC datetimes.
    astimezone recomputes the offset, so DST is handled correctly.
    """
    local = now.astimezone(tz)
    start_today_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_week_local = start_today_local - timedelta(days=start_today_local.weekday())
    return (start_today_local.astimezone(timezone.utc),
            start_week_local.astimezone(timezone.utc))


def _parse_start(rec: dict):
    ts = rec.get("startTime")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)  # 3.11+ handles trailing Z + fraction
    except ValueError:
        return None


def summarize_call_counts(records: list[dict], start_today: datetime,
                          start_week: datetime) -> dict:
    """Count connected Voice calls per direction, for today and this week."""
    counts = {"inboundToday": 0, "inboundWeek": 0,
              "outboundToday": 0, "outboundWeek": 0}
    for rec in records:
        if rec.get("type") != "Voice":
            continue
        if rec.get("result") not in CONNECTED_RESULTS:
            continue
        st = _parse_start(rec)
        if st is None or st < start_week:
            continue
        direction = rec.get("direction")
        if direction == "Inbound":
            counts["inboundWeek"] += 1
            if st >= start_today:
                counts["inboundToday"] += 1
        elif direction == "Outbound":
            counts["outboundWeek"] += 1
            if st >= start_today:
                counts["outboundToday"] += 1
    return counts
