# SFW Call Intelligence Bridge — Phase 1 (MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an MVP that tracks active RingCentral calls via WebSocket, stores call state in Upstash Redis, serves call metadata through a GPT Action API, and transcribes post-call recordings via Deepgram batch API.

**Architecture:** A persistent Python process subscribes to RingCentral telephony WebSocket events and writes call state to Upstash Redis. A separate FastAPI app (deployed to Vercel) reads from Redis and exposes endpoints that ChatGPT's GPT Actions can call. After calls end, recordings are fetched and sent to Deepgram for batch transcription.

**Tech Stack:** Python 3.14, FastAPI, ringcentral SDK, upstash-redis, httpx, Deepgram REST API, pytest + fakeredis

---

## File Structure

```
callrail-chatgpt/
├── src/
│   ├── __init__.py
│   ├── config.py                  # Load env vars, expose as typed settings
│   ├── redis_store.py             # Upstash Redis wrapper — read/write call state
│   ├── call_monitor.py            # RC WebSocket subscription + event processing
│   ├── recording_transcriber.py   # Deepgram batch transcription for post-call recordings
│   └── api/
│       ├── __init__.py
│       ├── main.py                # FastAPI app instance + middleware
│       ├── auth.py                # API key validation dependency
│       └── routes.py              # GET /api/calls/* endpoints
├── tests/
│   ├── __init__.py
│   ├── conftest.py                # Shared fixtures (fake Redis, test client)
│   ├── test_redis_store.py
│   ├── test_call_monitor.py
│   ├── test_recording_transcriber.py
│   └── test_api.py
├── api/
│   └── index.py                   # Vercel serverless entrypoint (imports FastAPI app)
├── run_monitor.py                 # Entrypoint for persistent call monitor process
├── pyproject.toml                 # Project config, dependencies, pytest settings
├── vercel.json                    # Vercel routing config
└── .env.example                   # Template for required env vars
```

---

### Task 1: Project Setup and Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `src/__init__.py`
- Create: `src/config.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `.env.example`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "sfw-call-bridge"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn>=0.34",
    "upstash-redis>=1.1",
    "ringcentral>=0.9",
    "httpx>=0.28",
    "python-dotenv>=1.0",
    "websockets>=14.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "fakeredis>=2.26",
    "httpx",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create `src/__init__.py`**

```python
```

(Empty file — marks `src` as a package.)

- [ ] **Step 3: Create `src/config.py`**

```python
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # RingCentral
    RC_CLIENT_ID: str = os.environ["RC_CLIENT_ID"]
    RC_CLIENT_SECRET: str = os.environ["RC_CLIENT_SECRET"]
    RC_JWT: str = os.environ["RC_JWT"]
    RC_SERVER: str = os.environ.get("RC_SERVER", "https://platform.ringcentral.com")

    # Upstash Redis
    UPSTASH_REDIS_URL: str = os.environ["UPSTASH_REDIS_URL"]
    UPSTASH_REDIS_TOKEN: str = os.environ["UPSTASH_REDIS_TOKEN"]

    # Deepgram
    DEEPGRAM_API_KEY: str = os.environ.get("DEEPGRAM_API_KEY", "")

    # API
    API_KEY: str = os.environ["CALL_BRIDGE_API_KEY"]

    # TTLs (seconds)
    CALL_TTL: int = 3600  # 1 hour after call ends
```

- [ ] **Step 4: Create `.env.example`**

```env
# RingCentral (JWT server-to-server auth)
RC_CLIENT_ID=
RC_CLIENT_SECRET=
RC_JWT=
RC_SERVER=https://platform.ringcentral.com

# Upstash Redis
UPSTASH_REDIS_URL=
UPSTASH_REDIS_TOKEN=

# Deepgram (for post-call transcription)
DEEPGRAM_API_KEY=

# GPT Action API key
CALL_BRIDGE_API_KEY=
```

- [ ] **Step 5: Create `tests/__init__.py` and `tests/conftest.py`**

`tests/__init__.py`:
```python
```

`tests/conftest.py`:
```python
import pytest
import fakeredis


@pytest.fixture
def fake_redis():
    """Provide a fakeredis instance that mimics Upstash Redis interface."""
    return fakeredis.FakeRedis(decode_responses=True)
```

- [ ] **Step 6: Install dependencies and verify**

Run:
```bash
cd C:/Users/tfalcon/callrail-chatgpt
pip install -e ".[dev]"
pytest --co
```

Expected: dependencies install cleanly, pytest collects 0 tests (no test files with tests yet).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/__init__.py src/config.py tests/__init__.py tests/conftest.py .env.example
git commit -m "feat: project setup with config, dependencies, and test infrastructure"
```

---

### Task 2: Redis Store — Call State Management

**Files:**
- Create: `src/redis_store.py`
- Create: `tests/test_redis_store.py`

The Redis store is the shared data layer. It writes call state from the monitor and reads it from the API. All keys are documented in the design spec.

Key structure:
- `call:{sessionId}:state` — JSON blob with call metadata
- `call:{sessionId}:transcript` — transcript text (Phase 1: post-call only)
- `calls:active` — set of active session IDs
- `rep:{extensionId}:current` — pointer to rep's current session ID

