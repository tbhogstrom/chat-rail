import json
import time
from datetime import datetime, timezone

# Seconds a disconnected call stays eligible for extraction after it ends.
# Short calls (voicemails) flush their final Deepgram transcript to Redis at or
# just after the Disconnected event — the worker needs one more cycle or two to
# see the complete transcript before the session is forgotten.
FINAL_EXTRACTION_GRACE = 60


class CallStore:
    """Manages call state in Redis. Works with both upstash-redis and fakeredis."""

    def __init__(self, redis_client):
        self.redis = redis_client

    def store_call(self, session_id: str, call_data: dict) -> None:
        """Store or update call state and add to active set."""
        self.redis.set(f"call:{session_id}:state", json.dumps(call_data))
        self.redis.sadd("calls:active", session_id)

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

    def is_active(self, session_id: str) -> bool:
        """True while the session is in the active set."""
        return bool(self.redis.sismember("calls:active", session_id))

    def active_session_ids(self) -> list[str]:
        """Session IDs currently in the active set (no grace-window entries).

        Unlike list_active_calls, includes sessions whose state key has
        expired — the reconcile sweep must be able to complete those too.
        """
        return list(self.redis.smembers("calls:active"))

    def list_active_calls(self) -> list[dict]:
        """Return state for all active calls."""
        session_ids = self.redis.smembers("calls:active")
        calls = []
        for sid in session_ids:
            call = self.get_call(sid)
            if call:
                calls.append(call)
        return calls

    def complete_call(self, session_id: str, ttl: int = 3600,
                       grace: int = FINAL_EXTRACTION_GRACE) -> None:
        """Mark a call as completed: remove from active set, set TTL on state.

        Also starts a post-disconnect grace window during which the extraction
        worker continues to process this session. This catches late transcript
        flushes on short calls where Deepgram's final message lands at or just
        after the Disconnected event.
        """
        call = self.get_call(session_id)
        if call:
            call["status"] = "Disconnected"
            self.redis.set(f"call:{session_id}:state", json.dumps(call))
            self.redis.expire(f"call:{session_id}:state", ttl)
            self.redis.expire(f"call:{session_id}:transcript", ttl)
        self.redis.srem("calls:active", session_id)
        # Mark for continued extraction during grace window.
        self.redis.sadd("calls:recently-ended", session_id)
        self.redis.set(f"call:{session_id}:extract-grace", "1", ex=grace)

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

    def list_active_sessions(self) -> list[str]:
        """Session IDs the extraction worker should process this cycle.

        Includes currently-active calls plus recently-ended calls whose
        post-disconnect grace marker hasn't expired. Stale recently-ended
        entries (marker gone) are cleaned up lazily on the way past.
        """
        active = set(self.redis.smembers("calls:active"))
        recent = list(self.redis.smembers("calls:recently-ended"))
        stale = []
        for sid in recent:
            if sid in active:
                continue
            if self.redis.get(f"call:{sid}:extract-grace") is not None:
                active.add(sid)
            else:
                stale.append(sid)
        for sid in stale:
            self.redis.srem("calls:recently-ended", sid)
        return list(active)

    def set_extracted(self, session_id: str, data: dict, ttl: int = 3600) -> None:
        """Persist extractor output for a session as JSON."""
        self.redis.set(f"call:{session_id}:extracted", json.dumps(data))
        self.redis.expire(f"call:{session_id}:extracted", ttl)

    def get_extracted(self, session_id: str) -> dict | None:
        """Return the stored extractor output, or None if absent."""
        raw = self.redis.get(f"call:{session_id}:extracted")
        if raw is None:
            return None
        return json.loads(raw)

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

    def set_rep_pointer(self, extension_id: str, session_id: str) -> None:
        """Point rep:{extension_id}:current at this session.

        Separate from store_call because queue-routed calls need to point
        every party's extensionId at the same session, not just the primary.
        """
        self.redis.set(f"rep:{extension_id}:current", session_id)

    def clear_rep_pointer(self, extension_id: str) -> None:
        """Remove the rep's current-call pointer, marking them as not in a call."""
        self.redis.delete(f"rep:{extension_id}:current")

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

    def set_rep_metrics(self, metrics: dict) -> None:
        """Persist per-rep call counts ({extId: {inbound/outbound Today/Week}})."""
        self.redis.set("reps:metrics", json.dumps(metrics))

    def get_rep_metrics(self) -> dict:
        """Return per-rep call counts, or {} if not yet computed."""
        raw = self.redis.get("reps:metrics")
        return json.loads(raw) if raw is not None else {}

    def set_recent_calls(self, calls: list) -> None:
        """Persist the recent-calls feed (list of row dicts, newest first)."""
        self.redis.set("overview:recent", json.dumps(calls))

    def get_recent_calls(self) -> list:
        """Return the recent-calls feed, or [] if not yet computed."""
        raw = self.redis.get("overview:recent")
        return json.loads(raw) if raw is not None else []

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

    def touch_call_events(self, session_id: str, ttl: int = 3600) -> None:
        """Refresh the events key TTL. Call each cycle for live sessions so a
        checkpoint clicked early in a long call doesn't expire mid-call."""
        self.redis.expire(f"call:{session_id}:events", ttl)

    def set_sellometer(self, session_id: str, data: dict, ttl: int = 3600) -> None:
        """Persist the live sell-o-meter JSON for a session."""
        self.redis.set(f"call:{session_id}:sellometer", json.dumps(data))
        self.redis.expire(f"call:{session_id}:sellometer", ttl)

    def get_sellometer(self, session_id: str) -> dict | None:
        """Return the live sell-o-meter JSON, or None if not computed yet."""
        raw = self.redis.get(f"call:{session_id}:sellometer")
        return json.loads(raw) if raw is not None else None

    def try_mark_sellometer_finalized(self, session_id: str,
                                      ttl: int = 86400) -> bool:
        """Atomically claim finalization for a session. True on first claim.

        An engine restart re-processes a just-ended call's END event, which
        re-opens its grace window and re-tracks the session in a fresh
        worker — without this guard that pushes a duplicate history record
        (LPUSH is not idempotent). SET NX makes exactly one claim win.
        """
        return bool(self.redis.set(f"call:{session_id}:sellometer-finalized",
                                   "1", nx=True, ex=ttl))

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
