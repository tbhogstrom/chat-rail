# Past-Session Transcript Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dropdown to the dashboard (and a global one on the overview page) that lists call sessions from the last hour and lets the user view a past session's stored transcript + sell-o-meter, read-only.

**Architecture:** The Node bridge indexes each session into a Redis sorted set `sessions:recent` (score = first-transcript time) the first time it writes a transcript chunk. A new Python endpoint reads that set, windows it to the last hour, hydrates labels from `call:{sid}:state`, and returns a list. The dashboard renders the list in a `<select>` and, on selection, "pins" the view to that session (fetching its transcript + sell-o-meter once, read-only); the overview page reuses the same list to deep-link into a pinned dashboard.

**Tech Stack:** Node + `@upstash/redis` (bridge), Python + FastAPI + `upstash_redis` (web), `fakeredis` + `pytest` (Python tests), `vitest` (bridge tests), vanilla JS (dashboard/overview static HTML).

## Global Constraints

- Recent window = **3600 seconds**, matching `CALL_TTL_SECONDS = 3600` (bridge) and the state/transcript TTLs. One hour, no more.
- Past-session view is **read-only**. No export/download; no retention beyond the existing 1-hour TTL.
- Reuse existing auth: the new endpoint lives under the `/api/calls` router which already has `dependencies=[Depends(verify_api_key)]` (cookie or `x-api-key`).
- Redis calls must work on BOTH `upstash_redis` (prod) and `fakeredis` (test): use `zadd(mapping)`, `zrevrangebyscore(key, max, min)` **without** `withscores`, `zscore`, `zremrangebyscore`, `smembers`. (Bridge writes via `@upstash/redis` `zadd(key, {nx:true}, {score, member})`.)
- Commit messages: **no `Co-Authored-By: Claude` trailer** (repo convention).
- Sorted-set key name is exactly `sessions:recent`. Score is epoch **milliseconds**.

---

### Task 1: Bridge indexes `sessions:recent` on first transcript

**Files:**
- Modify: `softphone-bridge/src/redis.ts` (`appendTranscript`, ~lines 8-13)
- Test: `softphone-bridge/src/redis.test.ts` (create)

**Interfaces:**
- Produces: `RECENT_SESSIONS_KEY = "sessions:recent"` (exported const); `appendTranscript(sessionId, chunk)` now also `ZADD sessions:recent NX <Date.now()> <sessionId>` on the first (empty→non-empty) chunk.

- [ ] **Step 1: Write the failing test**

Create `softphone-bridge/src/redis.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";

// In-memory fake of the two Redis calls appendTranscript uses, plus zadd.
const kv = new Map<string, string>();
const get = vi.fn(async (k: string) => kv.get(k) ?? null);
const set = vi.fn(async (k: string, v: string) => { kv.set(k, v); });
const zadd = vi.fn(async () => 1);

vi.mock("@upstash/redis", () => ({
  Redis: vi.fn(() => ({ get, set, zadd, lpush: vi.fn(), ltrim: vi.fn(), expire: vi.fn() })),
}));

const { appendTranscript, RECENT_SESSIONS_KEY } = await import("./redis.js");

describe("appendTranscript indexing", () => {
  beforeEach(() => { kv.clear(); zadd.mockClear(); get.mockClear(); set.mockClear(); });

  it("indexes sessions:recent on the FIRST chunk only", async () => {
    await appendTranscript("s-1", "hello");
    expect(zadd).toHaveBeenCalledTimes(1);
    expect(zadd).toHaveBeenCalledWith(
      RECENT_SESSIONS_KEY,
      { nx: true },
      { score: expect.any(Number), member: "s-1" },
    );

    await appendTranscript("s-1", "world");
    expect(zadd).toHaveBeenCalledTimes(1); // second chunk must NOT re-index
  });

  it("stores the joined transcript", async () => {
    await appendTranscript("s-2", "a");
    await appendTranscript("s-2", "b");
    expect(kv.get("call:s-2:transcript")).toBe("a b");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd softphone-bridge && npx vitest run src/redis.test.ts`
Expected: FAIL — `RECENT_SESSIONS_KEY` undefined / `zadd` not called.

- [ ] **Step 3: Implement the index write**

