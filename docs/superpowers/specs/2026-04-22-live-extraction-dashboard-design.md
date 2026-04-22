# Live Extraction Dashboard — Design Spec

**Date:** 2026-04-22
**Status:** Accepted, ready for implementation plan
**Related:** Phase 1 (`2026-04-15-sfw-call-intelligence-bridge-design.md`), Phase 2 (`2026-04-17-phase2-live-transcripts.md` Rev 2)

## Problem

Phase 2 streams live transcripts of sales calls into Redis. Reps currently have no way to act on that information during the call. They need to:

1. See extracted customer details (name, email, phone, address, company) appear in real time as the call progresses.
2. Look up or create a HubSpot Contact without leaving the call.
3. Get an LLM-generated scope-of-work summary they can review, edit, and save as a HubSpot Deal description.
4. Hand the raw transcript off to ChatGPT for ad-hoc coaching.

This spec covers a browser dashboard that does all four, with the sales rep explicitly approving each action.

## Scope

**In scope (v1):**
- Browser dashboard with live transcript and traffic-light field indicators.
- Deterministic Python extractors (regex) for first/last name, email, phone, company, street address, city, state, ZIP.
- Claude Haiku scope-of-work summary via the local `claude` CLI (OAuth-authenticated subscription), rep-triggered.
- HubSpot Contact lookup + create/update.
- HubSpot Deal creation (associated with contact) using the approved scope summary.
- "Copy Transcript to Clipboard" handoff to ChatGPT.

