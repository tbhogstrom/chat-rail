# Sell-o-meter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live, points-based sell-o-meter on the agents dashboard that scores checkpoints (name, phone, email, sales script opened, agreement opened) per active call, records a per-minute score timeline, and persists a final JSON record per rep.

**Architecture:** A new engine worker (Fly, always-on) computes the score every 3s from already-extracted fields plus a new per-call events key, writing `call:{sid}:sellometer` to Redis; dashboards poll a new read endpoint; button clicks POST checkpoint events; when a call drops out of the extraction set the worker finalizes the record into `sellometer:history:{extId}`.

**Tech Stack:** Python 3 / FastAPI / upstash-redis (fakeredis in tests) / pytest / vanilla-JS static dashboard. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-03-sellometer-design.md`

## Model Routing

Planning was done by a frontier model (Fable 5). Recommended execution model per task — use the cheapest model that can hold the task's full complexity:

| Task | Work | Recommended model | Why |
|---|---|---|---|
| 1 | Scoring config + pure scoring module | `claude-sonnet-5` | Pure functions with edge cases; needs solid test judgment, no cross-system reasoning |
| 2 | CallStore Redis methods | `claude-haiku-4-5-20251001` | Mechanical pattern-following of existing store methods |
| 3 | Sellometer worker (timeline + finalization) + engine wiring | `claude-opus-4-8` | The one genuinely subtle task: in-memory session tracking, minute-boundary timeline, finalization races |
| 4 | API endpoints | `claude-sonnet-5` | Standard FastAPI endpoints, but validation + route-shape decisions matter |
| 5 | Dashboard UI + tool-event config | `claude-sonnet-5` | UI work against an existing hand-rolled design system |
| 6 | Full-suite verification + deploy checklist | `claude-haiku-4-5-20251001` | Run commands, read output, report |

## Global Constraints

- Store every new Redis value as a **single JSON string, never a hash** — hashes don't round-trip identically on upstash-redis and fakeredis (see comment on `CallStore.set_rep_roster`, `src/redis_store.py:120-126`).
- New call-scoped keys use the `call:{session_id}:<name>` pattern with `ttl=3600` (matches `set_extracted`).
- Timestamps in stored JSON are ISO-8601 UTC strings from `datetime.now(timezone.utc).isoformat()`.
- Checkpoint point values (v1): caller-name 10, phone 15, email 15, sales-script 25, agreement 35 — total 100.
- Timeline semantics: `timeline[i]` = score at the **end** of call minute `i`; append only when a minute boundary has passed (`len(timeline) < elapsed_minutes`).
- Commit messages: conventional-commit style, **no Co-Authored-By / Claude credit trailers** (repo preference).
- Run tests with `python -m pytest` from the repo root (Windows; PowerShell or Git Bash both fine).

---

### Task 1: Scoring config + pure scoring module

**Files:**
- Create: `src/sellometer.json`
- Create: `src/sellometer.py`
- Test: `tests/test_sellometer.py`

**Interfaces:**
- Consumes: nothing (leaf module — stdlib only).
- Produces (used by Tasks 3 and 4):
  - `load_config() -> dict` — cached load of `src/sellometer.json`; raises `ValueError`/`OSError` on bad/missing file.
  - `known_event_ids(config: dict) -> set[str]`
  - `compute_score(config: dict, extracted: dict, events: dict) -> dict` returning `{"score": int, "max": int, "checkpoints": [{"id","label","points","hit","ts"}]}`
  - `advance_timeline(timeline: list[int], started_at: datetime, now: datetime, score: int) -> list[int]` (mutates and returns `timeline`)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sellometer.py`:

