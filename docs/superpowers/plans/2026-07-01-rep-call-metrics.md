# Per-Rep Call Metrics + Recent Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show per-rep connected-call counts (today / this week, split inbound/outbound) and a recent-calls feed on the overview page, sourced from the RingCentral call-log.

**Architecture:** A monitor-side worker fetches each monitored rep's call-log once per cycle, derives per-rep counts and a merged recent-calls list, and writes both to Redis. The API reads Redis; the overview page polls the API. Pure logic lives in `src/rc_metrics.py`; the worker is thin glue.

**Tech Stack:** Python 3.12+, FastAPI, RingCentral SDK, upstash-redis / fakeredis, `zoneinfo` + `tzdata`, vanilla HTML+JS.

## Global Constraints

- Python `>=3.12` (repo runs 3.14). `datetime.fromisoformat` handles trailing `Z` (3.11+).
- All `CallStore` methods work with **both** upstash-redis and fakeredis — JSON string under a single key, positional `set`/`get` only (no `hset`).
- Reuse existing auth: `/api/calls` router already depends on `verify_api_key` (`x-api-key`).
- **Connected** call = `type == "Voice"` and `result ∈ {"Accepted", "Call connected"}`.
- Windows in **America/Los_Angeles** (config `METRICS_TIMEZONE`, default `America/Los_Angeles`), **week starts Monday** (ISO, `weekday()==0`).
- Recent feed: monitored reps only, **all results**, newest first, limit **15**, dedup a shared `telephonySessionId` to the most-handled instance (connected > answered-elsewhere > other).
- `otherNumber` = `to.phoneNumber` for outbound, `from.phoneNumber` for inbound.
- Worker refresh interval **60s**; on failure retain last-good (never overwrite with empties).
- Run tests with `python -m pytest` from repo root. Commit messages: conventional style, **no** `Co-Authored-By: Claude` trailer.

---

### Task 1: `rc_metrics` counting core — `day_week_bounds`, `summarize_call_counts`

**Files:**
- Create: `src/rc_metrics.py`
- Test: `tests/test_rc_metrics.py`

**Interfaces:**
- Produces:
  - `day_week_bounds(now: datetime, tz) -> tuple[datetime, datetime]` — `(start_today_utc, start_week_utc)`, both tz-aware UTC. `now` must be tz-aware. Week starts Monday.
  - `summarize_call_counts(records: list[dict], start_today: datetime, start_week: datetime) -> dict` — `{"inboundToday","inboundWeek","outboundToday","outboundWeek"}` (ints).
  - `CONNECTED_RESULTS: set[str]` = `{"Accepted", "Call connected"}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rc_metrics.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rc_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.rc_metrics'`

- [ ] **Step 3: Implement the counting core**

Create `src/rc_metrics.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rc_metrics.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/rc_metrics.py tests/test_rc_metrics.py
git commit -m "feat(metrics): call-log counting core (day/week bounds + connected counts)"
```

---

### Task 2: `rc_metrics` assembly — recent calls, fetch, compute

**Files:**
- Modify: `src/rc_metrics.py`
- Test: `tests/test_rc_metrics.py`

**Interfaces:**
- Consumes: `CONNECTED_RESULTS`, `day_week_bounds`, `summarize_call_counts` (Task 1).
- Produces:
  - `build_recent_calls(per_ext_records: dict[str, list[dict]], roster: dict, limit: int = 15) -> list[dict]` — merged, deduped, newest-first rows: `{sessionId, startTime, repExtId, repName, direction, otherNumber, result, connected}`.
  - `fetch_ext_call_log(platform, ext_id: str, date_from_iso: str) -> list[dict]` — paginated call-log records.
  - `compute_metrics_and_recent(platform, monitored: list[str], roster: dict, now: datetime, tz, limit: int = 15) -> tuple[dict, list]` — `({extId: counts}, recent_rows)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rc_metrics.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_rc_metrics.py -k "recent or compute" -v`