**Out of scope (v1):**
- Speaker diarization (so extractors may false-positive on rep's own words — rep catches via edit).
- LLM-based field extraction (kept deterministic per user preference for v1).
- Auto-triggering HubSpot writes; every write is rep-approved.
- Supervisor/manager views across multiple reps.
- Authentication beyond a single `x-api-key` header (rep types it once into the dashboard; stored in localStorage).
- Push notifications (dashboard polls; no WebSocket).
- Mobile layout (desktop only).

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  Browser — http://localhost:8000/dashboard?rep=576959052              │
│  ┌──────────────────────┐  ┌──────────────────────────────────────┐  │
│  │  Live Transcript     │  │  Extracted fields (●red/●green)      │  │
│  │  (polled every 2s)   │  │  + editable inputs + action buttons  │  │
│  └──────────────────────┘  └──────────────────────────────────────┘  │
└───────────────────────────────────┬───────────────────────────────────┘
                                    │ polls + button POSTs
                                    ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      FastAPI (additions)                             │
│  GET  /dashboard                         [new, serves HTML]          │
│  GET  /api/calls/{sid}/extracted         [new]                       │
│  POST /api/calls/{sid}/scope-summary     [new → claude CLI]          │
│  POST /api/hubspot/contacts/lookup       [new]                       │
│  POST /api/hubspot/contacts              [new, create/update]        │
│  POST /api/hubspot/deals                 [new]                       │
└───┬───────────────────┬─────────────────┬────────────────────────────┘
    │                   │                 │
    ▼                   ▼                 ▼
┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
│ Upstash KV  │   │ HubSpot REST │   │ claude CLI       │
│ (transcripts│   │ (private app │   │ subprocess       │
│ + extracted)│   │  token)      │   │ (OAuth subscr.)  │
└──────▲──────┘   └──────────────┘   └──────────────────┘
       │ writes
       │
┌──────┴───────────────────────────────────────────────────────────────┐
│  Extraction worker (asyncio task in run_local.py)                    │
│  Every 3s: for each active monitored session, run regex extractors   │
│  against the current transcript, write JSON to call:{sid}:extracted. │
└──────────────────────────────────────────────────────────────────────┘
```

The Phase 2 pipeline (sidecar, call_monitor, transcript writes) is unchanged. This design adds one background task, several API endpoints, one static HTML page, one HubSpot client module, and one Claude CLI wrapper.

## Components

### 1. `src/extractor.py` — regex field extractors

One function per field, each taking a transcript string and returning the most recent match or `None`:

```python
extract_email(text: str) -> str | None
extract_phone(text: str) -> str | None         # normalized to 10-digit string
extract_firstname(text: str) -> str | None
extract_lastname(text: str) -> str | None
extract_company(text: str) -> str | None
extract_address(text: str) -> str | None       # street line
extract_city(text: str) -> str | None
extract_state(text: str) -> str | None         # 2-letter code
extract_zip(text: str) -> str | None
```

Pattern sketch (full patterns in implementation):
- `email`: `[\w.+-]+@[\w-]+\.[\w.-]+`
- `phone`: US format regex plus a spoken-number normalizer (`"five oh three"` → `"503"`) using a hand-written digit mapping.
- `firstname`: `(?:my name is|this is|i'm|i am|speaking with)\s+([A-Z][a-z]+)`
- `zip`: `\b\d{5}(?:-\d{4})?\b`

Re-runs against the full transcript each cycle; monotonic transcript growth means re-extraction cost is bounded per call (transcript stays under ~10 KB for most calls).

**Explicit limitation:** no speaker diarization. The rep's own "Hi, my name is Doug" can false-positive. Documented in UI help text, caught by rep edit.

### 2. `src/extraction_worker.py` — background task

Pattern:

```python
async def run_extraction_worker(store: CallStore, interval: float = 3.0) -> None:
    while True:
        try:
            for sid in store.list_active_sessions():
                transcript = store.get_transcript(sid) or ""
                extracted = {
                    field: fn(transcript) for field, fn in EXTRACTORS.items()
                }
                store.set_extracted(sid, extracted)
        except Exception:
            logger.exception("extraction worker iteration failed")
        await asyncio.sleep(interval)
```

Launched from `run_local.py` via `asyncio.gather` alongside the existing `run_monitor`, API server, etc. Failures in one iteration don't kill the loop.

`store.list_active_sessions()` and `store.set_extracted(sid, dict)` are new methods on `CallStore`:

```python
def list_active_sessions(self) -> list[str]:
    return list(self.redis.smembers("calls:active"))

def set_extracted(self, sid: str, data: dict, ttl: int = 3600) -> None:
    self.redis.set(f"call:{sid}:extracted", json.dumps(data), ex=ttl)

def get_extracted(self, sid: str) -> dict | None:
    raw = self.redis.get(f"call:{sid}:extracted")
    return json.loads(raw) if raw else None
```

### 3. `src/hubspot_client.py` — HubSpot wrapper

Thin wrapper over `httpx.AsyncClient`, no SDK dependency. Auth via `HUBSPOT_PRIVATE_APP_TOKEN` env var.

```python
class HubSpotClient:
    def __init__(self, token: str, base_url: str = "https://api.hubapi.com"): ...

    async def lookup_contact(
        self, email: str | None = None, phone: str | None = None
    ) -> dict | None:
        """Search by email first, then phone. Returns the first match or None."""

    async def upsert_contact(self, props: dict) -> dict:
        """Email is the idempotency key. Create if new, patch if existing."""

    async def create_deal(
        self, contact_id: str, dealname: str, description: str, stage: str | None = None
    ) -> dict:
        """Create a deal and associate it with the given contact.

        When stage is None, HubSpot uses the default pipeline's default stage
        (typically 'appointmentscheduled' for the sales pipeline).
        """
```

Uses HubSpot's v3 endpoints:
- `POST /crm/v3/objects/contacts/search` for lookup
- `POST /crm/v3/objects/contacts` / `PATCH /crm/v3/objects/contacts/{id}` for upsert
- `POST /crm/v3/objects/deals` + association via the request body's `associations` field

### 4. `src/scope_summarizer.py` — Claude CLI wrapper

```python
import asyncio, json

SCOPE_PROMPT = """You're summarizing a construction sales call for HubSpot.

Produce a 2-3 sentence scope of work summarizing what the customer wants done,
where, and any timeline or budget constraints they mentioned. Write it as if
it will go directly into a HubSpot Deal description — factual, no salesy
language, no preamble like 'Based on the transcript...'.

TRANSCRIPT:
{transcript}

SCOPE OF WORK:"""


async def summarize_scope(transcript: str, timeout: float = 30.0) -> str:
    prompt = SCOPE_PROMPT.format(transcript=transcript)
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--output-format=json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {stderr.decode()[:500]}")
    return json.loads(stdout)["result"]
```

Async subprocess so the FastAPI request thread isn't blocked. The `claude -p ... --output-format=json` invocation returns JSON containing a `result` field with the model's text response.

### 5. `src/api/routes.py` additions

All new endpoints are placed in the existing `/api` router, gated by the existing `verify_api_key` dependency (matches the Phase 1 auth model):

```python
@router.get("/calls/{sid}/extracted")
def get_extracted(sid: str) -> dict:
    ...

@router.post("/calls/{sid}/scope-summary")
async def generate_scope(sid: str) -> dict:  # {"summary": "..."}
    ...

@router.post("/hubspot/contacts/lookup")
async def hubspot_lookup(body: LookupReq) -> dict | Response:  # 404 if no match
    ...

@router.post("/hubspot/contacts")
async def hubspot_upsert_contact(body: ContactProps) -> dict:  # returns {"contactId": "..."}
    ...

@router.post("/hubspot/deals")
async def hubspot_create_deal(body: DealReq) -> dict:  # returns {"dealId": "..."}
    ...
```

Pydantic models for the request bodies live next to the route handlers. No new models needed elsewhere.

### 6. `GET /dashboard` — static HTML page

Serves a single `dashboard.html` file from `src/api/static/dashboard.html`. Not auth-gated at the HTTP layer (so the rep doesn't need to inject a header on load); auth happens inside the page via JS:

- On first load, JS prompts for the API key and stores it in `localStorage["sfw-bridge-key"]`.
- Every subsequent fetch from the dashboard includes the key in the `x-api-key` header.

URL params consumed: `?rep={extension_id}` (required). Missing → error message, no polling.

**HTML structure:**

```
<body>
  <header>SFW Call Bridge — Dashboard</header>
  <main class="grid">
    <section id="transcript">
      <h2>Live Transcript</h2>
      <div id="transcript-body"></div>
    </section>
    <section id="fields">
      <h2>Caller Info</h2>
      <div class="field" data-key="firstname">
        <span class="dot"></span>
        <label>First name</label>
        <input type="text" />
      </div>
      <!-- ... 8 more fields ... -->
      <div class="actions">
        <button id="lookup">🔍 Lookup HubSpot Contact</button>
        <button id="upsert">✅ Create/Update Contact</button>
        <hr />
        <button id="scope">✨ Generate Scope Summary</button>
        <textarea id="scope-text" rows="5"></textarea>
        <button id="deal">💼 Create Deal</button>
        <hr />
        <button id="copy">📋 Copy Transcript</button>
      </div>
    </section>
  </main>
  <div id="toast"></div>
  <script> /* polling + button handlers */ </script>
</body>
```

**Polling loop:**
- Every 2s: `GET /api/calls/latest?rep={ext_id}` → updates transcript text, session ID, and "Call ended" banner when no active call.
- Every 3s: `GET /api/calls/{sid}/extracted` → updates field values + traffic lights. Rep's manual edits to inputs are NOT overwritten (once rep has touched a field, the dashboard stops auto-filling that field until reload).

**Button wiring:** straightforward — each reads the current field values from the inputs, POSTs to the matching endpoint, shows a toast with the result.

## Data model (Redis keys)

| Key                          | Writer                | Reader                   | TTL |
|------------------------------|-----------------------|--------------------------|-----|
| `call:{sid}:state`           | Python call_monitor   | FastAPI                  | 1h after disconnect |
| `call:{sid}:transcript`      | TS sidecar            | FastAPI, dashboard       | 1h  |
| `call:{sid}:extracted`       | Python extractor worker [NEW] | FastAPI, dashboard | 1h  |
| `calls:active` (set)         | Python call_monitor   | extractor worker [NEW]   | —   |
| `rep:{ext_id}:current`       | Python call_monitor   | FastAPI                  | 1h  |

`call:{sid}:extracted` JSON shape:
```json
{
  "firstname": "Sebastian",
  "lastname": null,
  "email": null,
  "phone": "5034441123",
  "company": "CORE Electric LLC",
  "address": "1516 NE Marie Drive",
  "city": null,
  "state": null,
  "zip": "97230"
}
```

## Configuration

New env vars:
- `HUBSPOT_PRIVATE_APP_TOKEN` — from HubSpot Settings → Integrations → Private Apps. Scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.deals.read`, `crm.objects.deals.write`.

Existing env vars reused: `CALL_BRIDGE_API_KEY` (dashboard auth), `KV_REST_API_URL`/`KV_REST_API_TOKEN`.

## Error handling

| Failure                    | Response                                                   |
|----------------------------|------------------------------------------------------------|
| HubSpot 401 (bad token)    | 502 from our API, body includes "HubSpot auth failed"      |
| HubSpot 429 (rate limit)   | 429 passthrough                                             |
| HubSpot 5xx                | 502 from our API, body includes HubSpot's error response   |
| Claude CLI timeout (>30s)  | 504, message "summarizer timed out"                         |
| Claude CLI nonzero exit    | 500, stderr clip in response body                           |
| Extraction worker crash    | Logged via `logger.exception`, loop continues after 3s      |
| Missing env vars at boot   | FastAPI health check fails with explicit message           |
| Redis unavailable          | 503 from API, existing behavior                             |

The dashboard shows any non-2xx response as a toast with the server's error body visible.

## Testing

- **`tests/test_extractor.py`** — unit tests per extractor. Positive cases (does it find X?) and negative cases (does it avoid false positives?). Minimum ~5 cases per field.
- **`tests/test_extraction_worker.py`** — mocks `CallStore`, exercises one iteration and verifies the right Redis writes.
- **`tests/test_hubspot_client.py`** — `respx`-mocked HubSpot REST responses (search → match / no match; upsert → create / patch; deal create + association).
- **`tests/test_scope_summarizer.py`** — mock `asyncio.create_subprocess_exec`, feed canned stdout, verify parsing + timeout handling.
- **Dashboard** — manually smoke-tested only. No browser-automation tests in v1.

## Open questions / known limitations

1. **Speaker diarization.** Phase 2 pipes a single mixed-audio stream to Deepgram; we don't know who said what. Extractors can't distinguish customer vs rep. Flag in UI help text; rely on rep edit to fix false positives. A future change could enable Deepgram's diarization on the sidecar side.

2. **HubSpot custom properties.** v1 uses standard HubSpot contact properties (`firstname`, `lastname`, `email`, `phone`, `company`, `address`, `city`, `state`, `zip`). If SFW wants to track jobsite-specific fields differently from billing address, that's a custom property schema conversation for v2.

3. **Concurrent-rep collision.** If two reps somehow end up on calls with the same `sessionId` (shouldn't happen — RC session IDs are unique per call), their dashboards would share state. Not a realistic concern but worth noting.

4. **Dashboard auth.** `localStorage` storage of the API key is convenient but a mild risk if the rep's laptop is shared. Reasonable for internal SFW use.

5. **LocalStorage edit state.** If a rep edits a field manually, we stop auto-filling it until reload. That's the simple rule; may feel slightly surprising if the rep expected a subsequent extraction to overwrite.

6. **Call-ended freeze.** Dashboard freezes on last state when the call ends; fields stay editable so the rep can still create contact/deal post-call. This means the dashboard is still usable for a few seconds to wrap up after hangup.
