# Per-Rep Call Metrics + Recent Calls — Design

**Date:** 2026-07-01
**Status:** Approved

## Problem

The overview page lists monitored reps and live on-call state, but shows no
sense of activity. We want two additions:

1. **Per-rep call volume:** connected calls **today** and **this week**, split
   **inbound / outbound**.
2. **A recent-calls feed:** the last ~15 calls across the monitored reps
   (including missed / unanswered), newest first — a safety net so recent calls
   are visible even when nobody is watching a live transcript.

The Redis call store keeps a call only ~1h after it ends (`CALL_TTL = 3600`), so
it cannot answer "how many calls today/this week" or "what were the last calls".
RingCentral's call-log is the authoritative history and is the source for both.
Both are produced by one monitor-side worker from the same per-rep call-log
fetch.

## Semantics (confirmed)

- **Source:** RingCentral per-extension call-log
  (`GET /restapi/v1.0/account/~/extension/{extensionId}/call-log`).
- **Connected only:** `type == "Voice"` and `result ∈ {"Accepted", "Call connected"}`.
  Grounded against real account data — the distinct `(direction, result)` values
  seen in a week were:

  | Direction | result | connected? |
  |-----------|--------|------------|
  | Outbound  | `Call connected`      | yes |
  | Inbound   | `Accepted`            | yes |
  | Inbound   | `Answered Elsewhere`  | no (a colleague answered the ring-group call) |
  | Inbound   | `Missed`              | no |
  | Outbound  | `Hang Up`             | no |
  | Inbound   | `Rejected`            | no |

  `Answered Elsewhere` is excluded automatically, so counts reflect calls the rep
  actually handled. If RC later emits other connected variants, extend the set.
- **Split:** inbound vs outbound, via the record's `direction` field.
- **Windows:** **today** and **this week**, boundaries in **America/Los_Angeles**
  (PST/PDT), **week starts Monday** (ISO). Timezone is configurable via a new
  `METRICS_TIMEZONE` env var (IANA name), default `America/Los_Angeles`.
- **Four numbers per rep:** `inboundToday, inboundWeek, outboundToday, outboundWeek`
  (today ⊆ week).

### Recent-calls feed

- **Scope:** the monitored reps only, **all results** (connected, missed,
  voicemail, answered-elsewhere) so unhandled calls are visible.
- **Size / order:** last **15**, newest first (by `startTime`).
- **Per row:** `startTime`, rep name + ext, `direction`, the other party's number
  (`to` for outbound, `from` for inbound), `result`, and a `connected` flag (for
  styling missed vs handled). Time is rendered in America/Los_Angeles on the page.
- **Dedup:** a call can appear on two monitored reps' logs (e.g. `Accepted` on the
  rep who answered, `Answered Elsewhere` on another who was rung). Group by
  `telephonySessionId` and keep the most-handled instance
  (connected > answered-elsewhere > other), so each call appears once, attributed
  to the rep who handled it.

## Architecture

Mirror the roster pattern: a monitor-side worker refreshes metrics on an interval
and writes them to Redis; the API only reads Redis.

- Keeps RC access (JWT login, rate limits) in the always-on monitor process, not
  the serverless Vercel API.
- Keeps the overview page fast — it polls Redis-backed data, no RC round-trip.
- One call-log fetch per rep (from start-of-week) covers both windows.

Rejected alternative: API calls RC live per page-load with caching — puts RC
latency + JWT login into a page that polls every few seconds and does not fit
Vercel's serverless API.

## Components

### 1. `src/rc_metrics.py`

Three focused units:

- `day_week_bounds(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]`
  — returns `(start_of_today_utc, start_of_week_utc)`. Start of week = most recent
  Monday 00:00 in `tz`. Pure; testable including a DST boundary.

- `summarize_call_counts(records, start_today, start_week) -> dict`
  — `start_today` / `start_week` are timezone-aware UTC `datetime`s. Filters to
  connected Voice calls, parses each record's `startTime` with
  `datetime.fromisoformat` (Python 3.11+ handles the trailing `Z` and fractional
  seconds), and buckets by direction: a call counts toward `*Week` when
  `startTime >= start_week` and additionally toward `*Today` when
  `startTime >= start_today`. Comparison is on parsed `datetime`s, not strings,
  to avoid millisecond/precision boundary bugs. Records outside the week are
  ignored, so the function is self-contained regardless of the fetch window.
  Returns `{"inboundToday", "inboundWeek", "outboundToday", "outboundWeek"}`
  (ints). Pure; the core logic.

- `fetch_ext_call_log(platform, ext_id: str, date_from_iso: str) -> list[dict]`
  — GETs the extension call-log from `date_from_iso` to now, paginating
  (`perPage=250`, follow pages while a full page returns). Thin RC wrapper.

