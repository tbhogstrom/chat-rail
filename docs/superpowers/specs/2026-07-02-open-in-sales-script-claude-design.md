# "Open in Sales Script Claude" Button — Design

**Date:** 2026-07-02
**Status:** Approved

## Problem

A rep working a call on the dashboard wants to take that call's transcript into
a specific Claude.ai project ("Sales Script") to work it against the sales
script. Today they'd manually copy the transcript and navigate to the project.
We want a one-click path.

## Constraint

There is **no supported way for a link to auto-attach a file to a Claude.ai
chat.** A URL can open a specific project and can prefill message *text*, but it
cannot silently attach a `.txt`. So the flow can't be fully hands-free. Chosen
approach: copy the transcript to the clipboard and open the project; the rep
pastes (Ctrl/Cmd+V), and Claude converts a large paste into an attachment. This
is reliable (no URL length limit), works with the specific project, and depends
on no undocumented URL parameters.

All end users are already authenticated into Claude in their browser, so opening
the project URL lands them directly in the project.

## Behavior

A button **"🧠 Open in Sales Script Claude"** on the dashboard, in the actions
column next to **Copy Transcript**. On click:

1. Read the current transcript (the `#transcript-body` text, same source as the
   existing Copy button). If empty → toast "No transcript yet" and stop.
2. Copy the full transcript to the clipboard.
3. Open the Sales Script project URL in a **new tab** (`window.open(url, "_blank")`).
4. Toast: "Transcript copied — paste into Claude with Ctrl/Cmd+V."

If the clipboard write fails, still open the project and toast an error telling
the rep to copy manually (the transcript is right there on the page).

## Configuration

The project URL is **not hardcoded in the page** — it is a config value so it
can change per environment without a code edit (relevant to the upcoming Vercel
deployment).

- `Config.SALES_SCRIPT_CLAUDE_URL` — default
  `https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061`, overridable
  via the `SALES_SCRIPT_CLAUDE_URL` env var. Uses the present-but-empty-safe
  `os.environ.get(...) or "<default>"` pattern already used for `ANTHROPIC_MODEL`.

## Components

### 1. `src/config.py`

Add:

```python
    SALES_SCRIPT_CLAUDE_URL: str = os.environ.get("SALES_SCRIPT_CLAUDE_URL") \
        or "https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061"
```

### 2. `src/api/routes.py`

Add an authed endpoint on the existing `/api/calls` router:

```python
@router.get("/config")
def get_ui_config():
    return {"salesScriptClaudeUrl": Config.SALES_SCRIPT_CLAUDE_URL}
```

Returns the URL the dashboard needs; behind `verify_api_key` like its siblings
(the dashboard already holds the API key). The value is not secret; auth is for
consistency, not confidentiality.

### 3. `src/api/static/dashboard.html`

- Add the button to the `.actions` block, next to `#copy`:
  `<button id="open-claude" class="secondary">🧠 Open in Sales Script Claude</button>`
- On page load, fetch `GET /api/calls/config` (with the existing `authHeaders()`)
  and store `salesScriptClaudeUrl` in a variable (fallback: button disabled or a
  toast if the fetch fails).
- Click handler: read `#transcript-body` textContent; if blank → toast
  "No transcript yet". Else `navigator.clipboard.writeText(transcript)` then
  `window.open(url, "_blank")` and toast the paste hint. On clipboard failure,
  still open and toast an error.

Reuses the page's existing `toast()` and `authHeaders()` helpers.

## Data flow

```
dashboard load ── GET /api/calls/config ──> { salesScriptClaudeUrl }
button click ──> copy #transcript-body to clipboard ──> window.open(url, "_blank")
             └─> toast "Transcript copied — paste into Claude"
rep in Claude project ──> Ctrl/Cmd+V ──> paste becomes an attachment
```

## Error handling & edge cases

- Empty transcript → toast "No transcript yet", no copy/open.
- Clipboard write fails (permissions/older browser) → toast error, still open the
  project so the rep can copy manually.
- Config fetch fails → toast an error on click and skip opening (no URL to open).

## Testing

- Endpoint: `GET /api/calls/config` returns `{"salesScriptClaudeUrl": <default>}`
  when the env var is unset, and the overridden value when
  `Config.SALES_SCRIPT_CLAUDE_URL` is patched.
- Button behavior (clipboard, `window.open`) verified manually — consistent with
  how the existing dashboard JS is tested (no DOM/browser harness in this repo).

## Out of scope (YAGNI)

A pre-typed starting prompt, placing the button on the overview / recent-calls
feed, and auto-creating the chat via API (claude.ai project chats are not
API-addressable).
