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
                     score: int, max_minutes: int = 480) -> list[int]:
    """Append `score` once per completed call minute not yet recorded.

    timeline[i] is the score at the end of call minute i. Gaps (a stalled
    worker) are back-filled with the score known at catch-up time. Growth is
    capped at `max_minutes` (default 8h) so a zombie session whose end event
    was lost cannot grow the stored JSON unboundedly. Mutates and returns
    `timeline`.
    """
    elapsed = min(int((now - started_at).total_seconds() // 60), max_minutes)
    while len(timeline) < elapsed:
        timeline.append(score)
    return timeline