- `build_recent_calls(per_ext_records: dict[str, list[dict]], roster: dict, limit=15) -> list[dict]`
  — merges every monitored rep's records, maps each to its rep via the queried
  ext (name from `roster`), dedups by `telephonySessionId` (keeping the
  most-handled instance), sorts by `startTime` descending, and returns the top
  `limit` as row dicts: `{sessionId, startTime, repExtId, repName, direction,
  otherNumber, result, connected}`. `otherNumber` = `to.phoneNumber` for
  outbound, `from.phoneNumber` for inbound. Pure; testable.

### 2. `CallStore` (`src/redis_store.py`)

- `set_rep_metrics(metrics: dict[str, dict]) -> None` — JSON under `reps:metrics`
  (`{extId: {inboundToday, inboundWeek, outboundToday, outboundWeek}}`).
- `get_rep_metrics() -> dict` — read it back, `{}` if unset.
- `set_recent_calls(calls: list[dict]) -> None` — JSON list under `overview:recent`.
- `get_recent_calls() -> list` — read it back, `[]` if unset.

Same JSON-string storage as `reps:roster` (upstash/fakeredis compatible).

### 3. `run_metrics_worker(store, interval=60)` (`src/metrics_worker.py`)

Own JWT login (like `poll_for_recordings`). Every `interval` seconds:
compute `day_week_bounds` in `METRICS_TIMEZONE`; for each ext in
`Config.MONITORED_EXTENSIONS` fetch its week call-log once, keep the records, and
`summarize_call_counts` per ext → `store.set_rep_metrics(...)`. Then feed all the
fetched records to `build_recent_calls(..., roster=store.get_rep_roster())` →
`store.set_recent_calls(...)`. One fetch per rep feeds both outputs. On any
exception, log and **retain the last good metrics/recent** (do not overwrite with
empties). Wired into `run_local.py`'s `asyncio.gather` and `run_monitor.py`'s task
list, alongside the existing workers.

Lives in its own module `src/metrics_worker.py` (keeps `call_monitor.py` focused
on the telephony event stream).

### 4. API endpoint (`src/api/routes.py`)

`GET /api/calls/reps` — each rep object gains:
```json
"metrics": { "inboundToday": 5, "inboundWeek": 20,
             "outboundToday": 7, "outboundWeek": 31 }
```
Read from `store.get_rep_metrics()`; `null` when a rep has no metrics computed yet.

New route `GET /api/calls/recent` → `{"calls": [...]}` from
`store.get_recent_calls()` (`[]` when unset). Same auth'd `/api/calls` router.

### 5. Overview page (`src/api/static/overview.html`)

Each rep row shows a compact metrics line: `in 5·20 · out 7·31` (today·week),
muted "—" when `metrics` is null. Placed under the rep name/ext.

Below the rep list, a **Recent calls** section polls `GET /api/calls/recent`
(every ~5s) and renders rows: local time (America/Los_Angeles), rep name,
direction arrow, other-party number, and result — with missed/unhandled results
visually flagged. Empty state: "No recent calls".

## Data flow

```
metrics worker ── RC call-log per ext ──┬─ summarize per ext ─────> Redis reps:metrics
                                         └─ build_recent_calls ────> Redis overview:recent
                                                           │
overview.html ── poll /api/calls/reps  ──> reps + on-call + metrics → rep rows "in 5·20 · out 7·31"
              └─ poll /api/calls/recent ──> recent calls ──────────> "Recent calls" feed
```

## Error handling & edge cases

- RC login / fetch failure → log warning, keep last good `reps:metrics` and
  `overview:recent` (never overwrite with empties on a failed cycle).
- Rep with no calls in the window → zeros; recent feed simply has fewer rows.
- Pagination: rep exceeding one page in a week → follow pages while full.
- Metrics not yet computed (fresh start) → endpoint returns `metrics: null`,
  page shows "—".
- `START of week`/`today` computed once per refresh in `METRICS_TIMEZONE`, so the
  buckets roll over at local midnight / Monday.

## Testing

- `summarize_call_counts`: connected `Accepted`/`Call connected` counted; `Missed`,
  `Answered Elsewhere`, `Rejected`, `Hang Up` excluded; inbound vs outbound split;
  a record earlier this week counts in week-not-today; empty input → all zeros.
- `day_week_bounds`: a known UTC instant → correct LA start-of-today and most-recent
  Monday; one case across a US DST transition.
- `build_recent_calls`: merges across reps and sorts newest-first; dedups a shared
  `telephonySessionId` to the handled rep (Accepted beats Answered Elsewhere);
  includes missed calls; `otherNumber` picks `to` vs `from` by direction; respects
  `limit`.
- `CallStore` metrics and recent-calls round-trips (fakeredis).
- Endpoints: `/api/calls/reps` includes `metrics` (`null` when unset);
  `/api/calls/recent` returns stored calls (`{"calls": []}` when unset).
- Overview page: manual (numbers render with "—" before first refresh; recent
  feed lists calls, missed flagged).

## Out of scope (YAGNI)

Month / all-time windows, talk-time / duration, averages, charts, per-rep
timezones, non-monitored reps, recent-feed pagination / "load more" / click-through
to a transcript.