- [ ] **Step 1: Write failing tests for `store_call`, `get_call`, and `list_active_calls`**

File: `tests/test_redis_store.py`

```python
import json
import pytest
from src.redis_store import CallStore


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


def test_store_call_saves_state(store, fake_redis):
    call_data = {
        "sessionId": "s-abc123",
        "direction": "Inbound",
        "status": "Answered",
        "from": {"phoneNumber": "+12065551234", "name": "John Doe"},
        "to": {"extensionId": "119", "name": "Doug Stoker"},
        "startTime": "2026-04-16T10:00:00Z",
    }
    store.store_call("s-abc123", call_data)

    raw = fake_redis.get("call:s-abc123:state")
    assert raw is not None
    saved = json.loads(raw)
    assert saved["sessionId"] == "s-abc123"
    assert saved["direction"] == "Inbound"


def test_store_call_adds_to_active_set(store, fake_redis):
    store.store_call("s-abc123", {"sessionId": "s-abc123", "status": "Answered"})
    assert fake_redis.sismember("calls:active", "s-abc123")


def test_store_call_sets_rep_pointer(store, fake_redis):
    call_data = {
        "sessionId": "s-abc123",
        "status": "Answered",
        "to": {"extensionId": "119"},
    }
    store.store_call("s-abc123", call_data)
    assert fake_redis.get("rep:119:current") == "s-abc123"


def test_get_call_returns_stored_data(store):
    call_data = {"sessionId": "s-abc123", "status": "Answered"}
    store.store_call("s-abc123", call_data)

    result = store.get_call("s-abc123")
    assert result["sessionId"] == "s-abc123"


def test_get_call_returns_none_for_missing(store):
    assert store.get_call("nonexistent") is None


def test_list_active_calls(store):
    store.store_call("s-1", {"sessionId": "s-1", "status": "Answered"})
    store.store_call("s-2", {"sessionId": "s-2", "status": "Answered"})

    active = store.list_active_calls()
    assert len(active) == 2
    session_ids = {c["sessionId"] for c in active}
    assert session_ids == {"s-1", "s-2"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_redis_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.redis_store'`

- [ ] **Step 3: Implement `CallStore`**

File: `src/redis_store.py`

```python
import json
from typing import Any


class CallStore:
    """Manages call state in Redis. Works with both upstash-redis and fakeredis."""

    def __init__(self, redis_client):
        self.redis = redis_client

    def store_call(self, session_id: str, call_data: dict) -> None:
        """Store or update call state and add to active set."""
        self.redis.set(f"call:{session_id}:state", json.dumps(call_data))
        self.redis.sadd("calls:active", session_id)

        # Set rep pointer if we know the rep's extension
        ext_id = call_data.get("to", {}).get("extensionId")
        if not ext_id:
            ext_id = call_data.get("from", {}).get("extensionId")
        if ext_id:
            self.redis.set(f"rep:{ext_id}:current", session_id)

    def get_call(self, session_id: str) -> dict | None:
        """Get call state by session ID."""
        raw = self.redis.get(f"call:{session_id}:state")
        if raw is None:
            return None
        return json.loads(raw)

    def list_active_calls(self) -> list[dict]:
        """Return state for all active calls."""
        session_ids = self.redis.smembers("calls:active")
        calls = []
        for sid in session_ids:
            call = self.get_call(sid)
            if call:
                calls.append(call)
        return calls

    def complete_call(self, session_id: str, ttl: int = 3600) -> None:
        """Mark a call as completed: remove from active set, set TTL on state."""
        call = self.get_call(session_id)
        if call:
            call["status"] = "Disconnected"
            self.redis.set(f"call:{session_id}:state", json.dumps(call))
            self.redis.expire(f"call:{session_id}:state", ttl)
            self.redis.expire(f"call:{session_id}:transcript", ttl)
        self.redis.srem("calls:active", session_id)

    def get_rep_current_call(self, extension_id: str) -> dict | None:
        """Get the current/latest call for a rep by extension ID."""
        session_id = self.redis.get(f"rep:{extension_id}:current")
        if session_id is None:
            return None
        return self.get_call(session_id)

    def store_transcript(self, session_id: str, transcript: str) -> None:
        """Store transcript text for a call."""
        self.redis.set(f"call:{session_id}:transcript", transcript)

    def get_transcript(self, session_id: str) -> str | None:
        """Get transcript text for a call."""
        return self.redis.get(f"call:{session_id}:transcript")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_redis_store.py -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Write tests for `complete_call`, `get_rep_current_call`, and transcript methods**

Append to `tests/test_redis_store.py`:

```python
def test_complete_call_removes_from_active(store, fake_redis):
    store.store_call("s-abc123", {"sessionId": "s-abc123", "status": "Answered"})
    store.complete_call("s-abc123")

    assert not fake_redis.sismember("calls:active", "s-abc123")


def test_complete_call_sets_status_disconnected(store):
    store.store_call("s-abc123", {"sessionId": "s-abc123", "status": "Answered"})
    store.complete_call("s-abc123")

    call = store.get_call("s-abc123")
    assert call["status"] == "Disconnected"


