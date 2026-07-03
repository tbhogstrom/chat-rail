# Sell-o-meter — Design

**Date:** 2026-07-03
**Status:** Draft — awaiting user review

## Purpose

A live, points-based gauge on the agents dashboard (`/dashboard?rep=<extId>`) that fills up as a live call transcript passes sales checkpoints: caller name captured, phone captured, email captured, sales script started, service agreement generator started. The score must be readable by other consumers (supervisor/home dashboard) in near real time, and final scores must be persisted per rep for later analysis (e.g. future "power phrase" correlation work).

## Decisions made with the user

- **Audience:** live gauge on the rep dashboard now; score data must live server-side so it can be pushed to supervisor/home dashboards in near real time. Final numbers stored per rep (JSON is fine).
- **Script/agreement checkpoints:** a dashboard button click counts as hitting the checkpoint (the system cannot see inside the external Claude projects).
- **Config:** checkpoints and point values live in a JSON config file in the repo — edit + redeploy to tune; extensible to new checkpoint types (e.g. power phrases) later.
- **Output shape:** the sell-o-meter output for a call is an **array of scores, one per minute the call is live** (a score timeline), plus the current running score. The timeline is what gets persisted per rep — it captures *when* in the call checkpoints landed, which is the raw material for the future power-phrase/pacing analysis.
- **Where scoring runs:** the Fly engine's extraction worker (chosen by Claude while user was away — it is the only always-on process, already cycles every 3s per active call, and already owns call-end handling; this makes Redis the single source of truth any dashboard can read).

## Architecture

```
dashboard button click ──POST /api/calls/{sid}/events──▶ Redis call:{sid}:events
transcript (bridge)    ──▶ Redis call:{sid}:transcript
                                    │
              engine extraction worker (every 3s)
                                    │
        extracted fields + events + sellometer.json config
                                    │
                        compute_score() (pure function)
                                    ▼
                       Redis call:{sid}:sellometer  ◀──GET /api/calls/{sid}/sellometer── any dashboard
                                    │
                on call end (after 60s extraction grace)
                                    ▼
                       Redis sellometer:history:{extId}  (list of final-score JSON records)
```

## Components

### 1. Scoring config — `src/sellometer.json`

Loaded by both the engine and the API (packaged with `src/`). v1 shape:

```json
{
  "version": 1,
  "checkpoints": [
    { "id": "caller-name", "label": "Name",       "points": 10, "detect": { "type": "extracted_field", "field": "firstname" } },
    { "id": "phone",       "label": "Phone",      "points": 15, "detect": { "type": "extracted_field", "field": "phone" } },
    { "id": "email",       "label": "Email",      "points": 15, "detect": { "type": "extracted_field", "field": "email" } },
    { "id": "sales-script","label": "Sales Script","points": 25, "detect": { "type": "event", "event": "sales-script-opened" } },
    { "id": "agreement",   "label": "Agreement",   "points": 35, "detect": { "type": "event", "event": "agreement-opened" } }
  ]
}
```

Point values are first-guess defaults (total 100); tune by editing the file. New detect types (e.g. `{"type": "phrase", "patterns": [...]}` scanning the transcript for power phrases) are added by extending the detect-type registry in `src/sellometer.py` — the config schema doesn't change.

### 2. Scoring module — `src/sellometer.py`

Pure function, no I/O:

```python
compute_score(config, extracted: dict, events: dict[str, float], transcript: str = "") -> dict
```