```python
from datetime import datetime, timedelta, timezone

from src.sellometer import (
    advance_timeline,
    compute_score,
    known_event_ids,
    load_config,
)


def _config():
    return {
        "version": 1,
        "checkpoints": [
            {"id": "caller-name", "label": "Name", "points": 10,
             "detect": {"type": "extracted_field", "field": "firstname"}},
            {"id": "email", "label": "Email", "points": 15,
             "detect": {"type": "event", "event": "email-entered"}},
        ],
    }


def test_load_config_real_file_totals_100():
    config = load_config()
    assert config["version"] == 1
    assert sum(cp["points"] for cp in config["checkpoints"]) == 100
    ids = [cp["id"] for cp in config["checkpoints"]]
    assert ids == ["caller-name", "phone", "email", "sales-script", "agreement"]


def test_known_event_ids_only_event_checkpoints():
    assert known_event_ids(_config()) == {"email-entered"}


def test_compute_score_empty_inputs():
    result = compute_score(_config(), {}, {})
    assert result["score"] == 0
    assert result["max"] == 25
    assert [cp["hit"] for cp in result["checkpoints"]] == [False, False]
    assert all(cp["ts"] is None for cp in result["checkpoints"])


def test_compute_score_extracted_field_hit():
    result = compute_score(_config(), {"firstname": "Jim"}, {})
    assert result["score"] == 10
    by_id = {cp["id"]: cp for cp in result["checkpoints"]}
    assert by_id["caller-name"]["hit"] is True
    assert by_id["caller-name"]["ts"] is None  # extracted fields carry no ts


def test_compute_score_event_hit_carries_timestamp():
    result = compute_score(_config(), {}, {"email-entered": "2026-07-03T14:11:00+00:00"})
    by_id = {cp["id"]: cp for cp in result["checkpoints"]}
    assert by_id["email"]["hit"] is True
    assert by_id["email"]["ts"] == "2026-07-03T14:11:00+00:00"
    assert result["score"] == 15


def test_compute_score_all_hit():
    result = compute_score(_config(), {"firstname": "Jim"},
                           {"email-entered": "2026-07-03T14:11:00+00:00"})
    assert result["score"] == result["max"] == 25


def test_compute_score_empty_string_field_is_not_hit():
    result = compute_score(_config(), {"firstname": ""}, {})
    assert result["score"] == 0


def test_compute_score_none_inputs_treated_as_empty():
    result = compute_score(_config(), None, None)
    assert result["score"] == 0


def test_compute_score_unknown_detect_type_skipped():
    config = {"checkpoints": [
        {"id": "phrase", "label": "Phrase", "points": 50,
         "detect": {"type": "phrase", "patterns": ["guarantee"]}},
        {"id": "caller-name", "label": "Name", "points": 10,
         "detect": {"type": "extracted_field", "field": "firstname"}},
    ]}
    result = compute_score(config, {"firstname": "Jim"}, {})
    # Unknown type contributes to neither score nor max, and emits no checkpoint.
    assert result["score"] == 10
    assert result["max"] == 10
    assert [cp["id"] for cp in result["checkpoints"]] == ["caller-name"]


def _t(minutes: float) -> datetime:
    return datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def test_advance_timeline_first_minute_in_progress_appends_nothing():
    assert advance_timeline([], _t(0), _t(0.5), score=10) == []


def test_advance_timeline_appends_on_minute_boundary():
    assert advance_timeline([], _t(0), _t(1.05), score=10) == [10]


def test_advance_timeline_no_duplicate_within_same_minute():
    tl = advance_timeline([], _t(0), _t(1.05), score=10)
    assert advance_timeline(tl, _t(0), _t(1.9), score=25) == [10]


def test_advance_timeline_backfills_gap_with_current_score():
    # Worker stalled for 3 minutes: missing minutes get the catch-up score.
    assert advance_timeline([10], _t(0), _t(4.2), score=40) == [10, 40, 40, 40]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sellometer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.sellometer'`

- [ ] **Step 3: Create the config file**

Create `src/sellometer.json`:

```json
{
  "version": 1,
  "checkpoints": [
    {"id": "caller-name", "label": "Name", "points": 10,
     "detect": {"type": "extracted_field", "field": "firstname"}},
    {"id": "phone", "label": "Phone", "points": 15,
     "detect": {"type": "extracted_field", "field": "phone"}},
    {"id": "email", "label": "Email", "points": 15,
     "detect": {"type": "extracted_field", "field": "email"}},
    {"id": "sales-script", "label": "Sales Script", "points": 25,
     "detect": {"type": "event", "event": "sales-script-opened"}},
    {"id": "agreement", "label": "Agreement", "points": 35,
     "detect": {"type": "event", "event": "agreement-opened"}}
  ]
}
```

- [ ] **Step 4: Write the module**

Create `src/sellometer.py`:

```python
"""Sell-o-meter scoring: config loading and pure score computation.

src/sellometer.json defines the checkpoints. Detect types:
  extracted_field — hit when extracted[field] is truthy
  event           — hit when the event id appears in the call's events

Future detect types (e.g. transcript phrase matching) are added to
_DETECTORS; the JSON schema stays the same. Unknown types are skipped with
a warning so an older deploy doesn't crash on a newer config.
"""
import json
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "sellometer.json"


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config.get("checkpoints"), list):
        raise ValueError("sellometer config must have a 'checkpoints' list")
    return config


def known_event_ids(config: dict) -> set[str]:
    """Event ids referenced by event-type checkpoints (for API validation)."""
    return {
        cp["detect"]["event"]
        for cp in config["checkpoints"]
        if (cp.get("detect") or {}).get("type") == "event"
    }


def _detect_extracted_field(detect: dict, extracted: dict, events: dict):
    return bool(extracted.get(detect["field"])), None


def _detect_event(detect: dict, extracted: dict, events: dict):
    ts = events.get(detect["event"])
    return ts is not None, ts


_DETECTORS = {
    "extracted_field": _detect_extracted_field,
    "event": _detect_event,
}


def compute_score(config: dict, extracted: dict | None, events: dict | None) -> dict:
    """Score a call right now. Pure — no I/O, no clock.

    extracted: extractor output for the call (call:{sid}:extracted).
    events:    {event_id: iso8601_ts} (call:{sid}:events).
    """
    extracted = extracted or {}
    events = events or {}
    checkpoints = []
    score = 0
    total = 0
    for cp in config["checkpoints"]:
        detect = cp.get("detect") or {}
        detector = _DETECTORS.get(detect.get("type"))
        if detector is None:
            logger.warning("Unknown sellometer detect type %r (checkpoint %r) — skipped",
                           detect.get("type"), cp.get("id"))
            continue
        hit, ts = detector(detect, extracted, events)
        total += cp["points"]
        if hit:
            score += cp["points"]
        checkpoints.append({"id": cp["id"], "label": cp["label"],
                            "points": cp["points"], "hit": hit, "ts": ts})
    return {"score": score, "max": total, "checkpoints": checkpoints}


def advance_timeline(timeline: list[int], started_at: datetime, now: datetime,
                     score: int) -> list[int]:
    """Append `score` once per completed call minute not yet recorded.

    timeline[i] is the score at the end of call minute i. Gaps (a stalled
    worker) are back-filled with the score known at catch-up time. Mutates
    and returns `timeline`.
    """
    elapsed = int((now - started_at).total_seconds() // 60)
    while len(timeline) < elapsed:
        timeline.append(score)
    return timeline
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_sellometer.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/sellometer.json src/sellometer.py tests/test_sellometer.py
git commit -m "feat(sellometer): scoring config and pure compute module"
```

