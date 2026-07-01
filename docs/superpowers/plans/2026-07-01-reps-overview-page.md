# Reps Overview Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an overview landing page at `/` that lists the monitored reps, shows who is on an active call, and links each rep to their dashboard.

**Architecture:** The monitor persists the monitored reps' `{name, number}` to Redis at startup (it already loads these from RingCentral). A new authed endpoint `GET /api/calls/reps` reads that roster plus active-call state and returns a per-rep list. A new static page `overview.html` (served at `/`) polls the endpoint every 3s and renders the list, each row linking to the existing `/dashboard?rep=<extId>`.

**Tech Stack:** Python 3.12+, FastAPI, upstash-redis / fakeredis, vanilla HTML+JS (matching `dashboard.html`).

## Global Constraints

- Python `>=3.12` (repo runs on 3.14).
- All `CallStore` methods must work with **both** `upstash-redis` and `fakeredis` — use only `set`/`get`/`sadd`/`smembers`/`expire`-style calls with positional args; do **not** use `hset`/`hgetall` (their keyword signatures differ between the two clients).
- Reuse the existing auth: the `/api/calls` router already depends on `verify_api_key` (`x-api-key` header). The page reuses the `localStorage` key `sfw-bridge-key`.
- Roster is **monitored reps only** — `Config.MONITORED_EXTENSIONS`, in that order.
- Landing route is `/`. `/dashboard` and `/agreement` stay as-is.
- RC display names are the source of truth (e.g. "Travis Watters", "Doug Stoker").
- Commit messages: conventional-commit style, **no** `Co-Authored-By: Claude` trailer.

---

### Task 1: `CallStore` roster round-trip

**Files:**
- Modify: `src/redis_store.py` (add two methods at end of class)
- Test: `tests/test_redis_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `CallStore.set_rep_roster(roster: dict[str, dict]) -> None` — persists `roster` as JSON under key `reps:roster`.
  - `CallStore.get_rep_roster() -> dict[str, dict]` — returns the roster, or `{}` if unset.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_redis_store.py`:

```python
def test_set_and_get_rep_roster(store):
    roster = {"119": {"name": "Doug Stoker", "number": "119"}}
    store.set_rep_roster(roster)
    assert store.get_rep_roster() == roster


def test_get_rep_roster_empty_returns_empty_dict(store):
    assert store.get_rep_roster() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_redis_store.py::test_set_and_get_rep_roster tests/test_redis_store.py::test_get_rep_roster_empty_returns_empty_dict -v`
Expected: FAIL with `AttributeError: 'CallStore' object has no attribute 'set_rep_roster'`

- [ ] **Step 3: Implement the methods**

Append inside the `CallStore` class in `src/redis_store.py` (after `set_rep_pointer`):

```python
    def set_rep_roster(self, roster: dict[str, dict]) -> None:
        """Persist the monitored-rep roster ({extId: {"name", "number"}}).

        Stored as a single JSON string (not a hash) so it round-trips
        identically on upstash-redis and fakeredis.
        """
        self.redis.set("reps:roster", json.dumps(roster))

    def get_rep_roster(self) -> dict:
        """Return the monitored-rep roster, or {} if not yet written."""
        raw = self.redis.get("reps:roster")
        if raw is None:
            return {}
        return json.loads(raw)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_redis_store.py -v`
Expected: PASS (all, including the two new tests)

- [ ] **Step 5: Commit**

```bash
git add src/redis_store.py tests/test_redis_store.py
git commit -m "feat(store): persist and read monitored-rep roster"
```

---

### Task 2: `build_monitored_roster` helper + wire into monitor

**Files:**
- Modify: `src/call_monitor.py` (add helper; call it in `run_monitor`)
- Test: `tests/test_call_monitor.py`

