import json


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