Expected: FAIL with `ImportError: cannot import name 'build_recent_calls'`

- [ ] **Step 3: Implement the assembly functions**

Append to `src/rc_metrics.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_rc_metrics.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/rc_metrics.py tests/test_rc_metrics.py
git commit -m "feat(metrics): recent-calls feed + call-log fetch/compute assembly"
```

---

### Task 3: `CallStore` metrics + recent round-trip

**Files:**
- Modify: `src/redis_store.py` (add after `set_rep_roster`/`get_rep_roster`)
- Test: `tests/test_redis_store.py`

**Interfaces:**
- Produces:
  - `set_rep_metrics(metrics: dict) -> None` / `get_rep_metrics() -> dict` (key `reps:metrics`).
  - `set_recent_calls(calls: list) -> None` / `get_recent_calls() -> list` (key `overview:recent`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_redis_store.py`:

```python
def test_set_and_get_rep_metrics(store):
    m = {"119": {"inboundToday": 1, "inboundWeek": 2,
                 "outboundToday": 3, "outboundWeek": 4}}
    store.set_rep_metrics(m)
    assert store.get_rep_metrics() == m


def test_get_rep_metrics_empty_returns_empty_dict(store):
    assert store.get_rep_metrics() == {}


def test_set_and_get_recent_calls(store):
    calls = [{"sessionId": "s1", "repName": "Doug", "connected": True}]
    store.set_recent_calls(calls)
    assert store.get_recent_calls() == calls


def test_get_recent_calls_empty_returns_empty_list(store):
    assert store.get_recent_calls() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_redis_store.py -k "metrics or recent_calls" -v`
Expected: FAIL with `AttributeError: 'CallStore' object has no attribute 'set_rep_metrics'`

- [ ] **Step 3: Implement the methods**

Append inside the `CallStore` class in `src/redis_store.py` (after `get_rep_roster`):

```python
    def set_rep_metrics(self, metrics: dict) -> None:
        """Persist per-rep call counts ({extId: {inbound/outbound Today/Week}})."""
        self.redis.set("reps:metrics", json.dumps(metrics))

    def get_rep_metrics(self) -> dict:
        """Return per-rep call counts, or {} if not yet computed."""
        raw = self.redis.get("reps:metrics")
        return json.loads(raw) if raw is not None else {}

    def set_recent_calls(self, calls: list) -> None:
        """Persist the recent-calls feed (list of row dicts, newest first)."""
        self.redis.set("overview:recent", json.dumps(calls))

    def get_recent_calls(self) -> list:
        """Return the recent-calls feed, or [] if not yet computed."""
        raw = self.redis.get("overview:recent")
        return json.loads(raw) if raw is not None else []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_redis_store.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/redis_store.py tests/test_redis_store.py
git commit -m "feat(store): persist per-rep metrics and recent-calls feed"
```

---

### Task 4: API — `reps.metrics` + `GET /api/calls/recent`

**Files:**
- Modify: `src/api/routes.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `CallStore.get_rep_metrics` / `get_recent_calls` (Task 3).
- Produces: each `/api/calls/reps` rep gains `"metrics": {...}|null`; new `GET /api/calls/recent` → `{"calls": [...]}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.MONITORED_EXTENSIONS", ["119"])
def test_get_reps_includes_metrics(client, store):
    store.set_rep_roster({"119": {"name": "Doug Stoker", "number": "119"}})
    store.set_rep_metrics({"119": {"inboundToday": 1, "inboundWeek": 2,
                                   "outboundToday": 3, "outboundWeek": 4}})
    rep = client.get("/api/calls/reps", headers=auth_header()).json()["reps"][0]
    assert rep["metrics"] == {"inboundToday": 1, "inboundWeek": 2,
                              "outboundToday": 3, "outboundWeek": 4}


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.MONITORED_EXTENSIONS", ["119"])
def test_get_reps_metrics_null_when_unset(client, store):
    store.set_rep_roster({"119": {"name": "Doug Stoker", "number": "119"}})
    rep = client.get("/api/calls/reps", headers=auth_header()).json()["reps"][0]
    assert rep["metrics"] is None


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_recent_calls(client, store):
    store.set_recent_calls([{"sessionId": "s1", "repName": "Doug",
                             "direction": "Outbound", "connected": True}])
    r = client.get("/api/calls/recent", headers=auth_header())
    assert r.status_code == 200
    assert r.json() == {"calls": [{"sessionId": "s1", "repName": "Doug",
                                   "direction": "Outbound", "connected": True}]}


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_recent_calls_empty(client):
    r = client.get("/api/calls/recent", headers=auth_header())
    assert r.status_code == 200
    assert r.json() == {"calls": []}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -k "metrics or recent" -v`
Expected: FAIL (KeyError `metrics` / 404 on `/recent`)

- [ ] **Step 3: Implement — add metrics to `get_reps` and the `/recent` route**

In `src/api/routes.py`, in `get_reps`, add the metrics read before the loop and the field in the rep dict:

```python
def get_reps():
    store = get_store()
    roster = store.get_rep_roster()
    metrics = store.get_rep_metrics()
    active_ids = {c.get("sessionId") for c in store.list_active_calls()}
    reps = []
    for ext_id in Config.MONITORED_EXTENSIONS:
        entry = roster.get(ext_id) or {}
        call = store.get_rep_current_call(ext_id)
        on_call = _rep_on_call(call, ext_id, active_ids)
        reps.append({
            "extId": ext_id,
            "name": entry.get("name") or f"Ext {ext_id}",
            "number": entry.get("number"),
            "onCall": on_call,
            "status": call.get("status") if on_call else None,
            "direction": call.get("direction") if on_call else None,
            "sessionId": call.get("sessionId") if on_call else None,
            "callerNumber": _caller_number(call) if on_call else None,
            "metrics": metrics.get(ext_id),
        })
    return {"reps": reps}
```

Then add the route just after `get_reps`:

```python
@router.get("/recent")
def get_recent_calls():
    store = get_store()
    return {"calls": store.get_recent_calls()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/api/routes.py tests/test_api.py
git commit -m "feat(api): rep metrics on /reps and GET /api/calls/recent"
```

---

### Task 5: Config + metrics worker + wiring

**Files:**
- Modify: `src/config.py` (add `METRICS_TIMEZONE`)
- Modify: `pyproject.toml`, `requirements.txt` (declare `tzdata`)
- Create: `src/metrics_worker.py`
- Modify: `run_local.py`, `run_monitor.py` (wire the worker)
- Test: `tests/test_config.py` (create)

**Interfaces:**
- Consumes: `compute_metrics_and_recent` (Task 2), `CallStore` metrics/recent setters (Task 3), `CallStore.get_rep_roster`.
- Produces: `run_metrics_worker(store, interval: int = 60)` (async loop); `Config.METRICS_TIMEZONE`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config.py`:

```python
from zoneinfo import ZoneInfo
from src.config import Config


def test_metrics_timezone_default_is_valid_iana():
    assert Config.METRICS_TIMEZONE == "America/Los_Angeles"
    ZoneInfo(Config.METRICS_TIMEZONE)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: type object 'Config' has no attribute 'METRICS_TIMEZONE'`

- [ ] **Step 3: Add the config value**

In `src/config.py`, inside `class Config`, after the `MONITORED_EXTENSIONS` block, add:

```python
    # Timezone for metrics day/week boundaries (IANA name). Week starts Monday.
    METRICS_TIMEZONE: str = os.environ.get("METRICS_TIMEZONE") or "America/Los_Angeles"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Declare the `tzdata` dependency**

In `pyproject.toml`, add to the `dependencies` list:

```toml
    "tzdata>=2025.1",
```

In `requirements.txt`, add a line:

```
tzdata>=2025.1
```

- [ ] **Step 6: Create the worker**

Create `src/metrics_worker.py`:

```python
"""Background worker: refresh per-rep call metrics + recent-calls feed from RC."""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ringcentral import SDK

from src.config import Config
from src.rc_metrics import compute_metrics_and_recent
from src.redis_store import CallStore

logger = logging.getLogger(__name__)


async def run_metrics_worker(store: CallStore, interval: int = 60) -> None:
    """Every `interval`s: fetch each monitored rep's call-log, derive counts +
    recent feed, write to Redis. Retains last-good values on any failure.
    """
    tz = ZoneInfo(Config.METRICS_TIMEZONE)
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    logger.info("Metrics worker starting (interval=%ss, tz=%s)",
                interval, Config.METRICS_TIMEZONE)
    while True:
        try:
            if not platform.logged_in():
                platform.login(jwt=Config.RC_JWT)
            now = datetime.now(timezone.utc)
            metrics, recent = compute_metrics_and_recent(
                platform, Config.MONITORED_EXTENSIONS, store.get_rep_roster(),
                now, tz, limit=15)
            store.set_rep_metrics(metrics)
            store.set_recent_calls(recent)
            logger.info("Metrics refreshed for %d rep(s), %d recent call(s)",
                        len(metrics), len(recent))
        except Exception:
            # Retain last-good metrics/recent; re-login defensively for next cycle.
            logger.exception("Metrics worker cycle failed")
            try:
                platform.login(jwt=Config.RC_JWT)
            except Exception:
                logger.exception("Metrics worker re-login failed")
        await asyncio.sleep(interval)
```

Note: `platform.logged_in()` is provided by the RC SDK; the `except` re-login covers token expiry between cycles.

- [ ] **Step 7: Wire into `run_local.py`**

Add the import near the other `src` imports at the top of `run_local.py`:

```python
from src.metrics_worker import run_metrics_worker
```

Add the worker to the `asyncio.gather` call:

```python
    await asyncio.gather(
        server.serve(),
        run_monitor(store, sidecar=sidecar),
        run_extraction_worker(store),
        run_metrics_worker(store),
    )
```

- [ ] **Step 8: Wire into `run_monitor.py`**

Add the import near the top of `run_monitor.py`:

```python
from src.metrics_worker import run_metrics_worker
```

In `main()`, after the `tasks = [run_monitor(store)]` / DEEPGRAM block and before `await asyncio.gather(*tasks)`, add:

```python
    tasks.append(run_metrics_worker(store))
```

- [ ] **Step 9: Verify imports and full suite**

Run: `python -c "import run_local, run_monitor; from src.metrics_worker import run_metrics_worker; print('imports OK')"`
Expected: `imports OK`

Run: `python -m pytest -q`
Expected: PASS (all)

- [ ] **Step 10: Commit**

```bash
git add src/config.py src/metrics_worker.py run_local.py run_monitor.py pyproject.toml requirements.txt tests/test_config.py
git commit -m "feat(metrics): metrics worker, METRICS_TIMEZONE config, tzdata dep, wiring"
```

---

### Task 6: Overview page — metrics line + recent-calls feed

**Files:**
- Modify: `src/api/static/overview.html` (replace file)
- Test: `tests/test_api.py` (existing `test_root_serves_overview_page` still passes)

**Interfaces:**
- Consumes: `GET /api/calls/reps` (`metrics` per rep) and `GET /api/calls/recent`.

- [ ] **Step 1: Replace `overview.html` with the metrics + recent version**

Write `src/api/static/overview.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>SFW Call Bridge — Reps</title>
  <style>
    :root {
      --bg: #0f1320; --panel: #1a2036; --border: #2a3252;
      --text: #e6ecff; --muted: #8c97c2; --red: #f87171;
      --green: #4ade80; --accent: #7c9cff;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: -apple-system, Segoe UI, Roboto, sans-serif;
           background: var(--bg); color: var(--text); line-height: 1.4; }
    header { padding: 12px 20px; background: var(--panel);
             border-bottom: 1px solid var(--border);
             display: flex; justify-content: space-between; align-items: center; }
    h1 { margin: 0; font-size: 18px; font-weight: 600; }
    #status { font-size: 12px; color: var(--muted); }
    main { max-width: 720px; margin: 0 auto; padding: 16px; }
    h2.section { font-size: 12px; color: var(--muted); text-transform: uppercase;
                 letter-spacing: 0.08em; margin: 24px 0 10px; }
    a.rep { display: flex; align-items: center; gap: 12px; text-decoration: none;
            color: var(--text); background: var(--panel);
            border: 1px solid var(--border); border-radius: 8px;
            padding: 14px 16px; margin-bottom: 10px; }
    a.rep:hover { border-color: var(--accent); }
    .dot { width: 12px; height: 12px; border-radius: 50%;
           background: var(--muted); flex: none; }
    .dot.on { background: var(--green); }
    .name { font-size: 15px; font-weight: 600; }
    .ext { font-size: 12px; color: var(--muted); }
    .metrics { font-size: 12px; color: var(--muted); margin-top: 2px; }
    .meta { margin-left: auto; text-align: right; font-size: 12px;
            color: var(--muted); }
    .meta .live { color: var(--green); font-weight: 600; }
    .empty { color: var(--muted); text-align: center; padding: 24px 0; }
    table.recent { width: 100%; border-collapse: collapse; font-size: 13px; }
    table.recent td { padding: 8px 10px; border-bottom: 1px solid var(--border);
                      white-space: nowrap; }
    table.recent td.num { color: var(--muted); font-variant-numeric: tabular-nums; }
    table.recent tr.missed td { color: var(--muted); }
    .badge { font-size: 11px; padding: 1px 6px; border-radius: 3px;
             border: 1px solid var(--border); }
    .badge.miss { color: var(--red); border-color: var(--red); }
    .dir { font-weight: 600; }
  </style>
</head>
<body>
  <header>
    <h1>SFW Call Bridge — Reps</h1>
    <div id="status">Loading…</div>
  </header>
  <main>
    <div id="list"><div class="empty">Loading…</div></div>
    <h2 class="section">Recent calls</h2>
    <div id="recent"><div class="empty">Loading…</div></div>
  </main>

  <script>
    let apiKey = localStorage.getItem("sfw-bridge-key");
    if (!apiKey) {
      apiKey = prompt("Enter your SFW Bridge API key (x-api-key):") || "";
      if (apiKey) localStorage.setItem("sfw-bridge-key", apiKey);
    }
    const authHeaders = { "x-api-key": apiKey };

    function esc(s) {
      return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function fmtMetrics(m) {
      if (!m) return "—";
      return `in ${m.inboundToday}·${m.inboundWeek} · out ${m.outboundToday}·${m.outboundWeek}`;
    }

    function fmtTime(iso) {
      if (!iso) return "";
      try {
        return new Date(iso).toLocaleTimeString("en-US", {
          timeZone: "America/Los_Angeles", hour: "numeric", minute: "2-digit" });
      } catch { return iso; }
    }

    function renderReps(reps) {
      const list = document.getElementById("list");
      if (!reps.length) {
        list.innerHTML = '<div class="empty">Waiting for roster…</div>';
        return;
      }
      list.innerHTML = reps.map(r => {
        const on = r.onCall;
        const meta = on
          ? `<span class="live">On call</span><br>${esc(r.status || "")}` +
            (r.callerNumber ? `<br>${esc(r.callerNumber)}` : "")
          : "idle";
        return `<a class="rep" href="/dashboard?rep=${encodeURIComponent(r.extId)}">
          <span class="dot ${on ? "on" : ""}"></span>
          <span><span class="name">${esc(r.name)}</span>
            <span class="ext">ext ${esc(r.number || r.extId)}</span>
            <div class="metrics">${esc(fmtMetrics(r.metrics))}</div></span>
          <span class="meta">${meta}</span>
        </a>`;
      }).join("");
    }

    function renderRecent(calls) {
      const box = document.getElementById("recent");
      if (!calls.length) {
        box.innerHTML = '<div class="empty">No recent calls</div>';
        return;
      }
      box.innerHTML = '<table class="recent"><tbody>' + calls.map(c => {
        const arrow = c.direction === "Outbound" ? "→" : "←";
        const badge = c.connected
          ? `<span class="badge">${esc(c.result || "")}</span>`
          : `<span class="badge miss">${esc(c.result || "missed")}</span>`;
        return `<tr class="${c.connected ? "" : "missed"}">
          <td class="num">${esc(fmtTime(c.startTime))}</td>
          <td>${esc(c.repName || "")}</td>
          <td class="dir">${arrow}</td>
          <td class="num">${esc(c.otherNumber || "")}</td>
          <td>${badge}</td>
        </tr>`;
      }).join("") + '</tbody></table>';
    }

    async function poll() {
      try {
        const r = await fetch("/api/calls/reps", { headers: authHeaders });
        if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
        const data = await r.json();
        renderReps(data.reps || []);
        const active = (data.reps || []).filter(x => x.onCall).length;
        document.getElementById("status").textContent =
          `${(data.reps || []).length} reps • ${active} on call`;
      } catch (e) {
        document.getElementById("status").textContent = `Error: ${e.message}`;
      }
    }

    async function pollRecent() {
      try {
        const r = await fetch("/api/calls/recent", { headers: authHeaders });
        if (!r.ok) return;
        const data = await r.json();
        renderRecent(data.calls || []);
      } catch (_) { /* keep last render */ }
    }

    setInterval(poll, 3000);
    setInterval(pollRecent, 5000);
    poll();
    pollRecent();
  </script>
</body>
</html>
```

- [ ] **Step 2: Verify the page route still serves**

Run: `python -m pytest tests/test_api.py::test_root_serves_overview_page -v`
Expected: PASS

- [ ] **Step 3: Manually verify**

Restart the local server:

```bash
python run_local.py
```

Open `http://localhost:8000/`. Expected: each rep row shows a metrics line like `in 5·20 · out 7·31` (or `—` until the first ~60s refresh); a "Recent calls" table lists the latest calls with local (PT) time, rep, direction arrow, number, and result — missed/unhandled results shown in red. Confirm `/api/calls/recent` returns data:

```bash
KEY=$(grep '^CALL_BRIDGE_API_KEY=' .env | cut -d= -f2-)
curl -s -H "x-api-key: $KEY" http://localhost:8000/api/calls/recent | python -m json.tool | head -30
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/api/static/overview.html
git commit -m "feat(overview): show per-rep metrics and a recent-calls feed"
```

---

## Self-Review Notes

- **Spec coverage:** counting core + tz bounds (Task 1); recent feed + fetch/compute (Task 2); store persistence (Task 3); API surface `metrics` + `/recent` (Task 4); worker + config + tzdata + wiring (Task 5); page metrics line + recent feed (Task 6). Connected filter, dedup priority, otherNumber, Monday/LA, 60s/last-good, limit 15 — all present.
- **Placeholder scan:** none; every code step is complete.
- **Type consistency:** `summarize_call_counts(records, start_today, start_week)`, `build_recent_calls(per_ext_records, roster, limit)`, `compute_metrics_and_recent(platform, monitored, roster, now, tz, limit)`, `CONNECTED_RESULTS`, store `set_rep_metrics/get_rep_metrics/set_recent_calls/get_recent_calls`, rep field `metrics`, recent row keys (`sessionId/startTime/repExtId/repName/direction/otherNumber/result/connected`) — used identically across tasks and the page.
- **Windows/Vercel:** `tzdata` declared so `ZoneInfo` works off the system tz database absence.