**Interfaces:**
- Consumes: `CallStore.set_rep_roster` (Task 1).
- Produces:
  - `call_monitor.build_monitored_roster(display_map: dict, number_map: dict, monitored: list[str]) -> dict[str, dict]` — returns `{extId: {"name", "number"}}` for each ext in `monitored`, pulling name/number from the maps (`None` when absent).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_call_monitor.py`:

```python
from src.call_monitor import build_monitored_roster


def test_build_monitored_roster_filters_to_monitored():
    display = {"119": "Doug Stoker", "121": "Travis Watters", "200": "IVR"}
    numbers = {"119": "119", "121": "121", "200": "200"}
    roster = build_monitored_roster(display, numbers, ["119", "121"])
    assert roster == {
        "119": {"name": "Doug Stoker", "number": "119"},
        "121": {"name": "Travis Watters", "number": "121"},
    }


def test_build_monitored_roster_handles_missing_maps():
    roster = build_monitored_roster({}, {}, ["119"])
    assert roster == {"119": {"name": None, "number": None}}
```

(If `test_call_monitor.py` already imports from `src.call_monitor`, add `build_monitored_roster` to that existing import instead of adding a second import line.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_call_monitor.py::test_build_monitored_roster_filters_to_monitored tests/test_call_monitor.py::test_build_monitored_roster_handles_missing_maps -v`
Expected: FAIL with `ImportError: cannot import name 'build_monitored_roster'`

- [ ] **Step 3: Implement the helper**

Add to `src/call_monitor.py` (near the other `_load_ext_*` / `_resolve_*` helpers, before `run_monitor`):

```python
def build_monitored_roster(display_map: dict, number_map: dict,
                           monitored: list[str]) -> dict[str, dict]:
    """Roster ({extId: {"name","number"}}) for the monitored extensions only.

    Name/number come from the account snapshot maps; either may be None if the
    map didn't include that extension.
    """
    return {
        ext_id: {
            "name": display_map.get(ext_id),
            "number": number_map.get(ext_id),
        }
        for ext_id in monitored
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_call_monitor.py -v`
Expected: PASS (all)

- [ ] **Step 5: Wire the helper into `run_monitor`**

In `src/call_monitor.py`, in `run_monitor`, immediately after the ext-display-map is loaded and logged (the two lines that assign `ext_display_map` and log "Loaded ext-display map for %d extensions"), insert:

```python
            roster = build_monitored_roster(
                ext_display_map, ext_number_map, Config.MONITORED_EXTENSIONS)
            store.set_rep_roster(roster)
            logger.info("Persisted roster for %d monitored rep(s)", len(roster))
```

(Indentation matches the surrounding `try:` block body inside the `while True:` loop.)

- [ ] **Step 6: Run the full monitor test file to confirm no regressions**

