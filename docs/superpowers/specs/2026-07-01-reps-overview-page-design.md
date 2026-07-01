# Reps Overview Page — Design

**Date:** 2026-07-01
**Status:** Approved

## Problem

The call bridge has no discovery surface. To view a rep's transcript you must
open `/dashboard?rep=<extensionId>` and already know the extension ID. There is
no page that answers "who are my reps?" or "who is on a call right now?".

## Goal

An overview page (the app landing route `/`) that lists the monitored reps,
shows which ones are on an active call, and lets you click any rep to open their
dashboard (idle → their latest call; on a call → the live transcript).

## Scope decision

List **monitored reps only** — the reps in `MONITORED_EXTENSIONS`, which are the
ones the softphone bridge supervises and therefore the only ones with a live
transcript. Everyone shown has a viewable transcript. Non-monitored extensions
(queues, IVR, shared lines) are intentionally excluded.

Current monitored set (RC extension IDs):

| Rep            | extId       | ext # |
|----------------|-------------|-------|
| Travis Watters | 715449052   | 121   |
| Vince Rodas    | 731501052   | 122   |
| Doug Stoker    | 576959052   | 119   |
| Jacob Hair     | 442845052   | 118   |

## Roster source

The API needs each monitored rep's **name + extension number**. Those exist only
inside the monitor's startup snapshot from RingCentral
(`_load_ext_display_map` / `_load_ext_number_map`), not in Redis or Config.

**Chosen:** the monitor persists the monitored reps' `{name, number}` to Redis
after loading its ext maps; the API reads Redis. This adds no per-request RC
calls and works identically for local (`run_local.py`) and the separate Vercel
API process.

Rejected: (a) API calls RingCentral on every poll — RC latency/rate-limit on a
page polling every 3s; (b) hardcode names in Config — drifts from RC.

## Components

### 1. `CallStore` (`src/redis_store.py`)

- `set_rep_roster(roster: dict[str, dict]) -> None` — store `reps:roster` as a
  Redis hash of `extId -> json({"name","number"})`.
- `get_rep_roster() -> dict[str, dict]` — read it back (empty dict if unset).

Uses `hset`/`hgetall`, consistent with the existing string/set usage; works with
both `upstash-redis` and `fakeredis`.

### 2. Monitor (`src/call_monitor.py`)

In `run_monitor`, after loading `ext_number_map` and `ext_display_map`, build a
roster limited to `Config.MONITORED_EXTENSIONS`:

```python
roster = {
    ext_id: {
        "name": ext_display_map.get(ext_id),
        "number": ext_number_map.get(ext_id),
    }
    for ext_id in Config.MONITORED_EXTENSIONS
}
store.set_rep_roster(roster)
```

Runs on every reconnect, so name/number changes in RC propagate.

### 3. API endpoint (`src/api/routes.py`)

`GET /api/calls/reps` on the existing auth'd `/api/calls` router. For each
monitored rep: read the roster entry and `get_rep_current_call(extId)`, decide
`onCall` (the pointer's `sessionId` is in `calls:active`), and return:

```json
{ "reps": [
  { "extId": "576959052", "name": "Doug Stoker", "number": "119",
    "onCall": true, "status": "Answered", "direction": "Inbound",
    "sessionId": "s-...", "callerNumber": "+1512..." }
] }
```

Reps are returned in `MONITORED_EXTENSIONS` order. `callerNumber` is the `from`
number for inbound / `to` number for outbound, best-effort (may be null).

### 4. Overview page (`src/api/static/overview.html`)

New static page served at `/` (dashboard stays at `/dashboard`). Reuses the
dashboard's dark theme and the same `localStorage` `sfw-bridge-key` used as the
`x-api-key` header. Polls `/api/calls/reps` every 3s and renders a rep list:

- status dot (green = on call, muted = idle),
- rep name + extension number,
- for active calls, the caller number and call status,
- each row links to `/dashboard?rep=<extId>`.

`src/api/main.py` gets a `/` route returning `FileResponse(overview.html)`.

## Data flow

```
monitor startup ── loads RC ext maps ──> store.set_rep_roster() ──> Redis hash reps:roster
                                                                         │
overview.html ── poll GET /api/calls/reps ──> endpoint reads roster + rep pointers + calls:active
                                               └─> [{name, number, onCall, ...}]
row click ──> /dashboard?rep=<extId>   (existing page, unchanged)
```

## Error handling & edge cases

- **Roster not yet written** (monitor starting / RC down) → endpoint returns
  `{"reps": []}`; page shows "Waiting for roster…" instead of erroring.
- **Monitored ext missing from roster** (map lacked that ext) → still listed by
  extId with a placeholder name, never silently dropped.
- **Rep pointer → call no longer in `calls:active`** → shown as idle (it is the
  rep's latest call, not an active one).

## Testing

- `CallStore` roster round-trip with fakeredis.
- Endpoint: monitored rep on an active call → `onCall: true`; idle rep →
  `onCall: false`; empty roster → `{"reps": []}`. Follows the existing `tests/`
  patterns.

## Out of scope (YAGNI)

Call durations/timers, non-monitored reps, historical call lists, search/filter.
Addable later if wanted.
