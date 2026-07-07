# Past-session transcript picker — design

**Date:** 2026-07-07
**Status:** Approved, ready for implementation plan

## Problem

The sales team wants to review transcripts of recent calls, not just the one live
call. Today the dashboard (`/dashboard?rep={extId}`) only ever shows the rep's
*current* call — it polls `/api/calls/latest?rep=` and follows whatever session is
live. There is no way to look back at a call that just ended.

## Goal (scope)

- A **dropdown** on the dashboard listing that rep's sessions **from the last hour**.
- A **global** recent-sessions view (all reps) as a second entry point.
- Selecting a past session shows its **transcript + sell-o-meter**, **read-only**.
- The 1-hour window matches the existing transcript TTL. **No export, no retention
  beyond one hour** — those were explicitly out of scope.

## Non-goals

- Persisting transcripts longer than the existing 1-hour Redis TTL.
- Exporting / downloading / copying transcripts.
- Showing the live extracted-fields form for past sessions.
- Any per-user identity (the app keeps its single shared-password gate).

## Current-state facts (from exploration)

- Dashboard is per-rep via `?rep=`; polls `GET /api/calls/latest?rep=` every 2s and
  follows the live call (`src/api/static/dashboard.html`).
- **A transcript-by-id endpoint already exists**: `GET /api/calls/{session_id}/transcript`
  and `GET /api/calls/{session_id}/sellometer` (`src/api/routes.py:185`, `:219`).
- Transcripts live in `call:{sid}:transcript` (TTL 3600s), written by the **Node
  bridge** `appendTranscript` (`softphone-bridge/src/redis.ts`). Call state lives in
  `call:{sid}:state` (`src/redis_store.py`, `store_call` at `:16`), TTL 3600s on
  completion; it holds `direction`, `from`/`to`, `rep_first_name`, `activeExtIds`,
  `status`, `lastEventAt`.
- **There is no time-indexed list of recent sessions.** `calls:active` is
  live-only; `overview:recent` is RC-call-log-sourced, capped at 15, and includes
  calls that were never transcribed.
- Auth: shared-password cookie or `x-api-key`, enforced on all `/api/calls/*` via
  `Depends(verify_api_key)` (`src/api/auth.py`). The new endpoint reuses it.
- Redis clients confirmed to support the needed commands: Python `zadd`,
  `zrevrangebyscore`, `zremrangebyscore`, `zscore`; Node `zadd` with `{ nx: true }`.

## Architecture

Three coordinated pieces: a new **index** (who wrote it: the bridge), a new **list
endpoint** (Python), and **frontend** changes (dashboard + overview).

### 1. Index — `sessions:recent` (Redis sorted set)

- **Member** = `sessionId`, **score** = first-transcript timestamp (epoch ms).
- **Written by the Node bridge** in `appendTranscript` (`softphone-bridge/src/redis.ts`):
  on the **first** chunk for a session (i.e. when the existing transcript is empty),
  `ZADD sessions:recent { nx: true } <now_ms> <sessionId>`. `nx` keeps the score
  pinned to the first-transcript time even though `appendTranscript` runs on every
  final.
  - *Rationale:* indexing on first transcript (not on call creation) guarantees the
    list contains **only sessions that actually have a transcript** — no empty
    dropdown entries. The score doubles as the call's start-ish time for labels.