Run: `python -m pytest tests/test_call_monitor.py -v`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add src/call_monitor.py tests/test_call_monitor.py
git commit -m "feat(monitor): persist monitored-rep roster to Redis on startup"
```

---

### Task 3: `GET /api/calls/reps` endpoint

**Files:**
- Modify: `src/api/routes.py` (add `_caller_number` helper + `get_reps` route)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `CallStore.get_rep_roster` (Task 1), `CallStore.get_rep_current_call`, `CallStore.list_active_calls`, `Config.MONITORED_EXTENSIONS`.
- Produces: `GET /api/calls/reps` → `{"reps": [ {extId, name, number, onCall, status, direction, sessionId, callerNumber} ]}`. Reps are in `MONITORED_EXTENSIONS` order. When a rep is not on an active call, `status`/`direction`/`sessionId`/`callerNumber` are `null`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.MONITORED_EXTENSIONS", ["119", "121"])
def test_get_reps_lists_monitored_with_active_and_idle(client, store):
    store.set_rep_roster({
        "119": {"name": "Doug Stoker", "number": "119"},
        "121": {"name": "Travis Watters", "number": "121"},
    })
    # Doug (119) on an active inbound call; store_call sets the rep pointer.
    store.store_call("s-1", {
        "sessionId": "s-1", "status": "Answered", "direction": "Inbound",
        "from": {"phoneNumber": "+15125551234"}, "to": {"extensionId": "119"},
    })

    resp = client.get("/api/calls/reps", headers=auth_header())
    assert resp.status_code == 200
    reps = resp.json()["reps"]
    assert [r["extId"] for r in reps] == ["119", "121"]

    doug = reps[0]
    assert doug["name"] == "Doug Stoker"
    assert doug["number"] == "119"
    assert doug["onCall"] is True
    assert doug["status"] == "Answered"
    assert doug["callerNumber"] == "+15125551234"

    travis = reps[1]
    assert travis["onCall"] is False
    assert travis["status"] is None
    assert travis["callerNumber"] is None


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.MONITORED_EXTENSIONS", [])
def test_get_reps_empty_roster(client):
    resp = client.get("/api/calls/reps", headers=auth_header())
    assert resp.status_code == 200
    assert resp.json() == {"reps": []}


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.MONITORED_EXTENSIONS", ["999"])
def test_get_reps_missing_roster_entry_uses_placeholder(client):
    resp = client.get("/api/calls/reps", headers=auth_header())
    reps = resp.json()["reps"]
    assert reps[0]["extId"] == "999"
    assert reps[0]["name"] == "Ext 999"
    assert reps[0]["onCall"] is False


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.MONITORED_EXTENSIONS", ["119"])
def test_get_reps_outbound_uses_to_number(client, store):
    store.set_rep_roster({"119": {"name": "Doug Stoker", "number": "119"}})
    store.store_call("s-2", {
        "sessionId": "s-2", "status": "Answered", "direction": "Outbound",
        "from": {"extensionId": "119"}, "to": {"phoneNumber": "+15129990000"},
    })
    resp = client.get("/api/calls/reps", headers=auth_header())
    doug = resp.json()["reps"][0]
    assert doug["onCall"] is True
    assert doug["callerNumber"] == "+15129990000"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -k get_reps -v`
Expected: FAIL with 404 (route not defined)

- [ ] **Step 3: Implement the helper and route**

In `src/api/routes.py`, add this helper next to the other module-level helpers (e.g. after `_deal_url`):

```python
def _caller_number(call: dict) -> str | None:
    """The other party's phone number: `to` for outbound, else `from`."""
    if call.get("direction") == "Outbound":
        return (call.get("to") or {}).get("phoneNumber")
    return (call.get("from") or {}).get("phoneNumber")
```

Then add the route on the `router` (near `get_active_calls`):

```python
@router.get("/reps")
def get_reps():
    store = get_store()
    roster = store.get_rep_roster()
    active_ids = {c.get("sessionId") for c in store.list_active_calls()}
    reps = []
    for ext_id in Config.MONITORED_EXTENSIONS:
        entry = roster.get(ext_id) or {}
        call = store.get_rep_current_call(ext_id)
        on_call = bool(call) and call.get("sessionId") in active_ids
        reps.append({
            "extId": ext_id,
            "name": entry.get("name") or f"Ext {ext_id}",
            "number": entry.get("number"),
            "onCall": on_call,
            "status": call.get("status") if on_call else None,
            "direction": call.get("direction") if on_call else None,
            "sessionId": call.get("sessionId") if on_call else None,
            "callerNumber": _caller_number(call) if on_call else None,
        })
    return {"reps": reps}
```