---

### Task 2: CallStore methods for events, live score, and per-rep history

**Files:**
- Modify: `src/redis_store.py` (append methods to `CallStore`, after `get_recent_calls`)
- Test: `tests/test_redis_store.py` (append tests)

**Interfaces:**
- Consumes: existing `CallStore.redis` client.
- Produces (used by Tasks 3 and 4):
  - `add_call_event(session_id: str, event_id: str, ts: str, ttl: int = 3600) -> None` — idempotent, first ts wins
  - `get_call_events(session_id: str) -> dict` — `{event_id: iso_ts}`, `{}` if none
  - `set_sellometer(session_id: str, data: dict, ttl: int = 3600) -> None`
  - `get_sellometer(session_id: str) -> dict | None`
  - `push_sellometer_final(ext_id: str, record: dict, keep: int = 500) -> None`
  - `get_sellometer_history(ext_id: str, limit: int = 50) -> list[dict]` — newest first

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_redis_store.py`. The `fake_redis` fixture comes from `tests/conftest.py`; make sure the file's existing `from src.redis_store import CallStore` import is present (it is today):

```python
def test_call_events_roundtrip_and_idempotent(fake_redis):
    store = CallStore(fake_redis)
    assert store.get_call_events("s-1") == {}
    store.add_call_event("s-1", "sales-script-opened", "2026-07-03T14:11:00+00:00")
    store.add_call_event("s-1", "agreement-opened", "2026-07-03T14:12:00+00:00")
    # Duplicate click: first timestamp wins.
    store.add_call_event("s-1", "sales-script-opened", "2026-07-03T14:15:00+00:00")
    assert store.get_call_events("s-1") == {
        "sales-script-opened": "2026-07-03T14:11:00+00:00",
        "agreement-opened": "2026-07-03T14:12:00+00:00",
    }


def test_sellometer_roundtrip(fake_redis):
    store = CallStore(fake_redis)
    assert store.get_sellometer("s-1") is None
    data = {"score": 25, "max": 100, "checkpoints": [], "timeline": [0]}
    store.set_sellometer("s-1", data)
    assert store.get_sellometer("s-1") == data


def test_sellometer_history_newest_first_and_trimmed(fake_redis):
    store = CallStore(fake_redis)
    assert store.get_sellometer_history("576959052") == []
    store.push_sellometer_final("576959052", {"sessionId": "s-1", "score": 10})
    store.push_sellometer_final("576959052", {"sessionId": "s-2", "score": 20})
    records = store.get_sellometer_history("576959052")
    assert [r["sessionId"] for r in records] == ["s-2", "s-1"]
    # keep=1 trims to the newest record
    store.push_sellometer_final("576959052", {"sessionId": "s-3", "score": 30}, keep=1)
    assert [r["sessionId"] for r in store.get_sellometer_history("576959052")] == ["s-3"]


def test_sellometer_history_limit(fake_redis):
    store = CallStore(fake_redis)
    for i in range(5):
        store.push_sellometer_final("119", {"sessionId": f"s-{i}"})
    assert len(store.get_sellometer_history("119", limit=2)) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_redis_store.py -v`
Expected: new tests FAIL with `AttributeError: 'CallStore' object has no attribute 'add_call_event'` (existing tests still pass)

- [ ] **Step 3: Implement the methods**

Append to the `CallStore` class in `src/redis_store.py`:

```python
    # ---- sell-o-meter ----

    def add_call_event(self, session_id: str, event_id: str, ts: str,
                       ttl: int = 3600) -> None:
        """Record a checkpoint event for a call. Idempotent: first ts wins.

        Stored as a single JSON object ({event_id: iso_ts}), not a hash, so
        it round-trips identically on upstash-redis and fakeredis.
        """
        events = self.get_call_events(session_id)
        if event_id in events:
            return
        events[event_id] = ts
        self.redis.set(f"call:{session_id}:events", json.dumps(events))
        self.redis.expire(f"call:{session_id}:events", ttl)

    def get_call_events(self, session_id: str) -> dict:
        """Return {event_id: iso_ts} for a call, or {} if none recorded."""
        raw = self.redis.get(f"call:{session_id}:events")
        return json.loads(raw) if raw is not None else {}

    def set_sellometer(self, session_id: str, data: dict, ttl: int = 3600) -> None:
        """Persist the live sell-o-meter JSON for a session."""
        self.redis.set(f"call:{session_id}:sellometer", json.dumps(data))
        self.redis.expire(f"call:{session_id}:sellometer", ttl)

    def get_sellometer(self, session_id: str) -> dict | None:
        """Return the live sell-o-meter JSON, or None if not computed yet."""
        raw = self.redis.get(f"call:{session_id}:sellometer")
        return json.loads(raw) if raw is not None else None

    def push_sellometer_final(self, ext_id: str, record: dict,
                              keep: int = 500) -> None:
        """Prepend a final per-call sellometer record to the rep's history."""
        key = f"sellometer:history:{ext_id}"
        self.redis.lpush(key, json.dumps(record))
        self.redis.ltrim(key, 0, keep - 1)

    def get_sellometer_history(self, ext_id: str, limit: int = 50) -> list[dict]:
        """Return the rep's final sellometer records, newest first."""
        raw = self.redis.lrange(f"sellometer:history:{ext_id}", 0, limit - 1)
        return [json.loads(r) for r in raw]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_redis_store.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/redis_store.py tests/test_redis_store.py