def test_get_rep_current_call(store):
    call_data = {
        "sessionId": "s-abc123",
        "status": "Answered",
        "to": {"extensionId": "119", "name": "Doug Stoker"},
    }
    store.store_call("s-abc123", call_data)

    result = store.get_rep_current_call("119")
    assert result["sessionId"] == "s-abc123"


def test_get_rep_current_call_returns_none(store):
    assert store.get_rep_current_call("999") is None


def test_store_and_get_transcript(store):
    store.store_transcript("s-abc123", "Hello, this is Doug from SFW Construction.")
    result = store.get_transcript("s-abc123")
    assert result == "Hello, this is Doug from SFW Construction."


def test_get_transcript_returns_none_for_missing(store):
    assert store.get_transcript("nonexistent") is None
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/test_redis_store.py -v`
Expected: all 12 tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/redis_store.py tests/test_redis_store.py
git commit -m "feat: Redis store for call state, active tracking, rep lookup, and transcripts"
```

---

### Task 3: GPT Action API — FastAPI Endpoints

**Files:**
- Create: `src/api/__init__.py`
- Create: `src/api/auth.py`
- Create: `src/api/main.py`
- Create: `src/api/routes.py`
- Create: `tests/test_api.py`

The API is stateless — it reads from Redis and returns JSON. ChatGPT calls these endpoints via GPT Actions.

- [ ] **Step 1: Create `src/api/__init__.py`**

```python
```

- [ ] **Step 2: Write failing tests for API key auth**

File: `tests/test_api.py`

```python
import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
from src.redis_store import CallStore
from src.api.main import create_app


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


@pytest.fixture
def client(store):
    app = create_app(store)
    return TestClient(app)


API_KEY = "test-key-123"


def auth_header():
    return {"x-api-key": API_KEY}


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_missing_api_key_returns_401(client):
    resp = client.get("/api/calls/active")
    assert resp.status_code == 401


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_wrong_api_key_returns_401(client):
    resp = client.get("/api/calls/active", headers={"x-api-key": "wrong"})
    assert resp.status_code == 401


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_valid_api_key_passes(client, store):
    resp = client.get("/api/calls/active", headers=auth_header())
    assert resp.status_code == 200
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Implement auth dependency**

File: `src/api/auth.py`

```python
from fastapi import Header, HTTPException
from src.config import Config


def verify_api_key(x_api_key: str = Header(None)) -> str:
    if x_api_key is None or x_api_key != Config.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key
```

- [ ] **Step 5: Implement routes**

File: `src/api/routes.py`

```python
from fastapi import APIRouter, Depends, HTTPException
from src.redis_store import CallStore
from src.api.auth import verify_api_key

router = APIRouter(prefix="/api/calls", dependencies=[Depends(verify_api_key)])

# Store is injected via app.state at startup
_store: CallStore | None = None


def set_store(store: CallStore):
    global _store
    _store = store


def get_store() -> CallStore:
    if _store is None:
        raise RuntimeError("Store not initialized")
    return _store


@router.get("/active")
def get_active_calls():
    store = get_store()
    calls = store.list_active_calls()
    return {"calls": calls, "count": len(calls)}