Note: `/reps` must be registered **before** the `/{session_id}/...` routes already work regardless (different first path segment), but keep `get_reps` alongside `get_active_calls` for clarity.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -k get_reps -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full API test file to confirm no regressions**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add src/api/routes.py tests/test_api.py
git commit -m "feat(api): GET /api/calls/reps overview endpoint"
```

---

### Task 4: Overview page + `/` route

**Files:**
- Create: `src/api/static/overview.html`
- Modify: `src/api/main.py` (add `_OVERVIEW_HTML` + `/` route)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `GET /api/calls/reps` (Task 3).
- Produces: `GET /` → serves `overview.html`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:

```python
def test_root_serves_overview_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_api.py::test_root_serves_overview_page -v`
Expected: FAIL with 404 (no `/` route)

- [ ] **Step 3: Create the overview page**

Create `src/api/static/overview.html`:

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
    .meta { margin-left: auto; text-align: right; font-size: 12px;
            color: var(--muted); }
    .meta .live { color: var(--green); font-weight: 600; }
    .empty { color: var(--muted); text-align: center; padding: 40px 0; }
  </style>
</head>
<body>
  <header>
    <h1>SFW Call Bridge — Reps</h1>
    <div id="status">Loading…</div>
  </header>
  <main id="list"><div class="empty">Loading…</div></main>

  <script>
    let apiKey = localStorage.getItem("sfw-bridge-key");
    if (!apiKey) {
      apiKey = prompt("Enter your SFW Bridge API key (x-api-key):") || "";
      if (apiKey) localStorage.setItem("sfw-bridge-key", apiKey);
    }

    function esc(s) {
      return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function render(reps) {
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
            <span class="ext">ext ${esc(r.number || r.extId)}</span></span>
          <span class="meta">${meta}</span>
        </a>`;
      }).join("");
    }

    async function poll() {
      try {
        const r = await fetch("/api/calls/reps", { headers: { "x-api-key": apiKey } });
        if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
        const data = await r.json();
        render(data.reps || []);
        const active = (data.reps || []).filter(x => x.onCall).length;
        document.getElementById("status").textContent =
          `${(data.reps || []).length} reps • ${active} on call`;
      } catch (e) {
        document.getElementById("status").textContent = `Error: ${e.message}`;
      }
    }

    setInterval(poll, 3000);
    poll();
  </script>
</body>
</html>
```

- [ ] **Step 4: Add the `/` route**

In `src/api/main.py`, add the overview path constant next to `_STATIC_DIR` (after line 9):

```python
_OVERVIEW_HTML = _STATIC_DIR / "overview.html"
```

And add the route inside `create_app`, alongside the `/dashboard` route:

```python
    @app.get("/")
    def overview() -> FileResponse:
        return FileResponse(_OVERVIEW_HTML)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_api.py::test_root_serves_overview_page -v`
Expected: PASS

- [ ] **Step 6: Manually verify the page**

Restart the local server so it picks up the new route and the four monitored extensions:

```bash
python run_local.py
```

Then open `http://localhost:8000/` in a browser. Expected: the four monitored reps (Travis Watters, Vince Rodas, Doug Stoker, Jacob Hair) render as rows; any rep on a live call shows a green dot + "On call" + caller number; clicking a row opens `/dashboard?rep=<extId>`.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -v`
Expected: PASS (all)

- [ ] **Step 8: Commit**

```bash
git add src/api/static/overview.html src/api/main.py tests/test_api.py
git commit -m "feat(api): reps overview page at /"
```

---

## Self-Review Notes

- **Spec coverage:** roster persistence (Task 2 + Task 1), monitored-only + order (Task 3), endpoint shape incl. `callerNumber`/`onCall` (Task 3), placeholder-name edge case (Task 3), empty-roster edge case (Tasks 3 & 4 page "Waiting for roster…"), page at `/` reusing dark theme + `sfw-bridge-key` auth + row→dashboard link (Task 4), tests per component. All spec sections map to a task.
- **Storage deviation from spec:** spec said "Redis hash"; implemented as a single JSON string under `reps:roster` for upstash/fakeredis signature compatibility. Interface (`set_rep_roster(dict)` / `get_rep_roster()->dict`) is unchanged.
- **Type consistency:** `set_rep_roster`/`get_rep_roster`, `build_monitored_roster`, `_caller_number`, and the `reps[]` field names (`extId`, `onCall`, `callerNumber`, …) are referenced identically across tasks and the page.