git commit -m "feat(sellometer): CallStore events, live score, and per-rep history"
```

---

### Task 3: Sellometer worker — live compute, minute timeline, finalization; engine wiring

**Files:**
- Create: `src/sellometer_worker.py`
- Modify: `run_engine.py` (add worker to the `asyncio.gather` at lines 47-51)
- Test: `tests/test_sellometer_worker.py`

**Interfaces:**
- Consumes:
  - `CallStore.list_active_sessions/get_extracted/get_call_events/get_sellometer/set_sellometer/get_call/push_sellometer_final` (Task 2)
  - `src.sellometer.load_config/compute_score/advance_timeline` (Task 1)
  - `Config.MONITORED_EXTENSIONS` (`src/config.py:57`)
- Produces:
  - `run_sellometer_cycle(store: CallStore, tracked: set[str], now: datetime | None = None) -> None` (exposed for tests; `tracked` is worker-local memory of sessions seen while live)
  - `run_sellometer_worker(store: CallStore, interval: float = 3.0) -> None` (async forever-loop, error-swallowing — mirrors `run_extraction_worker`, `src/extraction_worker.py:31-41`)

**Design notes (read before coding):**
- The extraction set (`list_active_sessions`) includes active calls plus recently-ended calls inside the 60s grace window. A session "ends" for our purposes when it **drops out of that set** — that is the finalization trigger, and it is detected by diffing against the in-memory `tracked` set. Known v1 limitation (accepted in spec review): if the engine restarts between call end and grace expiry, that call's final record is skipped.
- `list_active_sessions` lazily cleans stale entries, and the extraction worker also calls it — in-memory tracking is what makes finalization independent of which worker's call did the cleanup.
- The rep extension id is resolved at finalization time from call state (still present — `complete_call` keeps it for `ttl=3600`): prefer the intersection of the call's `activeExtIds` with `Config.MONITORED_EXTENSIONS` (correct for queue-routed calls), falling back to `to.extensionId` then `from.extensionId` (mirrors `store_call`, `src/redis_store.py:21-23`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sellometer_worker.py`:

```python
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.redis_store import CallStore
from src.sellometer_worker import run_sellometer_cycle, run_sellometer_worker

T0 = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)


def _t(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


def _start_call(store, sid="s-1", ext="576959052"):
    store.store_call(sid, {"sessionId": sid, "status": "Answered",
                           "to": {"extensionId": ext},
                           "activeExtIds": [ext]})


def test_cycle_writes_sellometer_with_started_at(store):
    _start_call(store)
    store.set_extracted("s-1", {"firstname": "Jim"})
    tracked = set()
    run_sellometer_cycle(store, tracked, now=_t(0))
    sm = store.get_sellometer("s-1")
    assert sm["score"] == 10
    assert sm["max"] == 100
    assert sm["startedAt"] == _t(0).isoformat()
    assert sm["timeline"] == []          # first minute still in progress
    assert sm["updatedAt"] == _t(0).isoformat()
    assert "s-1" in tracked


def test_cycle_preserves_started_at_and_grows_timeline(store):
    _start_call(store)
    store.set_extracted("s-1", {"firstname": "Jim"})
    tracked = set()
    run_sellometer_cycle(store, tracked, now=_t(0))
    store.add_call_event("s-1", "sales-script-opened", _t(1).isoformat())
    run_sellometer_cycle(store, tracked, now=_t(1.1))
    sm = store.get_sellometer("s-1")
    assert sm["startedAt"] == _t(0).isoformat()
    assert sm["score"] == 35             # name 10 + script 25
    assert sm["timeline"] == [35]        # end of minute 0
    by_id = {cp["id"]: cp for cp in sm["checkpoints"]}
    assert by_id["sales-script"]["ts"] == _t(1).isoformat()


def test_cycle_finalizes_when_session_leaves_active_set(store, fake_redis):
    _start_call(store)
    store.set_extracted("s-1", {"firstname": "Jim", "phone": "5034441123"})
    tracked = set()
    run_sellometer_cycle(store, tracked, now=_t(0))
    run_sellometer_cycle(store, tracked, now=_t(2.1))

    # Call ends; grace window expires (simulate by deleting the marker).
    store.complete_call("s-1")
    fake_redis.delete("call:s-1:extract-grace")

    run_sellometer_cycle(store, tracked, now=_t(3))
    records = store.get_sellometer_history("576959052")
    assert len(records) == 1
    rec = records[0]
    assert rec["sessionId"] == "s-1"
    assert rec["repExtId"] == "576959052"
    assert rec["score"] == 25
    assert rec["timeline"] == [25, 25, 25]   # minutes 0,1 + final append
    assert rec["startedAt"] == _t(0).isoformat()
    assert rec["endedAt"] == _t(3).isoformat()
    assert "s-1" not in tracked
    # Not finalized twice on the next cycle.
    run_sellometer_cycle(store, tracked, now=_t(3.1))
    assert len(store.get_sellometer_history("576959052")) == 1


def test_finalize_prefers_monitored_active_ext(store, fake_redis):
    with patch("src.sellometer_worker.Config") as MockConfig:
        MockConfig.MONITORED_EXTENSIONS = ["119"]
        store.store_call("s-q", {"sessionId": "s-q", "status": "Answered",
                                 "to": {"extensionId": "999"},   # queue ext
                                 "activeExtIds": ["119", "999"]})
        store.set_extracted("s-q", {"firstname": "Ann"})
        tracked = set()
        run_sellometer_cycle(store, tracked, now=_t(0))
        store.complete_call("s-q")
        fake_redis.delete("call:s-q:extract-grace")
        run_sellometer_cycle(store, tracked, now=_t(1))
        assert store.get_sellometer_history("119")[0]["repExtId"] == "119"


def test_finalize_without_sellometer_is_skipped(store, fake_redis):
    """A session that vanishes before any sellometer was written is dropped
    quietly (nothing to record)."""
    _start_call(store, sid="s-ghost")
    tracked = {"s-ghost"}
    store.complete_call("s-ghost")
    fake_redis.delete("call:s-ghost:extract-grace")
    fake_redis.delete("call:s-ghost:sellometer")
    run_sellometer_cycle(store, tracked, now=_t(1))
    assert store.get_sellometer_history("576959052") == []
    assert "s-ghost" not in tracked


@pytest.mark.asyncio
async def test_worker_loop_swallows_per_iteration_errors():
    store = MagicMock()
    calls = []

    def flaky_list():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return []

    store.list_active_sessions.side_effect = flaky_list
    task = asyncio.create_task(run_sellometer_worker(store, interval=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(calls) >= 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sellometer_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.sellometer_worker'`

- [ ] **Step 3: Implement the worker**

Create `src/sellometer_worker.py`:

```python
"""Background worker: computes the live sell-o-meter for every session the
extraction worker is tracking, and finalizes a per-rep history record when a
session drops out of that set (post-disconnect grace window expired).

Finalization relies on worker-local memory (`tracked`): a session seen live
in an earlier cycle that is absent from the current cycle gets finalized
exactly once. If the engine restarts between a call's end and its grace
expiry, that call's final record is skipped — accepted v1 limitation.
"""
import asyncio
import logging
from datetime import datetime, timezone

from src.config import Config
from src.redis_store import CallStore
from src.sellometer import advance_timeline, compute_score, load_config

logger = logging.getLogger(__name__)


def _rep_ext_id(call: dict) -> str | None:
    """The monitored rep on a call. Queue-routed calls carry the queue's
    extensionId in to/from, so prefer a monitored member of activeExtIds."""
    active = [str(e) for e in (call.get("activeExtIds") or [])]
    monitored = [e for e in active if e in Config.MONITORED_EXTENSIONS]
    if monitored:
        return monitored[0]
    ext = (call.get("to") or {}).get("extensionId") \
        or (call.get("from") or {}).get("extensionId")
    return str(ext) if ext else None


def run_sellometer_cycle(store: CallStore, tracked: set[str],
                         now: datetime | None = None) -> None:
    """One pass. Exposed (with injectable `now`) for unit testing."""
    now = now or datetime.now(timezone.utc)
    config = load_config()
    current = set(store.list_active_sessions())

    for sid in current:
        extracted = store.get_extracted(sid)
        events = store.get_call_events(sid)
        result = compute_score(config, extracted, events)
        prev = store.get_sellometer(sid) or {}
        started_at_iso = prev.get("startedAt") or now.isoformat()
        timeline = advance_timeline(prev.get("timeline") or [],
                                    datetime.fromisoformat(started_at_iso),
                                    now, result["score"])
        result["startedAt"] = started_at_iso
        result["timeline"] = timeline
        result["updatedAt"] = now.isoformat()
        store.set_sellometer(sid, result)
    tracked |= current

    for sid in list(tracked - current):
        tracked.discard(sid)
        final = store.get_sellometer(sid)
        if final is None:
            continue  # never scored — nothing to record
        ext_id = _rep_ext_id(store.get_call(sid) or {})
        if not ext_id:
            logger.warning("Sellometer final for %s has no rep ext id — dropped", sid)
            continue
        timeline = list(final.get("timeline") or [])
        timeline.append(final["score"])  # score at call end closes the series
        store.push_sellometer_final(ext_id, {
            "sessionId": sid,
            "repExtId": ext_id,
            "score": final["score"],
            "max": final["max"],
            "checkpoints": final["checkpoints"],
            "startedAt": final.get("startedAt"),
            "timeline": timeline,
            "endedAt": now.isoformat(),
        })
        logger.info("Sellometer finalized %s for rep %s: %s/%s",
                    sid, ext_id, final["score"], final["max"])


async def run_sellometer_worker(store: CallStore, interval: float = 3.0) -> None:
    """Forever loop: one cycle every `interval` seconds. Swallows per-cycle
    errors so a bad session (or missing config) doesn't take down the engine.
    """
    logger.info("Sellometer worker starting (interval=%ss)", interval)
    tracked: set[str] = set()
    while True:
        try:
            run_sellometer_cycle(store, tracked)
        except Exception:
            logger.exception("Sellometer worker cycle failed")
        await asyncio.sleep(interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sellometer_worker.py -v`
