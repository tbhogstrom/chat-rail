import json

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