- **No TTL on the sorted set itself** (sorted sets can't expire per-member); the
  window + prune below bound it.

### 2. List endpoint — `GET /api/calls/sessions/recent`

Under the existing authed `/api/calls` router.

Query params: `rep` (optional extId filter).

Logic (new `CallStore.list_recent_sessions(rep=None, window_seconds=3600)` in
`src/redis_store.py`, plus a thin route in `src/api/routes.py`):
1. `now_ms = time.time()*1000`; `min_ms = now_ms - window_seconds*1000`.
2. `ZREMRANGEBYSCORE sessions:recent 0 (min_ms-1)` — opportunistic prune.
3. `sids = ZREVRANGEBYSCORE sessions:recent +inf min_ms WITHSCORES` — newest-first,
   in-window, with the start-time scores.
4. Pipeline/`MGET` `call:{sid}:state` for those sids.
5. Build one row per sid:
   `{ sessionId, startTime (from score, ISO), repExtId, repName, number, direction,
      status, live }` where `live = sid ∈ calls:active`, `number` via the existing
   `_caller_number` helper, `repName` from `rep_first_name`/roster.
6. If `rep` given, keep only rows whose `repExtId == rep`.
7. Return newest-first JSON list. Rows whose state has expired are skipped.

### 3. `repExtId` on call state

Add `repExtId` to `call_data` in `src/call_monitor.py` — the monitored party's ext
id, captured at the same place `rep_first_name` is resolved (`call_monitor.py:73-82`).
This lets the `?rep=` filter match precisely instead of guessing from `activeExtIds`.

### 4. Frontend — dashboard (`src/api/static/dashboard.html`)

- New `<select id="session-picker">` in the transcript panel header, refreshed every
  ~10s from `/api/calls/sessions/recent?rep={repId}`:
  - First option **"🔴 Live — current call"** (`value="live"`, default).
  - One option per past session, newest-first: `2:47 PM · Inbound · (555) 123-4567`.
    The currently-live session is tagged `(live)`.
- New state `pinnedSid` (null = live):
  - **Live mode** (default): unchanged — `pollCall` follows the current call;
    extracted-fields + sell-o-meter poll live.
  - **Pinned mode** (a past session picked): set `pinnedSid`; the live auto-follow
    stops (`pollCall` must not overwrite `currentSid` while pinned); fetch the
    session **once** — `GET /api/calls/{sid}/transcript` and `.../sellometer` — and
    render both **read-only**; hide the live extracted-fields form; header shows the
    session's time/direction/number. Choosing "Live" clears `pinnedSid` and resumes.
- **Deep-link:** on load, `?session={sid}` boots straight into pinned mode for that
  sid. This is the single mechanism both entry points share.

### 5. Frontend — overview (`src/api/static/overview.html`), global view

- A compact **"Recent transcripts (last hour)"** dropdown at the top, populated from
  `/api/calls/sessions/recent` (no `rep` → all reps), labeled
  `2:47 PM · Alice · Inbound · (555) 123-4567`.
- Selecting one navigates to `/dashboard?rep={repExtId}&session={sid}` — it just
  launches the pinned dashboard. No separate viewer.

## Data flow

```
Node bridge: first transcript chunk ─ZADD nx→ sessions:recent (sid @ startTime)
                                     ─SET──→ call:{sid}:transcript (TTL 1h)
Python monitor: ──SET──→ call:{sid}:state {..., repExtId}  (TTL 1h on complete)

Dashboard dropdown ─GET /api/calls/sessions/recent?rep=X→ [rows]
  pick past sid → GET /api/calls/{sid}/transcript  (read-only)
                → GET /api/calls/{sid}/sellometer   (read-only)
Overview dropdown ─GET /api/calls/sessions/recent→ [rows] → link /dashboard?rep&session
```

## Error handling / edge cases

- **Empty window:** dropdown shows only "Live" (dashboard) / "No recent transcripts"
  (overview).
- **Transcript expired mid-view** (endpoint 404): show "Transcript expired."
- **No sell-o-meter data** for the session: that panel blanks.
- **Pinned to a still-live call:** allowed; it's a static snapshot until the user
  switches back to Live.
- **State expired but sid still in the sorted set:** row skipped; prune removes it.
- **Prune concurrency:** `ZREMRANGEBYSCORE` is idempotent.

## Testing

- **Python** (`CallStore.list_recent_sessions` + route): windowing excludes >1h,
  `?rep=` filter, newest-first ordering, `live` flag, label hydration, empty case,
  state-expired-row skipped. Use the existing store test patterns / fake redis.
- **Bridge** (`redis.ts`): `appendTranscript` calls `ZADD nx` exactly once — only on
  the first (empty→non-empty) chunk, not on subsequent appends.
- **Frontend:** manual verification — pin/unpin, deep-link `?session=`, read-only
  rendering, overview→dashboard navigation.

## Files touched

- `softphone-bridge/src/redis.ts` — index write in `appendTranscript` (+ test).
- `src/redis_store.py` — `list_recent_sessions()`, sorted-set helpers.
- `src/call_monitor.py` — add `repExtId` to `call_data`.
- `src/api/routes.py` — `GET /api/calls/sessions/recent`.
- `src/api/static/dashboard.html` — dropdown + pinned mode + deep-link.
- `src/api/static/overview.html` — global recent-transcripts dropdown.
- Tests alongside the above.