Expected: all PASS

- [ ] **Step 5: Wire into the engine**

In `run_engine.py`, add the import after the `run_extraction_worker` import:

```python
from src.sellometer_worker import run_sellometer_worker
```

and add the worker to the gather (lines 47-51 become):

```python
    await asyncio.gather(
        run_monitor(store, sidecar=sidecar),
        run_extraction_worker(store),
        run_sellometer_worker(store),
        run_metrics_worker(store),
    )
```

- [ ] **Step 6: Run the engine tests plus the new suite**

Run: `python -m pytest tests/test_run_engine.py tests/test_sellometer_worker.py tests/test_sellometer.py -v`
Expected: all PASS (test_run_engine.py only tests `build_store`/`build_sidecar`; no changes needed)

- [ ] **Step 7: Commit**

```bash
git add src/sellometer_worker.py tests/test_sellometer_worker.py run_engine.py
git commit -m "feat(sellometer): engine worker with per-minute timeline and per-rep finalization"
```

---

### Task 4: API endpoints — record events, read live score, read rep history

**Files:**
- Modify: `src/api/models.py` (append one model)
- Modify: `src/api/routes.py` (three endpoints on the existing `/api/calls` router; insert after `get_extracted`, `src/api/routes.py:151-157`)
- Test: `tests/test_api.py` (append tests)

