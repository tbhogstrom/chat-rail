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


def _other_number(rec: dict):
    if rec.get("direction") == "Outbound":
        return (rec.get("to") or {}).get("phoneNumber")
    return (rec.get("from") or {}).get("phoneNumber")


def _result_rank(result) -> int:
    if result in CONNECTED_RESULTS:
        return 2
    if result == "Answered Elsewhere":
        return 1
    return 0


def build_recent_calls(per_ext_records: dict, roster: dict,
                       limit: int = 15) -> list[dict]:
    """Merge monitored reps' call-logs into a deduped, newest-first feed.

    A call can appear on two reps' logs (answered by one, rung on another);
    keep the most-handled instance so it shows once, attributed to who took it.
    startTime is uniform ISO-8601 `...Z`, so string sort == chronological.
    """
    by_session: dict[str, dict] = {}
    for ext_id, records in per_ext_records.items():
        rep_name = (roster.get(ext_id) or {}).get("name") or f"Ext {ext_id}"
        for rec in records:
            if rec.get("type") != "Voice":
                continue
            sid = (rec.get("telephonySessionId") or rec.get("sessionId")
                   or rec.get("id"))
            if not sid or not rec.get("startTime"):
                continue
            row = {
                "sessionId": sid,
                "startTime": rec.get("startTime"),
                "repExtId": ext_id,
                "repName": rep_name,
                "direction": rec.get("direction"),
                "otherNumber": _other_number(rec),
                "result": rec.get("result"),
                "connected": rec.get("result") in CONNECTED_RESULTS,
            }
            prev = by_session.get(sid)
            if prev is None or _result_rank(row["result"]) > _result_rank(prev["result"]):
                by_session[sid] = row
    rows = sorted(by_session.values(), key=lambda r: r["startTime"], reverse=True)
    return rows[:limit]


def fetch_ext_call_log(platform, ext_id: str, date_from_iso: str) -> list[dict]:
    """All call-log records for an extension since `date_from_iso` (paginated)."""
    records: list[dict] = []
    page = 1
    while True:
        resp = platform.get(
            f"/restapi/v1.0/account/~/extension/{ext_id}/call-log",
            {"dateFrom": date_from_iso, "perPage": 250, "page": page, "view": "Simple"},
        ).json_dict()
        page_records = resp.get("records", [])
        records.extend(page_records)
        if len(page_records) < 250:
            break
        page += 1
    return records


def compute_metrics_and_recent(platform, monitored: list[str], roster: dict,
                               now: datetime, tz, limit: int = 15):
    """Fetch each monitored rep's week call-log once; derive counts + recent feed."""
    start_today, start_week = day_week_bounds(now, tz)
    date_from = start_week.isoformat().replace("+00:00", "Z")
    per_ext: dict[str, list[dict]] = {}
    metrics: dict[str, dict] = {}
    for ext_id in monitored:
        records = fetch_ext_call_log(platform, ext_id, date_from)
        per_ext[ext_id] = records
        metrics[ext_id] = summarize_call_counts(records, start_today, start_week)
    recent = build_recent_calls(per_ext, roster, limit=limit)
    return metrics, recent