Returns the current-cycle result: `{ "score": int, "max": int, "checkpoints": [{id, label, points, hit, ts}] }`. A checkpoint is `hit` when its detect rule matches (extracted field non-empty, or event id present). `ts` is the event timestamp for event checkpoints, null otherwise (extracted fields don't carry timestamps today; acceptable for v1). Unknown detect types are skipped with a log warning so an old engine doesn't crash on a newer config.

The worker (not the pure function) maintains the per-minute timeline around this result — see §4.

### 3. Call events — Redis + API

- New `CallStore` methods in `src/redis_store.py`: `add_call_event(sid, event_id)` (hash `call:{sid}:events`, field = event id, value = first-hit timestamp; idempotent — first click wins) and `get_call_events(sid)`. TTL matches other call keys.
- New endpoint `POST /api/calls/{sid}/events` body `{"event": "<id>"}` in `src/api/routes.py`, same auth as the other `/api/calls/*` endpoints. Rejects event ids not present in the config (400) to keep the event space clean.

### 4. Engine integration — `src/extraction_worker.py`

Each cycle, after writing `extracted` for a session, the worker also reads `call:{sid}:events`, calls `compute_score`, and writes `call:{sid}:sellometer` (new `CallStore.set_sellometer`/`get_sellometer`). Update latency is bounded by the existing 3s cycle.

**Per-minute timeline.** The stored sellometer JSON is:

```json
{
  "score": 65, "max": 100,
  "checkpoints": [ { "id": "email", "label": "Email", "points": 15, "hit": true, "ts": "..." }, ... ],
  "startedAt": "2026-07-03T14:10:02Z",
  "timeline": [ 0, 10, 25, 25 ],
  "updatedAt": "2026-07-03T14:14:31Z"
}
```

- `startedAt` is set on the worker's first compute for the session and never changes (self-contained; no dependency on call-state timestamps, which don't exist today).
- `timeline[i]` is the score at the end of call minute `i`. Each cycle the worker computes `elapsed = floor((now − startedAt) / 60s)`; while `len(timeline) < elapsed` it appends the current score, so each minute boundary is captured within one 3s cycle of it passing, and gaps (e.g. a stalled worker) are back-filled with the score known at catch-up time. The live `score` field covers the in-progress minute.

**Finalization:** when a session leaves the active set and its 60s extraction grace window expires (the point where the worker stops processing it today), the engine appends the final score as the last timeline entry and writes a final record to `sellometer:history:{extId}` (Redis list, `LPUSH` + `LTRIM` to 500):

```json
{ "sessionId": "...", "repExtId": "...", "score": 65, "max": 100,
  "checkpoints": [...], "startedAt": "...", "timeline": [0, 10, 25, 25, 65],
  "endedAt": "2026-07-03T14:22:05Z" }
```

The timeline array — one score per live minute of the call — is the primary analytical output ("array of sell-o-meter scores for the call").

`extId` comes from the call state's `activeExtIds` (same source the rep pointer uses). This is the "final numbers by rep, JSON for now" store.

### 5. Read API — `src/api/routes.py`

- `GET /api/calls/{sid}/sellometer` → the live sellometer JSON (404/empty until first compute). This is the one interface any future supervisor/home dashboard consumes.
- `GET /api/reps/{extId}/sellometer/history?limit=50` → recent final records for a rep (feeds future analysis).

### 6. Dashboard UI — `src/api/static/dashboard.html`

- A meter block at the top of the right column: horizontal fill bar + big score number (`65 / 100`), and one chip per checkpoint reusing the existing status-dot pattern (grey → green with points label, e.g. `● Email +15`). The timeline array is available in the same payload for a future sparkline, but v1 renders only the bar + chips.
- Polling: piggyback the existing 3s `pollExtracted` interval — fetch `/api/calls/{sid}/sellometer` alongside it. No new intervals, no SSE (matches the page's existing polling model).
- `openInClaude()` gains a fire-and-forget `POST /api/calls/{sid}/events` when the clicked tool maps to a checkpoint event. Mapping: add an optional `event` field to `Config.CLAUDE_TOOLS` entries (`sales-script-opened`, `agreement-opened`) so the button → event link is explicit config, not name matching. The in-repo `/agreement` tool flow is out of scope for v1 (the checkpoint is the button click).

## Error handling

- Config file missing/invalid → engine logs an error and skips sellometer computation; dashboard hides the meter when the endpoint returns empty. The rest of the pipeline is unaffected.
- Events endpoint is idempotent; duplicate clicks don't change the score or timestamp.
- Redis unavailability follows the existing worker behavior (cycle errors are logged and retried next tick).

## Testing

- Unit tests for `compute_score` in `tests/` (pure function: empty inputs, partial checkpoints, all hit, unknown detect type, event timestamps).
- Unit tests for the timeline append logic (minute boundary crossing, multi-minute gap back-fill, finalization appends last score) — factored as a small pure helper so it's testable without Redis.
- Unit test for config loading/validation.
- Manual end-to-end verify on a live/simulated call: dots fill, score climbs, button click adds points, final record lands in `sellometer:history:{extId}`.

## Out of scope (future)

- Power-phrase detection (new `detect.type: "phrase"` — config schema already accommodates it).
- Supervisor/home dashboard rendering (it will read the same `GET /api/calls/{sid}/sellometer`).
- Runtime-editable scoring config; per-rep score analytics UI; pushing scores into HubSpot.