**Interfaces:**
- Consumes: `CallStore.add_call_event/get_call_events/get_sellometer/get_sellometer_history` (Task 2); `src.sellometer.load_config/known_event_ids` (Task 1).
- Produces (consumed by the dashboard in Task 5 and any future supervisor dash):
  - `POST /api/calls/{session_id}/events` body `{"event": "<id>"}` → `{"ok": true, "event": "<id>"}`; 400 on unknown event id; 503 if config unavailable
  - `GET /api/calls/{session_id}/sellometer` → the live sellometer JSON; 404 until first compute
  - `GET /api/calls/reps/{ext_id}/sellometer-history?limit=50` → `{"records": [...]}` newest first (no collision with `/{session_id}/...` routes — different segment count)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py` (uses the file's existing `client`/`store` fixtures and `auth_header()` helper):

```python
@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_post_call_event_records_and_is_idempotent(client, store):
    resp = client.post("/api/calls/s-1/events", headers=auth_header(),
                       json={"event": "sales-script-opened"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "event": "sales-script-opened"}
    first_ts = store.get_call_events("s-1")["sales-script-opened"]

    resp = client.post("/api/calls/s-1/events", headers=auth_header(),
                       json={"event": "sales-script-opened"})
    assert resp.status_code == 200
    assert store.get_call_events("s-1")["sales-script-opened"] == first_ts


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_post_call_event_unknown_id_400(client):
    resp = client.post("/api/calls/s-1/events", headers=auth_header(),
                       json={"event": "made-up-event"})
    assert resp.status_code == 400


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_sellometer_404_until_computed(client):
    resp = client.get("/api/calls/s-1/sellometer", headers=auth_header())
    assert resp.status_code == 404


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_sellometer_returns_stored_json(client, store):
    data = {"score": 25, "max": 100, "checkpoints": [],
            "startedAt": "2026-07-03T14:00:00+00:00", "timeline": [0, 25],
            "updatedAt": "2026-07-03T14:02:00+00:00"}
    store.set_sellometer("s-1", data)
    resp = client.get("/api/calls/s-1/sellometer", headers=auth_header())
    assert resp.status_code == 200
    assert resp.json() == data


@patch("src.api.auth.Config.API_KEY", API_KEY)
def test_get_sellometer_history(client, store):
    store.push_sellometer_final("576959052", {"sessionId": "s-1", "score": 40})
    store.push_sellometer_final("576959052", {"sessionId": "s-2", "score": 70})
    resp = client.get("/api/calls/reps/576959052/sellometer-history?limit=1",
                      headers=auth_header())
    assert resp.status_code == 200
    records = resp.json()["records"]
    assert len(records) == 1
    assert records[0]["sessionId"] == "s-2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -v -k "sellometer or call_event"`
Expected: FAIL (404/405 responses — routes don't exist yet)

- [ ] **Step 3: Add the request model**

Append to `src/api/models.py`:

```python
class CallEventReq(BaseModel):
    event: str  # a sellometer event id, e.g. "sales-script-opened"
```

- [ ] **Step 4: Add the endpoints**

In `src/api/routes.py`, extend the imports:

```python
from datetime import datetime, timezone

from src.api.models import CallEventReq, ContactLookupReq, ContactProps, ContactSearchReq, DealReq
from src.sellometer import known_event_ids, load_config as load_sellometer_config
```

Insert after the `get_extracted` endpoint (`src/api/routes.py:151-157`):

```python
@router.post("/{session_id}/events")
def post_call_event(session_id: str, body: CallEventReq):
    """Record a sell-o-meter checkpoint event (e.g. a dashboard button click).
    Idempotent — the first timestamp for an event wins."""
    try:
        config = load_sellometer_config()
    except Exception:
        raise HTTPException(status_code=503, detail="Sellometer config unavailable")
    if body.event not in known_event_ids(config):
        raise HTTPException(status_code=400, detail=f"Unknown event: {body.event}")
    store = get_store()
    store.add_call_event(session_id, body.event,
                         datetime.now(timezone.utc).isoformat())
    return {"ok": True, "event": body.event}


@router.get("/{session_id}/sellometer")
def get_sellometer(session_id: str):
    store = get_store()
    data = store.get_sellometer(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="No sellometer data for session")
    return data


@router.get("/reps/{ext_id}/sellometer-history")
def get_sellometer_history(ext_id: str, limit: int = 50):
    store = get_store()
    return {"records": store.get_sellometer_history(ext_id, limit=min(limit, 500))}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_api.py -v`
Expected: all PASS (new and pre-existing)

- [ ] **Step 6: Commit**

```bash
git add src/api/models.py src/api/routes.py tests/test_api.py
git commit -m "feat(sellometer): API endpoints for events, live score, rep history"
```

---

### Task 5: Dashboard meter UI + button-click event wiring

**Files:**
- Modify: `src/config.py` (add `event` keys to two `CLAUDE_TOOLS` entries, lines 65-80)
- Modify: `src/api/static/dashboard.html` (CSS, markup, JS)
- Test: manual/visual (this is a static page with no JS test harness; the API contract it consumes is covered by Task 4's tests)

**Interfaces:**
- Consumes: `GET /api/calls/{sid}/sellometer`, `POST /api/calls/{sid}/events` (Task 4); existing `currentSid`, `authHeaders()`, `escapeHtml()`, `.dot`/`.dot.green` styles in `dashboard.html`.
- Produces: nothing downstream.

**Design note:** keep the meter visually native to this dashboard — its existing dark-panel palette (`--panel/--border/--accent/--green`), 11px uppercase chips, and status-dot idiom. If the executing agent has a dataviz/design skill available it may consult it, but do not introduce a chart library or new fonts; it's a fill bar and chips.

- [ ] **Step 1: Tag the two checkpoint tools in config**

In `src/config.py`, `CLAUDE_TOOLS` (lines 65-80), add an `event` key to the first two entries (all other entries unchanged — no `event` key means no checkpoint):

```python
        {"label": "🧠 Sales Script Claude",
         "event": "sales-script-opened",
         "url": "https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061"},
        {"label": "📄 Service Agreement Generator",
         "event": "agreement-opened",
         "url": "https://claude.ai/project/019eb27a-006c-7101-8f28-2a205e8c9fee"},
```

- [ ] **Step 2: Add the meter styles**

In `dashboard.html`, append inside the `<style>` block (after the `#legend .chip` rule, line 78-80):

```css
    /* ---- sell-o-meter ---- */
    #sellometer { margin-bottom: 16px; }
    #sm-score { font-size: 24px; font-weight: 700; }
    #sm-bar { height: 10px; background: var(--bg); border: 1px solid var(--border);
              border-radius: 5px; overflow: hidden; }
    #sm-fill { height: 100%; width: 0%; background: var(--accent);
               transition: width 0.4s ease; }
    #sm-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .sm-chip { display: inline-flex; align-items: center; gap: 6px;
               font-size: 11px; padding: 2px 8px; border-radius: 3px;
               border: 1px solid var(--border); color: var(--muted);
               text-transform: uppercase; letter-spacing: 0.06em; }
    .sm-chip .dot { width: 8px; height: 8px; }
    .sm-chip.hit { color: var(--text); border-color: var(--green); }
```

- [ ] **Step 3: Add the meter markup**

In `dashboard.html`, at the top of the right-hand section — immediately after `<section id="fields">` (line 101), before `<h2>Caller Info</h2>`:

```html
      <div id="sellometer" hidden>
        <h2>Sell-o-meter <span id="sm-score" style="float:right">0 / 100</span></h2>
        <div id="sm-bar"><div id="sm-fill"></div></div>
        <div id="sm-chips"></div>
      </div>
```

- [ ] **Step 4: Add polling + rendering JS**

In the `<script>`, after the `pollExtracted` function (ends line 306), add:

```js
    async function pollSellometer() {
      if (!currentSid) return;
      try {
        const r = await fetch(`/api/calls/${encodeURIComponent(currentSid)}/sellometer`, {
          headers: authHeaders(),
        });
        if (!r.ok) return;  // 404 until the worker's first compute
        renderSellometer(await r.json());
      } catch (_) { /* silent — just skip this tick */ }
    }

    function renderSellometer(sm) {
      document.getElementById("sellometer").hidden = false;
      document.getElementById("sm-score").textContent = `${sm.score} / ${sm.max}`;
      document.getElementById("sm-fill").style.width =
        sm.max ? `${Math.round(100 * sm.score / sm.max)}%` : "0%";
      const chips = document.getElementById("sm-chips");
      chips.innerHTML = "";
      for (const cp of sm.checkpoints || []) {
        const chip = document.createElement("span");
        chip.className = "sm-chip" + (cp.hit ? " hit" : "");
        const dot = document.createElement("span");
        dot.className = "dot" + (cp.hit ? " green" : "");
        chip.appendChild(dot);
        chip.appendChild(document.createTextNode(`${cp.label} +${cp.points}`));
        chips.appendChild(chip);
      }
    }
```

and register the poll next to the existing intervals (after line 309 `setInterval(pollExtracted, 3000);`):

```js
    setInterval(pollSellometer, 3000);
```

- [ ] **Step 5: Fire checkpoint events from the Claude tool buttons**

Change `openInClaude` (line 395) to take the whole tool object and report the event, and update the button wiring in `loadClaudeTools` (line 426). Replace the current `openInClaude(url)` function with:

```js
    async function openInClaude(tool) {
      const txt = document.getElementById("transcript-body").textContent.trim();
      if (!txt || txt === "Waiting for transcript…") {
        return toast("No transcript yet", true);
      }
      if (!tool.url) {
        return toast("Claude URL not loaded yet — try again in a moment", true);
      }
      // Sell-o-meter checkpoint: fire-and-forget; never block opening the tool.
      if (tool.event && currentSid) {
        fetch(`/api/calls/${encodeURIComponent(currentSid)}/events`, {
          method: "POST", headers: authHeaders(),
          body: JSON.stringify({ event: tool.event }),
        }).catch(() => {});
      }
      try {
        await navigator.clipboard.writeText(txt);
        window.open(tool.url, "_blank");
        toast("Transcript copied — paste into Claude (Ctrl/Cmd+V)");
      } catch (e) {
        // Clipboard blocked — still open Claude so the rep can copy manually.
        window.open(tool.url, "_blank");
        toast("Opened Claude — copy the transcript manually (clipboard blocked)", true);
      }
    }
```

and in `loadClaudeTools`, change the click wiring from `b.addEventListener("click", () => openInClaude(t.url));` to:

```js
        b.addEventListener("click", () => openInClaude(t));
```

- [ ] **Step 6: Smoke-test the page locally**

Run the API locally (see `run_local.py`) and load `/dashboard?rep=<any monitored ext>`:
- With no sellometer data: the meter block stays hidden, no console errors.
- Seed fake data to see it render (from a Python shell against local/fake Redis, or temporarily via the worker): expect the bar filled proportionally, hit chips green-bordered.
- Config check: `python -c "from src.config import Config; print([t.get('event') for t in Config.CLAUDE_TOOLS])"` → `['sales-script-opened', 'agreement-opened', None, None, None, None, None]`

- [ ] **Step 7: Run the full test suite (guards against config-shape regressions)**

Run: `python -m pytest -v`
Expected: all PASS (`tests/test_config.py` exists — if it asserts `CLAUDE_TOOLS` shape, update it to tolerate/assert the new `event` keys)

- [ ] **Step 8: Commit**

```bash
git add src/config.py src/api/static/dashboard.html
git commit -m "feat(dashboard): sell-o-meter gauge with checkpoint chips and click events"
```

---

### Task 6: Full verification + deploy checklist

**Files:**
- No new files. Verification only, then deployment per `docs/DEPLOYMENT.md`.

**Interfaces:** none.

- [ ] **Step 1: Full suite**

Run: `python -m pytest -v`
Expected: all PASS, no warnings about missing `sellometer.json` (it ships inside `src/`, so both the Vercel bundle and the Fly image pick it up — verify `Dockerfile.engine` copies the whole `src/` tree; it does today).

- [ ] **Step 2: Deploy**

Both deployables changed — the engine (new worker) and the web app (routes + dashboard). Follow `docs/DEPLOYMENT.md`:
- Fly engine: `fly deploy -c fly.engine.toml`
- Vercel web app: deploy via the repo's normal Vercel flow (`vercel --prod` or git push, per DEPLOYMENT.md)

- [ ] **Step 3: Live verification (needs a real monitored call)**

During a test call on a monitored extension:
1. `GET /api/calls/{sid}/sellometer` returns a growing `timeline` and the meter renders on `/dashboard?rep=...`.
2. Say a name/phone/email → corresponding chips go green within ~6s.
3. Click "🧠 Sales Script Claude" → +25 within ~3s; click again → no change.
4. After hangup + ~60s grace: `GET /api/calls/reps/{extId}/sellometer-history` shows the final record with `endedAt` and the closing timeline entry.

- [ ] **Step 4: Commit any verification fixes, report results**

---

## Out of scope (per spec)

Power-phrase detect type, supervisor dashboard rendering, runtime-editable config, HubSpot score push. The seams for each already exist (`_DETECTORS` registry, `GET .../sellometer`, `src/sellometer.json`).