In `softphone-bridge/src/redis.ts`, add the exported const near `CALL_TTL_SECONDS` and update `appendTranscript`:

```ts
export const RECENT_SESSIONS_KEY = "sessions:recent";

export async function appendTranscript(sessionId: string, chunk: string): Promise<void> {
  const key = `call:${sessionId}:transcript`;
  const existing = (await redis.get<string>(key)) ?? "";
  const next = existing ? `${existing} ${chunk}`.trim() : chunk;
  await redis.set(key, next, { ex: CALL_TTL_SECONDS });
  if (!existing) {
    // First transcript chunk for this session — index it once so the dashboard
    // can list recently transcribed sessions. `nx` keeps the score pinned to the
    // first-transcript time even though appendTranscript runs on every final.
    await redis.zadd(RECENT_SESSIONS_KEY, { nx: true }, { score: Date.now(), member: sessionId });
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd softphone-bridge && npx vitest run src/redis.test.ts && npx tsc --noEmit`
Expected: PASS (2 tests) and tsc exit 0.

- [ ] **Step 5: Commit**

```bash
git add softphone-bridge/src/redis.ts softphone-bridge/src/redis.test.ts
git commit -m "feat(bridge): index sessions:recent on first transcript chunk"
```

---

### Task 2: Store `repExtId` on call state

**Files:**
- Modify: `src/call_monitor.py` (`process_telephony_event`, ~lines 72-98)
- Test: `tests/test_call_monitor.py` (add one test)

