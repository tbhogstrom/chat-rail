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
        store.touch_call_events(sid)
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
        if not store.try_mark_sellometer_finalized(sid):
            continue  # already finalized (e.g. re-graced by a restart's hydration)
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