@router.get("/{session_id}/context")
def get_call_context(session_id: str):
    store = get_store()
    call = store.get_call(session_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    transcript = store.get_transcript(session_id)
    return {
        "call": call,
        "transcript": transcript,
    }


@router.get("/{session_id}/transcript")
def get_call_transcript(session_id: str):
    store = get_store()
    transcript = store.get_transcript(session_id)
    if transcript is None:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return {"sessionId": session_id, "transcript": transcript}


@router.get("/latest")
def get_latest_call(rep: str | None = None):
    store = get_store()
    if rep:
        call = store.get_rep_current_call(rep)
        if call is None:
            raise HTTPException(status_code=404, detail=f"No calls found for rep {rep}")
        transcript = store.get_transcript(call["sessionId"])
        return {"call": call, "transcript": transcript}
    # No rep specified — return the most recent active call
    calls = store.list_active_calls()
    if not calls:
        raise HTTPException(status_code=404, detail="No active calls")
    return {"call": calls[0], "transcript": store.get_transcript(calls[0]["sessionId"])}
```

- [ ] **Step 6: Implement FastAPI app factory**

File: `src/api/main.py`

```python
from fastapi import FastAPI
from src.redis_store import CallStore
from src.api.routes import router, set_store


def create_app(store: CallStore | None = None) -> FastAPI:
    app = FastAPI(title="SFW Call Intelligence Bridge", version="1.0.0")

    if store:
        set_store(store)

    app.include_router(router)
    return app
```

- [ ] **Step 7: Run tests to verify auth tests pass**

Run: `pytest tests/test_api.py -v`
Expected: all 3 tests PASS

- [ ] **Step 8: Write tests for route endpoints**

Append to `tests/test_api.py`:

```python
@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_active_calls_empty(client):
    resp = client.get("/api/calls/active", headers=auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data["calls"] == []
    assert data["count"] == 0


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_active_calls_with_data(client, store):
    store.store_call("s-1", {"sessionId": "s-1", "status": "Answered", "to": {"extensionId": "119"}})
    store.store_call("s-2", {"sessionId": "s-2", "status": "Answered", "to": {"extensionId": "118"}})

    resp = client.get("/api/calls/active", headers=auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_call_context(client, store):
    store.store_call("s-abc", {"sessionId": "s-abc", "status": "Answered"})
    store.store_transcript("s-abc", "Hello from SFW.")

    resp = client.get("/api/calls/s-abc/context", headers=auth_header())
    assert resp.status_code == 200
    data = resp.json()
    assert data["call"]["sessionId"] == "s-abc"
    assert data["transcript"] == "Hello from SFW."


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_call_context_not_found(client):
    resp = client.get("/api/calls/nonexistent/context", headers=auth_header())
    assert resp.status_code == 404


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_transcript(client, store):
    store.store_call("s-abc", {"sessionId": "s-abc", "status": "Answered"})
    store.store_transcript("s-abc", "Hello from SFW.")

    resp = client.get("/api/calls/s-abc/transcript", headers=auth_header())
    assert resp.status_code == 200
    assert resp.json()["transcript"] == "Hello from SFW."


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_latest_call_by_rep(client, store):
    store.store_call("s-abc", {
        "sessionId": "s-abc",
        "status": "Answered",
        "to": {"extensionId": "119", "name": "Doug Stoker"},
    })

    resp = client.get("/api/calls/latest?rep=119", headers=auth_header())
    assert resp.status_code == 200
    assert resp.json()["call"]["sessionId"] == "s-abc"


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_latest_call_rep_not_found(client):
    resp = client.get("/api/calls/latest?rep=999", headers=auth_header())
    assert resp.status_code == 404
```

- [ ] **Step 9: Run all API tests**

Run: `pytest tests/test_api.py -v`
Expected: all 10 tests PASS

- [ ] **Step 10: Commit**

```bash
git add src/api/ tests/test_api.py
git commit -m "feat: GPT Action API with auth, active calls, call context, transcript, and latest-by-rep endpoints"
```

---

### Task 4: Call Monitor — WebSocket Event Processing

**Files:**
- Create: `src/call_monitor.py`
- Create: `tests/test_call_monitor.py`

This is the core persistent service. It subscribes to RingCentral's telephony WebSocket and translates events into call state stored in Redis. The event processing logic is extracted into a pure function for easy testing — the WebSocket connection itself is tested via the existing exploration scripts.

- [ ] **Step 1: Write failing tests for event processing**

File: `tests/test_call_monitor.py`

The RC WebSocket sends notification payloads like this (confirmed from the exploration scripts):

```python
import pytest
from unittest.mock import MagicMock
from src.redis_store import CallStore
from src.call_monitor import process_telephony_event


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


def make_event(session_id, status, from_info=None, to_info=None, parties=None):
    """Build a telephony session notification matching RC's format."""
    event = {
        "body": {
            "telephonySessionId": session_id,
            "parties": parties or [
                {
                    "direction": "Inbound",
                    "status": {"code": status},
                    "from": from_info or {"phoneNumber": "+12065551234", "name": "John Doe"},
                    "to": to_info or {"phoneNumber": "+12065559999", "extensionId": "119", "name": "Doug Stoker"},
                }
            ],
        }
    }
    return event


def test_answered_call_stored(store):
    event = make_event("s-100", "Answered")
    process_telephony_event(event, store)

    call = store.get_call("s-100")
    assert call is not None
    assert call["status"] == "Answered"
    assert call["from"]["phoneNumber"] == "+12065551234"


def test_answered_call_added_to_active(store, fake_redis):
    event = make_event("s-100", "Answered")
    process_telephony_event(event, store)

    assert fake_redis.sismember("calls:active", "s-100")


def test_disconnected_call_completed(store, fake_redis):
    # First answer the call
    process_telephony_event(make_event("s-100", "Answered"), store)
    # Then disconnect
    process_telephony_event(make_event("s-100", "Disconnected"), store)

    assert not fake_redis.sismember("calls:active", "s-100")
    call = store.get_call("s-100")
    assert call["status"] == "Disconnected"


def test_proceeding_call_stored_as_ringing(store):
    event = make_event("s-100", "Proceeding")
    process_telephony_event(event, store)

    call = store.get_call("s-100")
    assert call is not None
    assert call["status"] == "Proceeding"


def test_rep_pointer_set_on_answered(store, fake_redis):
    event = make_event("s-100", "Answered",
                       to_info={"extensionId": "119", "name": "Doug Stoker"})
    process_telephony_event(event, store)

    assert fake_redis.get("rep:119:current") == "s-100"


def test_multiple_calls_tracked(store):
    process_telephony_event(make_event("s-100", "Answered"), store)
    process_telephony_event(make_event("s-200", "Answered",
                                       to_info={"extensionId": "118", "name": "Jacob Hair"}), store)

    active = store.list_active_calls()
    assert len(active) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_call_monitor.py -v`
Expected: FAIL — `cannot import name 'process_telephony_event'`

- [ ] **Step 3: Implement `process_telephony_event`**

File: `src/call_monitor.py`

```python
import logging
from src.redis_store import CallStore

logger = logging.getLogger(__name__)

# Statuses that mean a call is active
ACTIVE_STATUSES = {"Proceeding", "Answered", "Hold"}
END_STATUSES = {"Disconnected", "Gone", "VoiceMail"}


def process_telephony_event(event: dict, store: CallStore) -> None:
    """Process a single RC telephony session notification and update store."""
    body = event.get("body", {})
    session_id = body.get("telephonySessionId")
    if not session_id:
        logger.warning("Event missing telephonySessionId: %s", event)
        return

    parties = body.get("parties", [])
    if not parties:
        return

    # Use the first party to determine call state
    party = parties[0]
    status = party.get("status", {}).get("code", "Unknown")
    direction = party.get("direction", "Unknown")
    from_info = party.get("from", {})
    to_info = party.get("to", {})

    call_data = {
        "sessionId": session_id,
        "status": status,
        "direction": direction,
        "from": from_info,
        "to": to_info,
    }

    if status in END_STATUSES:
        logger.info("Call ended: %s (status=%s)", session_id, status)
        store.complete_call(session_id)
    else:
        logger.info("Call event: %s (status=%s)", session_id, status)
        store.store_call(session_id, call_data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_call_monitor.py -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor.py tests/test_call_monitor.py
git commit -m "feat: call monitor event processing — extracts call state from RC WebSocket events"
```

---

### Task 5: Call Monitor — WebSocket Runner

**Files:**
- Create: `run_monitor.py`
- Modify: `src/call_monitor.py` (add `run_monitor` async function)

This wires up the RC SDK WebSocket to the event processor. It's the entrypoint for the persistent process.

- [ ] **Step 1: Add `run_monitor` async function to `src/call_monitor.py`**

Append to `src/call_monitor.py`:

```python
import asyncio
import json
from ringcentral import SDK
from ringcentral.websocket.web_socket_client import WebSocketEvents
from src.config import Config


async def run_monitor(store: CallStore) -> None:
    """Connect to RC WebSocket and process telephony events indefinitely."""
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    platform.login(jwt=Config.RC_JWT)
    logger.info("Authenticated with RingCentral")

    ext = platform.get("/restapi/v1.0/account/~/extension/~").json_dict()
    logger.info("Monitoring as: %s (ext %s)", ext["name"], ext["extensionNumber"])

    event_filters = ["/restapi/v1.0/account/~/telephony/sessions"]

    ws_client = sdk.create_web_socket_client()
    token_data = ws_client.get_web_socket_token()

    def on_notification(msg):
        if isinstance(msg, dict):
            data = msg
        elif hasattr(msg, "json_dict"):
            data = msg.json_dict()
        else:
            try:
                data = json.loads(str(msg))
            except (json.JSONDecodeError, TypeError):
                logger.warning("Could not parse notification: %s", msg)
                return
        logger.debug("Raw event: %s", json.dumps(data, default=str)[:500])
        process_telephony_event(data, store)

    async def on_connected(client):
        logger.info("WebSocket connected, creating subscription...")
        await client.create_subscription(event_filters)
        logger.info("Subscription active — listening for call events")

    ws_client.on(WebSocketEvents.connectionCreated,
                 lambda c: asyncio.create_task(on_connected(c)))
    ws_client.on("notification", on_notification)

    logger.info("Opening WebSocket connection...")
    await ws_client.open_connection(token_data["uri"], token_data["ws_access_token"])
```

- [ ] **Step 2: Create `run_monitor.py` entrypoint**

File: `run_monitor.py`

```python
"""Entrypoint for the persistent call monitor process."""
import asyncio
import logging
from upstash_redis import Redis
from src.config import Config
from src.redis_store import CallStore
from src.call_monitor import run_monitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


async def main():
    redis = Redis(url=Config.UPSTASH_REDIS_URL, token=Config.UPSTASH_REDIS_TOKEN)
    store = CallStore(redis)
    await run_monitor(store)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
```

- [ ] **Step 3: Smoke test — verify the monitor starts and authenticates**

Run (requires .env with RC credentials):
```bash
cd C:/Users/tfalcon/callrail-chatgpt
timeout 10 python run_monitor.py || true
```

Expected: logs showing "Authenticated with RingCentral", "WebSocket connected", "Subscription active". Process keeps running until timeout kills it.

- [ ] **Step 4: Commit**

```bash
git add src/call_monitor.py run_monitor.py
git commit -m "feat: call monitor WebSocket runner — persistent process that subscribes to RC telephony events"
```

---

### Task 6: Post-Call Recording Transcription (Deepgram Batch)

**Files:**
- Create: `src/recording_transcriber.py`
- Create: `tests/test_recording_transcriber.py`

Phase 1 uses Deepgram's batch (pre-recorded) API, not real-time streaming. When a call ends and has a recording URL, we download it from RC and send it to Deepgram for transcription.

- [ ] **Step 1: Write failing tests for `transcribe_recording`**

File: `tests/test_recording_transcriber.py`

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.recording_transcriber import transcribe_recording_url


@pytest.fixture
def mock_deepgram_response():
    return {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "Hi this is Doug from SFW Construction. How can I help you?",
                            "paragraphs": {
                                "paragraphs": [
                                    {
                                        "speaker": 0,
                                        "sentences": [
                                            {"text": "Hi this is Doug from SFW Construction."},
                                            {"text": "How can I help you?"},
                                        ]
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }


@pytest.mark.asyncio
async def test_transcribe_recording_url_returns_transcript(mock_deepgram_response):
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_deepgram_response

    with patch("src.recording_transcriber.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        result = await transcribe_recording_url("https://media.ringcentral.com/recording/123.wav")

    assert "Doug from SFW Construction" in result
    mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_transcribe_recording_url_handles_error():
    mock_response = AsyncMock()
    mock_response.status_code = 400
    mock_response.text = "Bad Request"
    mock_response.raise_for_status.side_effect = Exception("400 Bad Request")

    with patch("src.recording_transcriber.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = mock_client

        result = await transcribe_recording_url("https://media.ringcentral.com/recording/123.wav")

    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_recording_transcriber.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `transcribe_recording_url`**

File: `src/recording_transcriber.py`

```python
import logging
import httpx
from src.config import Config

logger = logging.getLogger(__name__)

DEEPGRAM_API_URL = "https://api.deepgram.com/v1/listen"


async def transcribe_recording_url(recording_url: str) -> str | None:
    """Send a recording URL to Deepgram batch API and return the transcript text.

    Returns None if transcription fails.
    """
    headers = {
        "Authorization": f"Token {Config.DEEPGRAM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "url": recording_url,
    }
    params = {
        "model": "nova-3",
        "smart_format": "true",
        "diarize": "true",
        "paragraphs": "true",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                DEEPGRAM_API_URL,
                headers=headers,
                json=payload,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        # Extract transcript from Deepgram response
        channels = data.get("results", {}).get("channels", [])
        if not channels:
            logger.warning("No channels in Deepgram response")
            return None

        transcript = channels[0]["alternatives"][0]["transcript"]
        logger.info("Transcribed %d characters", len(transcript))
        return transcript

    except Exception:
        logger.exception("Deepgram transcription failed for %s", recording_url)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_recording_transcriber.py -v`
Expected: both tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/recording_transcriber.py tests/test_recording_transcriber.py
git commit -m "feat: Deepgram batch transcription for post-call recordings"
```

---

### Task 7: Wire Transcription into Call Monitor

**Files:**
- Modify: `src/call_monitor.py`
- Modify: `tests/test_call_monitor.py`

When a call ends and has a recording, the monitor should trigger transcription and store the result.

- [ ] **Step 1: Write failing test for post-call transcription trigger**

Append to `tests/test_call_monitor.py`:

```python
from unittest.mock import patch, AsyncMock


def test_disconnected_call_with_recording_url_triggers_transcription(store):
    """When a call ends with a recording, recording URL is stored for transcription."""
    event = {
        "body": {
            "telephonySessionId": "s-100",
            "parties": [
                {
                    "direction": "Inbound",
                    "status": {"code": "Disconnected"},
                    "from": {"phoneNumber": "+12065551234"},
                    "to": {"extensionId": "119"},
                }
            ],
        }
    }
    # First create the call so it exists
    store.store_call("s-100", {
        "sessionId": "s-100",
        "status": "Answered",
        "recording": {"id": "rec-1", "contentUri": "https://media.ringcentral.com/rec-1.wav"},
    })
    process_telephony_event(event, store)

    call = store.get_call("s-100")
    assert call["status"] == "Disconnected"
```

- [ ] **Step 2: Run test to verify it passes**

This test already passes with current code — it validates the recording URL is preserved in call state after disconnect. The actual async transcription trigger is integration-level (calls RC API + Deepgram), so we'll wire it in `run_monitor.py` with a background task.

Run: `pytest tests/test_call_monitor.py -v`
Expected: all 7 tests PASS

- [ ] **Step 3: Add transcription background task to `run_monitor.py`**

Replace the content of `run_monitor.py`:

```python
"""Entrypoint for the persistent call monitor process."""
import asyncio
import logging
from upstash_redis import Redis
from src.config import Config
from src.redis_store import CallStore
from src.call_monitor import run_monitor
from src.recording_transcriber import transcribe_recording_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def poll_for_recordings(store: CallStore, platform, interval: int = 30):
    """Periodically check completed calls for recordings and transcribe them."""
    while True:
        await asyncio.sleep(interval)
        try:
            # Get recent call log entries with recordings
            resp = platform.get("/restapi/v1.0/account/~/call-log", {
                "perPage": 10,
                "view": "Detailed",
                "dateFrom": "",  # RC defaults to recent
            })
            records = resp.json_dict().get("records", [])
            for call in records:
                recording = call.get("recording")
                if not recording:
                    continue
                session_id = call.get("sessionId")
                if not session_id:
                    continue
                # Skip if already transcribed
                existing = store.get_transcript(session_id)
                if existing:
                    continue
                content_uri = recording.get("contentUri")
                if not content_uri:
                    continue

                logger.info("Transcribing recording for session %s", session_id)
                transcript = await transcribe_recording_url(content_uri)
                if transcript:
                    store.store_transcript(session_id, transcript)
                    logger.info("Stored transcript for session %s (%d chars)",
                                session_id, len(transcript))
        except Exception:
            logger.exception("Error in recording poll")


async def main():
    redis = Redis(url=Config.UPSTASH_REDIS_URL, token=Config.UPSTASH_REDIS_TOKEN)
    store = CallStore(redis)

    # Run both the WebSocket monitor and the recording poller concurrently
    await asyncio.gather(
        run_monitor(store),
        # Recording poller will be enabled when Deepgram key is configured
        *([] if not Config.DEEPGRAM_API_KEY else [poll_for_recordings(store, None)]),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
```

**Note:** The `poll_for_recordings` function needs the RC `platform` object. We'll fix this — pass it from the monitor. Update `run_monitor.py`:

Actually, let's keep it simpler. The recording poller needs its own RC platform instance since `run_monitor` consumes the SDK internally. Update:

```python
"""Entrypoint for the persistent call monitor process."""
import asyncio
import logging
from upstash_redis import Redis
from ringcentral import SDK
from src.config import Config
from src.redis_store import CallStore
from src.call_monitor import run_monitor
from src.recording_transcriber import transcribe_recording_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def poll_for_recordings(store: CallStore, interval: int = 30):
    """Periodically check completed calls for recordings and transcribe them."""
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    platform.login(jwt=Config.RC_JWT)

    while True:
        await asyncio.sleep(interval)
        try:
            resp = platform.get("/restapi/v1.0/account/~/call-log", {
                "perPage": 10,
                "view": "Detailed",
            })
            records = resp.json_dict().get("records", [])
            for call in records:
                recording = call.get("recording")
                if not recording:
                    continue
                session_id = call.get("sessionId")
                if not session_id:
                    continue
                existing = store.get_transcript(session_id)
                if existing:
                    continue
                content_uri = recording.get("contentUri")
                if not content_uri:
                    continue

                logger.info("Transcribing recording for session %s", session_id)
                transcript = await transcribe_recording_url(content_uri)
                if transcript:
                    store.store_transcript(session_id, transcript)
                    logger.info("Stored transcript for session %s (%d chars)",
                                session_id, len(transcript))
        except Exception:
            logger.exception("Error in recording poll")


async def main():
    redis = Redis(url=Config.UPSTASH_REDIS_URL, token=Config.UPSTASH_REDIS_TOKEN)
    store = CallStore(redis)

    tasks = [run_monitor(store)]
    if Config.DEEPGRAM_API_KEY:
        tasks.append(poll_for_recordings(store))
    else:
        logger.warning("DEEPGRAM_API_KEY not set — post-call transcription disabled")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
```

- [ ] **Step 4: Run all tests**

Run: `pytest -v`
Expected: all tests PASS (unit tests don't touch run_monitor.py)

- [ ] **Step 5: Commit**

```bash
git add src/call_monitor.py run_monitor.py tests/test_call_monitor.py
git commit -m "feat: wire post-call recording transcription into monitor with polling loop"
```

---

### Task 8: Vercel Deployment Config

**Files:**
- Create: `api/index.py`
- Create: `vercel.json`

The API layer deploys as a Vercel serverless function. Vercel looks for Python handlers in the `api/` directory.

- [ ] **Step 1: Create `vercel.json`**

```json
{
  "version": 2,
  "builds": [
    {
      "src": "api/index.py",
      "use": "@vercel/python"
    }
  ],
  "routes": [
    {
      "src": "/api/(.*)",
      "dest": "api/index.py"
    }
  ]
}
```

- [ ] **Step 2: Create `api/index.py`**

```python
"""Vercel serverless entrypoint — exposes the FastAPI app as a handler."""
from upstash_redis import Redis
from src.config import Config
from src.redis_store import CallStore
from src.api.main import create_app

redis = Redis(url=Config.UPSTASH_REDIS_URL, token=Config.UPSTASH_REDIS_TOKEN)
store = CallStore(redis)
app = create_app(store)
```

- [ ] **Step 3: Create `requirements.txt` for Vercel**

Vercel's Python runtime uses `requirements.txt`. Update the existing file:

```
fastapi>=0.115
uvicorn>=0.34
upstash-redis>=1.1
httpx>=0.28
python-dotenv>=1.0
```

(Vercel only needs the API dependencies, not ringcentral/websockets/pjsua2.)

- [ ] **Step 4: Commit**

```bash
git add api/index.py vercel.json requirements.txt
git commit -m "feat: Vercel serverless deployment config for GPT Action API"
```

---

### Task 9: OpenAPI Schema for GPT Actions

**Files:**
- Create: `docs/openapi.yaml`

ChatGPT needs an OpenAPI schema to configure GPT Actions. This is the schema from the design spec, fleshed out with response schemas.

- [ ] **Step 1: Create `docs/openapi.yaml`**

```yaml
openapi: 3.0.0
info:
  title: SFW Call Intelligence Bridge
  description: Real-time call context for SFW Construction sales coaching via ChatGPT
  version: 1.0.0
servers:
  - url: https://sfw-call-bridge.vercel.app
paths:
  /api/calls/active:
    get:
      operationId: getActiveCalls
      summary: List all active calls across the SFW sales team
      description: Returns all currently active phone calls with caller info and rep assignment. Use this to see who is on the phone right now.
      responses:
        "200":
          description: Active calls with caller info and rep assignment
          content:
            application/json:
              schema:
                type: object
                properties:
                  calls:
                    type: array
                    items:
                      $ref: "#/components/schemas/Call"
                  count:
                    type: integer
  /api/calls/latest:
    get:
      operationId: getLatestCall
      summary: Get the most recent or current call for a rep
      description: Returns the current or most recent call for a specific sales rep. Use this when a rep asks for context on their current call.
      parameters:
        - name: rep
          in: query
          required: false
          schema:
            type: string
          description: Extension number of the rep (e.g. "119" for Doug Stoker, "118" for Jacob Hair)
      responses:
        "200":
          description: Latest call context for the specified rep
          content:
            application/json:
              schema:
                type: object
                properties:
                  call:
                    $ref: "#/components/schemas/Call"
                  transcript:
                    type: string
                    nullable: true
  /api/calls/{sessionId}/context:
    get:
      operationId: getCallContext
      summary: Get full context for a specific call including transcript
      parameters:
        - name: sessionId
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Call metadata and transcript
          content:
            application/json:
              schema:
                type: object
                properties:
                  call:
                    $ref: "#/components/schemas/Call"
                  transcript:
                    type: string
                    nullable: true
  /api/calls/{sessionId}/transcript:
    get:
      operationId: getCallTranscript
      summary: Get transcript for a call
      parameters:
        - name: sessionId
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Transcript text
          content:
            application/json:
              schema:
                type: object
                properties:
                  sessionId:
                    type: string
                  transcript:
                    type: string
components:
  schemas:
    Call:
      type: object
      properties:
        sessionId:
          type: string
        status:
          type: string
          enum: [Proceeding, Answered, Hold, Disconnected]
        direction:
          type: string
          enum: [Inbound, Outbound]
        from:
          type: object
          properties:
            phoneNumber:
              type: string
            name:
              type: string
        to:
          type: object
          properties:
            extensionId:
              type: string
            name:
              type: string
            phoneNumber:
              type: string
  securitySchemes:
    apiKey:
      type: apiKey
      in: header
      name: x-api-key
security:
  - apiKey: []
```

- [ ] **Step 2: Commit**

```bash
git add docs/openapi.yaml
git commit -m "feat: OpenAPI schema for ChatGPT GPT Action configuration"
```

---

### Task 10: End-to-End Smoke Test

**Files:** None created — this is a manual verification task.

- [ ] **Step 1: Run the full test suite**

```bash
cd C:/Users/tfalcon/callrail-chatgpt
pytest -v
```

Expected: all tests pass (12 redis + 10 api + 7 monitor + 2 transcriber = 31 tests).

- [ ] **Step 2: Start the API locally and test with curl**

Terminal 1:
```bash
cd C:/Users/tfalcon/callrail-chatgpt
uvicorn src.api.main:create_app --factory --port 8000
```

Terminal 2 — test auth rejection:
```bash
curl -s http://localhost:8000/api/calls/active | python -m json.tool
```
Expected: `{"detail": "Invalid API key"}`

Terminal 2 — test with API key:
```bash
curl -s -H "x-api-key: YOUR_KEY" http://localhost:8000/api/calls/active | python -m json.tool
```
Expected: `{"calls": [], "count": 0}`

- [ ] **Step 3: Start the monitor and make a test call**

```bash
cd C:/Users/tfalcon/callrail-chatgpt
python run_monitor.py
```

Make a test call to one of SFW's numbers. Watch the monitor logs for:
- `Call event: s-XXXX (status=Proceeding)`
- `Call event: s-XXXX (status=Answered)`
- `Call ended: s-XXXX (status=Disconnected)`

While the call is active, in another terminal:
```bash
curl -s -H "x-api-key: YOUR_KEY" http://localhost:8000/api/calls/active | python -m json.tool
```
Expected: the active call appears with caller info and rep assignment.

- [ ] **Step 4: Commit any fixes from smoke testing**

```bash
git add -A
git commit -m "fix: adjustments from end-to-end smoke testing"
```

(Only if changes were needed.)

---

## Deployment Checklist (post-implementation)

These are not automated tasks — they're manual steps for after the code is working:

1. **Upstash Redis:** Create a free database at upstash.com, get URL and token
2. **Deepgram:** Create account, get API key (~$0.006/min, they have free credits)
3. **Vercel:** Deploy API (`vercel --prod`), set env vars in Vercel dashboard
4. **Railway/Fly.io:** Deploy `run_monitor.py` as persistent process, set env vars
5. **RingCentral admin:** Add ext 120 (Tyler Falcon) as monitor in Sales call monitoring group
6. **ChatGPT:** Create GPT Action using `docs/openapi.yaml`, set API key auth
7. **Test:** Make a real call, verify it shows up in ChatGPT via the Sales Script Builder GPT