**Interfaces:**
- Produces: `call:{sid}:state` JSON now includes `repExtId` (the monitored party's extension id, or `None`). Consumed by Task 3's `?rep=` filter.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_call_monitor.py`:

```python
def test_call_data_includes_rep_ext_id(fake_redis):
    from src.redis_store import CallStore
    from src.call_monitor import process_telephony_event
    store = CallStore(fake_redis)
    event = {"body": {"telephonySessionId": "s-rep", "parties": [
        {"direction": "Inbound", "extensionId": "119",
         "status": {"code": "Answered"},
         "from": {"phoneNumber": "+15551234567"}, "to": {}},
    ]}}
    process_telephony_event(event, store, monitored_extensions={"119"})
    call = store.get_call("s-rep")
    assert call["repExtId"] == "119"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_call_monitor.py::test_call_data_includes_rep_ext_id -v`
Expected: FAIL — `KeyError: 'repExtId'`.

- [ ] **Step 3: Capture the monitored ext id and add it to `call_data`**

In `src/call_monitor.py`, inside `process_telephony_event`, extend the monitored-party loop to capture the ext id, then add it to `call_data`:

```python
    status = party.get("status", {}).get("code", "Unknown")
    rep_ext_id = None
    if monitored_extensions:
        for p in parties:
            p_ext = (p.get("extensionId")
                     or (p.get("to") or {}).get("extensionId")
                     or (p.get("from") or {}).get("extensionId"))
            if p_ext and p_ext in monitored_extensions:
                status = p.get("status", {}).get("code", status)
                rep_ext_id = p_ext
                break
```

Then add one key to the `call_data` dict (after `"rep_first_name": rep_first_name,`):

```python
        "rep_first_name": rep_first_name,
        # Monitored rep's extension id — lets the recent-sessions endpoint filter
        # by rep without guessing from activeExtIds.
        "repExtId": rep_ext_id,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_call_monitor.py -v`
Expected: PASS (new test + existing tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor.py tests/test_call_monitor.py
git commit -m "feat(monitor): record monitored repExtId on call state"
```

---

### Task 3: `CallStore.list_recent_sessions`

**Files:**
- Modify: `src/redis_store.py` (add method; ensure `datetime`/`timezone`/`json`/`time` imports)
- Test: `tests/test_redis_store.py` (add tests)

**Interfaces:**
- Produces: `CallStore.list_recent_sessions(rep: str | None = None, window_seconds: int = 3600) -> list[dict]`. Each row: `{"sessionId": str, "startTime": str|None (ISO), "live": bool, "state": dict}`. Newest-first, only sessions whose state still exists, filtered to `rep` when given. Consumed by Task 4.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_redis_store.py` (import `CallStore`, `json`, `time` at top if not present):

```python
import time, json
from src.redis_store import CallStore

def _seed(fake_redis, sid, score_ms, state):
    fake_redis.zadd("sessions:recent", {sid: score_ms})
    fake_redis.set(f"call:{sid}:state", json.dumps(state))

def test_list_recent_sessions_windows_and_orders(fake_redis):
    store = CallStore(fake_redis)
    now = int(time.time() * 1000)
    _seed(fake_redis, "s-old", now - 2 * 3600 * 1000, {"sessionId": "s-old"})  # >1h ago
    _seed(fake_redis, "s-a", now - 600 * 1000, {"sessionId": "s-a", "direction": "Inbound"})
    _seed(fake_redis, "s-b", now - 60 * 1000, {"sessionId": "s-b", "direction": "Outbound"})
    rows = store.list_recent_sessions()
    ids = [r["sessionId"] for r in rows]
    assert ids == ["s-b", "s-a"]              # newest first, s-old excluded
    assert rows[0]["startTime"] is not None
    assert rows[0]["state"]["direction"] == "Outbound"

def test_list_recent_sessions_filters_by_rep(fake_redis):
    store = CallStore(fake_redis)
    now = int(time.time() * 1000)
    _seed(fake_redis, "s-119", now - 30 * 1000, {"sessionId": "s-119", "repExtId": "119"})
    _seed(fake_redis, "s-118", now - 20 * 1000, {"sessionId": "s-118", "repExtId": "118"})
    rows = store.list_recent_sessions(rep="119")
    assert [r["sessionId"] for r in rows] == ["s-119"]

def test_list_recent_sessions_skips_expired_state(fake_redis):
    store = CallStore(fake_redis)
    now = int(time.time() * 1000)
    fake_redis.zadd("sessions:recent", {"s-gone": now - 10 * 1000})  # in set, no state
    rows = store.list_recent_sessions()
    assert rows == []

def test_list_recent_sessions_marks_live(fake_redis):
    store = CallStore(fake_redis)
    now = int(time.time() * 1000)
    _seed(fake_redis, "s-live", now - 10 * 1000, {"sessionId": "s-live"})
    fake_redis.sadd("calls:active", "s-live")
    rows = store.list_recent_sessions()
    assert rows[0]["live"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_redis_store.py -k list_recent_sessions -v`
Expected: FAIL — `AttributeError: 'CallStore' object has no attribute 'list_recent_sessions'`.

- [ ] **Step 3: Implement the method**

In `src/redis_store.py`, ensure the top imports include `import json`, `import time`, and `from datetime import datetime, timezone` (add any missing). Add the method to `CallStore`:

```python
    def list_recent_sessions(self, rep=None, window_seconds=3600):
        """Sessions that produced a transcript within the last `window_seconds`,
        newest-first. Rows: {sessionId, startTime(ISO|None), live, state}.
        Skips sessions whose state has expired; filters by rep when given."""
        now_ms = int(time.time() * 1000)
        min_ms = now_ms - window_seconds * 1000
        # Housekeeping: drop members older than the window so the set stays small.
        self.redis.zremrangebyscore("sessions:recent", 0, min_ms - 1)
        sids = self.redis.zrevrangebyscore("sessions:recent", "+inf", min_ms)
        if not sids:
            return []
        active = set(self.redis.smembers("calls:active"))
        rows = []
        for sid in sids:
            raw = self.redis.get(f"call:{sid}:state")
            if not raw:
                continue
            state = json.loads(raw) if isinstance(raw, str) else raw
            if rep is not None and state.get("repExtId") != rep:
                continue
            score = self.redis.zscore("sessions:recent", sid)
            start_iso = (
                datetime.fromtimestamp(score / 1000, tz=timezone.utc).isoformat()
                if score is not None else None
            )
            rows.append({
                "sessionId": sid,
                "startTime": start_iso,
                "live": sid in active,
                "state": state,
            })
        return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_redis_store.py -k list_recent_sessions -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/redis_store.py tests/test_redis_store.py
git commit -m "feat(store): list_recent_sessions windowed to the last hour"
```

---

### Task 4: `GET /api/calls/sessions/recent` endpoint

**Files:**
- Modify: `src/api/routes.py` (add route near `/recent`, ~line 146; uses existing `_caller_number` at :80, `get_store` at :24)
- Test: `tests/test_api.py` (add tests)

**Interfaces:**
- Consumes: `store.list_recent_sessions(rep, 3600)` (Task 3); `_caller_number(state)` (routes.py:80).
- Produces: `GET /api/calls/sessions/recent?rep=<extId?>` → `{"sessions": [ {sessionId, startTime, repExtId, repName, number, direction, status, live} ]}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
import time, json

@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_recent_sessions_lists_and_labels(client, store, fake_redis):
    now = int(time.time() * 1000)
    fake_redis.zadd("sessions:recent", {"s-1": now - 30 * 1000})
    fake_redis.set("call:s-1:state", json.dumps({
        "sessionId": "s-1", "direction": "Inbound", "status": "Disconnected",
        "repExtId": "119", "rep_first_name": "Alice",
        "from": {"phoneNumber": "+15551234567"}, "to": {},
    }))
    resp = client.get("/api/calls/sessions/recent", headers=auth_header())
    assert resp.status_code == 200
    rows = resp.json()["sessions"]
    assert len(rows) == 1
    assert rows[0]["sessionId"] == "s-1"
    assert rows[0]["repName"] == "Alice"
    assert rows[0]["number"] == "+15551234567"
    assert rows[0]["direction"] == "Inbound"

@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_recent_sessions_rep_filter(client, store, fake_redis):
    now = int(time.time() * 1000)
    for sid, ext in (("s-119", "119"), ("s-118", "118")):
        fake_redis.zadd("sessions:recent", {sid: now - 10 * 1000})
        fake_redis.set(f"call:{sid}:state", json.dumps({"sessionId": sid, "repExtId": ext}))
    rows = client.get("/api/calls/sessions/recent?rep=119", headers=auth_header()).json()["sessions"]
    assert [r["sessionId"] for r in rows] == ["s-119"]

@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_recent_sessions_empty(client):
    resp = client.get("/api/calls/sessions/recent", headers=auth_header())
    assert resp.status_code == 200
    assert resp.json()["sessions"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -k recent_sessions -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Add the route**

In `src/api/routes.py`, add this route (place it just before `@router.get("/latest")` so the static `/sessions/recent` path is registered among the collection routes):

```python
@router.get("/sessions/recent")
def get_recent_sessions(rep: str | None = None):
    store = get_store()
    rows = store.list_recent_sessions(rep=rep, window_seconds=3600)
    sessions = []
    for r in rows:
        state = r["state"]
        sessions.append({
            "sessionId": r["sessionId"],
            "startTime": r["startTime"],
            "repExtId": state.get("repExtId"),
            "repName": state.get("rep_first_name"),
            "number": _caller_number(state),
            "direction": state.get("direction"),
            "status": state.get("status"),
            "live": r["live"],
        })
    return {"sessions": sessions}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -k recent_sessions -v && python -m pytest -q`
Expected: PASS (3 new tests) and the full suite stays green.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes.py tests/test_api.py
git commit -m "feat(api): GET /api/calls/sessions/recent"
```

---

### Task 5: Dashboard dropdown + pinned mode + deep-link

**Files:**
- Modify: `src/api/static/dashboard.html` (header markup ~line 102; wrap fields form ~lines 123-164; script ~lines 171-377)

**Interfaces:**
- Consumes: `GET /api/calls/sessions/recent?rep=`, `GET /api/calls/{sid}/transcript`, `GET /api/calls/{sid}/sellometer`.

- [ ] **Step 1: Add the picker element and wrap the extract form**

In the header (line 102 area), add a `<select>` after `session-info`:

```html
    <div id="session-info">Waiting for call…</div>
    <select id="session-picker" title="View a recent session"></select>
```

Wrap the extracted-fields form so it can be hidden for past sessions. Change the block starting at `<h2>Caller Info</h2>` (line 123) through the closing `</div>` of `.actions` (line 164) by wrapping it:

```html
      <div id="extract-form">
        <h2>Caller Info</h2>
        <!-- …all existing .field blocks, Jobsite, and .actions unchanged… -->
        <div class="actions">
          <!-- …unchanged… -->
        </div>
      </div>
```

(Only add the opening `<div id="extract-form">` after `</div>` of `#sellometer` on line 122, and the matching closing `</div>` after the `.actions` div on line 164. Do not alter the inner markup.)

- [ ] **Step 2: Add pinned-mode state and functions to the script**

After `let currentSid = null;` (line 180) add:

```javascript
    let pinnedSid = null;                       // non-null => viewing a past session
    const initialSession = params.get("session"); // deep-link target, if any
```

Add these functions (place them just above the `// ---- polling ----` comment at line 297):

```javascript
    function fmtTime(iso) {
      if (!iso) return "";
      const d = new Date(iso);
      return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    }

    async function populateSessionPicker() {
      let rows = [];
      try {
        const r = await fetch(`/api/calls/sessions/recent?rep=${encodeURIComponent(repId)}`,
          { headers: authHeaders() });
        if (r.ok) rows = (await r.json()).sessions || [];
      } catch (_) { /* keep last options on a transient error */ }
      const sel = document.getElementById("session-picker");
      const keep = pinnedSid || "live";
      sel.innerHTML = "";
      const liveOpt = new Option("🔴 Live — current call", "live");
      sel.add(liveOpt);
      for (const s of rows) {
        const label = `${fmtTime(s.startTime)} · ${s.direction || "?"} · ${s.number || "unknown"}`
          + (s.live ? " (live)" : "");
        sel.add(new Option(label, s.sessionId));
      }
      sel.value = keep;
      if (sel.value !== keep) sel.value = "live"; // pinned session aged out of the window
    }

    async function pinSession(sid) {
      pinnedSid = sid;
      document.getElementById("extract-form").hidden = true;
      lastHighlights = [];  // past view shows plain transcript, no live field highlights
      try {
        const tr = await fetch(`/api/calls/${encodeURIComponent(sid)}/transcript`,
          { headers: authHeaders() });
        lastTranscript = tr.ok ? ((await tr.json()).transcript || "") : "";
        if (tr.status === 404) lastTranscript = "";
        document.getElementById("transcript-body").textContent =
          tr.status === 404 ? "Transcript expired." : "";
      } catch (_) { lastTranscript = ""; }
      lastRenderedTranscript = null;  // force redraw
      renderTranscript();
      document.getElementById("session-info").textContent = `Viewing past session • ${sid}`;
      const smEl = document.getElementById("sellometer");
      try {
        const sm = await fetch(`/api/calls/${encodeURIComponent(sid)}/sellometer`,
          { headers: authHeaders() });
        if (sm.ok) renderSellometer(await sm.json());
        else smEl.hidden = true;
      } catch (_) { smEl.hidden = true; }
    }

    function goLive() {
      pinnedSid = null;
      document.getElementById("extract-form").hidden = false;
      currentSid = null;
      lastTranscript = "";
      lastRenderedTranscript = null;
      renderTranscript();
      pollCall();
    }
```

- [ ] **Step 3: Guard the pollers and wire the picker**

At the very top of `pollCall`, `pollExtracted`, and `pollSellometer`, add a pinned guard as the first line inside each `try`/function body:

```javascript
    async function pollCall() {
      if (pinnedSid) return;   // pinned to a past session — don't follow the live call
      try {
```
```javascript
    async function pollExtracted() {
      if (pinnedSid || !currentSid) return;
```
```javascript
    async function pollSellometer() {
      if (pinnedSid || !currentSid) return;
```

Replace the bottom bootstrap block (lines 374-377) with:

```javascript
    document.getElementById("session-picker").addEventListener("change", (e) => {
      const v = e.target.value;
      if (v === "live") goLive();
      else pinSession(v);
    });

    setInterval(pollCall, 2000);
    setInterval(pollExtracted, 3000);
    setInterval(pollSellometer, 3000);
    setInterval(populateSessionPicker, 10000);
    populateSessionPicker();
    if (initialSession) pinSession(initialSession);
    else pollCall();
```

- [ ] **Step 4: Manual verification**

Run the app locally (`python -m uvicorn src.api.main:app --reload` with a store, or use the `run`/`verify` skill) and, using a `fakeredis`/dev store or a real recent call, seed one `sessions:recent` entry + matching `call:{sid}:state` and `call:{sid}:transcript`. Then:
- Load `/dashboard?rep=<extId>`: the dropdown lists "🔴 Live" + the recent session; live polling still works.
- Select the past session: transcript shows read-only, the Caller-Info form hides, the sell-o-meter shows its stored score, and the live poll no longer overwrites the view.
- Select "🔴 Live": the form returns and live following resumes.
- Load `/dashboard?rep=<extId>&session=<sid>`: it boots straight into the pinned view.
- Confirm: `python -m pytest -q` and `cd softphone-bridge && npx tsc --noEmit` still pass (no backend regressions).

- [ ] **Step 5: Commit**

```bash
git add src/api/static/dashboard.html
git commit -m "feat(dashboard): recent-session dropdown with read-only pinned view"
```

---

### Task 6: Overview global recent-transcripts dropdown

**Files:**
- Modify: `src/api/static/overview.html` (add a dropdown near the top + a small script block)

**Interfaces:**
- Consumes: `GET /api/calls/sessions/recent` (no `rep` → all reps). Navigates to `/dashboard?rep={repExtId}&session={sessionId}`.

- [ ] **Step 1: Add the dropdown markup**

Near the top of the overview `<body>` (before or above the recent-calls table), add:

```html
  <div id="recent-transcripts-bar">
    <label for="recent-transcripts">Recent transcripts (last hour):</label>
    <select id="recent-transcripts">
      <option value="">— select a call —</option>
    </select>
  </div>
```

- [ ] **Step 2: Add the populate + navigate script**

Add before `</body>` (this page already uses the shared-password cookie, so no auth header needed):

```html
  <script>
    async function loadRecentTranscripts() {
      let rows = [];
      try {
        const r = await fetch("/api/calls/sessions/recent",
          { headers: { "content-type": "application/json" } });
        if (r.ok) rows = (await r.json()).sessions || [];
      } catch (_) { return; }
      const sel = document.getElementById("recent-transcripts");
      sel.length = 1;  // keep the placeholder
      for (const s of rows) {
        const t = s.startTime ? new Date(s.startTime)
          .toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
        const label = `${t} · ${s.repName || "?"} · ${s.direction || "?"} · ${s.number || "unknown"}`;
        const opt = new Option(label, "");
        opt.dataset.rep = s.repExtId || "";
        opt.dataset.sid = s.sessionId;
        sel.add(opt);
      }
    }
    document.getElementById("recent-transcripts").addEventListener("change", (e) => {
      const opt = e.target.selectedOptions[0];
      if (!opt || !opt.dataset.sid) return;
      const rep = encodeURIComponent(opt.dataset.rep || "");
      const sid = encodeURIComponent(opt.dataset.sid);
      location.href = `/dashboard?rep=${rep}&session=${sid}`;
    });
    loadRecentTranscripts();
    setInterval(loadRecentTranscripts, 15000);
  </script>
```

- [ ] **Step 3: Manual verification**

Load `/` (overview): the "Recent transcripts (last hour)" dropdown lists all reps' recent sessions; picking one navigates to `/dashboard?rep=…&session=…` and opens the pinned view from Task 5.

- [ ] **Step 4: Commit**

```bash
git add src/api/static/overview.html
git commit -m "feat(overview): global recent-transcripts dropdown deep-links into pinned dashboard"
```

---

## Self-Review

**Spec coverage:**
- Per-rep dashboard dropdown → Task 5. Global view → Task 6. ✓
- Index on first transcript → Task 1. Windowing/prune/labels → Task 3. Endpoint → Task 4. `repExtId` filter → Task 2. ✓
- Pinned read-only transcript + sell-o-meter, hide extract form → Task 5. Deep-link `?session=` → Tasks 5 & 6. ✓
- 1-hour window, read-only, existing auth → Global Constraints + Tasks 3/4. ✓
- Edge cases (empty window, transcript expired 404, no sell-o-meter, live flag, expired state skipped) → Tasks 3/4 tests + Task 5 pin logic. ✓

**Placeholder scan:** none — every step has concrete code/commands.

**Type consistency:** `sessions:recent` key, ms scores, `list_recent_sessions(rep, window_seconds)` row shape `{sessionId, startTime, live, state}`, and the endpoint JSON `{sessionId, startTime, repExtId, repName, number, direction, status, live}` are used identically across Tasks 1/3/4/5/6. `RECENT_SESSIONS_KEY` and `_caller_number` referenced with their real names. ✓
