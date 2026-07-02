# "Open in Sales Script Claude" Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dashboard button that copies the current call transcript to the clipboard and opens the "Sales Script" Claude project in a new tab.

**Architecture:** A config-driven project URL (`SALES_SCRIPT_CLAUDE_URL`) is exposed via a small authed endpoint `GET /api/calls/config`; the dashboard fetches it on load and wires a button that copies `#transcript-body` and `window.open`s the URL.

**Tech Stack:** Python 3.12+, FastAPI, vanilla HTML+JS (dashboard).

## Global Constraints

- Reuse existing auth: `/api/calls` router already depends on `verify_api_key` (`x-api-key`). The dashboard already holds the key and uses `authHeaders()`.
- Config default URL: `https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061`, overridable via `SALES_SCRIPT_CLAUDE_URL` env var, using the present-but-empty-safe `os.environ.get(...) or "<default>"` pattern.
- Response key is exactly `salesScriptClaudeUrl`.
- Run tests with `python -m pytest` from repo root. Commit messages: conventional style, **no** `Co-Authored-By: Claude` trailer.

---

### Task 1: Config value + `GET /api/calls/config` endpoint

**Files:**
- Modify: `src/config.py` (add `SALES_SCRIPT_CLAUDE_URL`)
- Modify: `src/api/routes.py` (add `/config` route)
- Test: `tests/test_config.py`, `tests/test_api.py`

**Interfaces:**
- Produces: `Config.SALES_SCRIPT_CLAUDE_URL: str`; `GET /api/calls/config` → `{"salesScriptClaudeUrl": <str>}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_sales_script_claude_url_default():
    from src.config import Config
    assert Config.SALES_SCRIPT_CLAUDE_URL == \
        "https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061"
```

Append to `tests/test_api.py`:

```python
@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_ui_config_returns_default_url(client):
    r = client.get("/api/calls/config", headers=auth_header())
    assert r.status_code == 200
    assert r.json()["salesScriptClaudeUrl"].startswith("https://claude.ai/project/")


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.api.routes.Config.SALES_SCRIPT_CLAUDE_URL",
       "https://claude.ai/project/override-123")
def test_get_ui_config_returns_configured_url(client):
    r = client.get("/api/calls/config", headers=auth_header())
    assert r.json() == {"salesScriptClaudeUrl": "https://claude.ai/project/override-123"}


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_ui_config_requires_auth(client):
    assert client.get("/api/calls/config").status_code == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_config.py::test_sales_script_claude_url_default tests/test_api.py -k ui_config -v`
Expected: FAIL (`AttributeError` for the config attr; 404 for the route)

- [ ] **Step 3: Add the config value**

In `src/config.py`, inside `class Config` (after the `METRICS_TIMEZONE` line), add:

```python
    # Claude project the dashboard "Open in Sales Script Claude" button opens.
    SALES_SCRIPT_CLAUDE_URL: str = os.environ.get("SALES_SCRIPT_CLAUDE_URL") \
        or "https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061"
```

- [ ] **Step 4: Add the endpoint**

In `src/api/routes.py`, add just after the `get_recent_calls` route (`Config` is already imported):

```python
@router.get("/config")
def get_ui_config():
    return {"salesScriptClaudeUrl": Config.SALES_SCRIPT_CLAUDE_URL}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py tests/test_api.py -q`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add src/config.py src/api/routes.py tests/test_config.py tests/test_api.py
git commit -m "feat(api): SALES_SCRIPT_CLAUDE_URL config + GET /api/calls/config"
```

---

### Task 2: Dashboard button

**Files:**
- Modify: `src/api/static/dashboard.html`

**Interfaces:**
- Consumes: `GET /api/calls/config` (Task 1).

- [ ] **Step 1: Add the button to the actions block**

In `src/api/static/dashboard.html`, replace:

```html
        <button id="copy" class="secondary">📋 Copy Transcript</button>
      </div>
```

with:

```html
        <button id="copy" class="secondary">📋 Copy Transcript</button>
        <button id="open-claude" class="secondary">🧠 Open in Sales Script Claude</button>
      </div>
```

- [ ] **Step 2: Add config fetch + click handler**

In the `<script>` of `src/api/static/dashboard.html`, immediately after the existing `#copy` click handler (the block ending with the `document.getElementById("copy").addEventListener(...)` closing `});`), add:

```javascript
    // ---- Open in Sales Script Claude ----
    let salesScriptUrl = null;
    (async function loadUiConfig() {
      try {
        const r = await fetch("/api/calls/config", { headers: authHeaders() });
        if (r.ok) salesScriptUrl = (await r.json()).salesScriptClaudeUrl;
      } catch (_) { /* button will report if URL missing */ }
    })();

    document.getElementById("open-claude").addEventListener("click", async () => {
      const txt = document.getElementById("transcript-body").textContent.trim();
      if (!txt || txt === "Waiting for transcript…") {
        return toast("No transcript yet", true);
      }
      if (!salesScriptUrl) {
        return toast("Sales Script URL not loaded yet — try again in a moment", true);
      }
      try {
        await navigator.clipboard.writeText(txt);
        window.open(salesScriptUrl, "_blank");
        toast("Transcript copied — paste into Claude (Ctrl/Cmd+V)");
      } catch (e) {
        // Clipboard blocked — still open Claude so the rep can copy manually.
        window.open(salesScriptUrl, "_blank");
        toast("Opened Claude — copy the transcript manually (clipboard blocked)", true);
      }
    });
```

- [ ] **Step 3: Verify the dashboard route still serves**

Run: `python -m pytest tests/test_api.py -k dashboard -v`
Expected: PASS (the existing dashboard route test, if present) — otherwise confirm the file is valid by loading it in Step 4.

- [ ] **Step 4: Manually verify**

Restart the local server:

```bash
python run_local.py
```

Open `http://localhost:8000/dashboard?rep=576959052`. Expected: a "🧠 Open in Sales Script Claude" button appears below "Copy Transcript". With a transcript present, clicking it copies the transcript, opens `https://claude.ai/project/019eaedf-…` in a new tab, and shows the "Transcript copied" toast. With no transcript, it shows "No transcript yet". Confirm the config endpoint:

```bash
KEY=$(grep '^CALL_BRIDGE_API_KEY=' .env | cut -d= -f2-)
curl -s -H "x-api-key: $KEY" http://localhost:8000/api/calls/config
```

Expected: `{"salesScriptClaudeUrl":"https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061"}`

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add src/api/static/dashboard.html
git commit -m "feat(dashboard): Open in Sales Script Claude button"
```

---

## Self-Review Notes

- **Spec coverage:** config value + default/override (Task 1); authed `/api/calls/config` returning `salesScriptClaudeUrl` (Task 1); button next to Copy Transcript with copy-then-open, empty-transcript guard, clipboard-failure fallback, config-not-loaded guard (Task 2); endpoint tests + manual button verification (both tasks). All spec sections map to a task.
- **Placeholder scan:** none; every code step is complete.
- **Type consistency:** `SALES_SCRIPT_CLAUDE_URL`, response key `salesScriptClaudeUrl`, and JS var `salesScriptUrl` used consistently across tasks and matching the spec.
- **Route safety:** `/api/calls/config` is a single static segment — no collision with `/api/calls/{session_id}/...`.
