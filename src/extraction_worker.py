"""Background task that re-runs extractors against every active session's
transcript and writes the result to Redis.
"""
import asyncio
import logging

from src.extractor import EXTRACTORS, find_highlights
from src.redis_store import CallStore

logger = logging.getLogger(__name__)


def run_extraction_cycle(store: CallStore) -> None:
    """One pass over all active sessions. Exposed for unit testing."""
    for sid in store.list_active_sessions():
        transcript = store.get_transcript(sid) or ""
        call = store.get_call(sid) or {}
        rep_first_name = call.get("rep_first_name")
        extracted = {field: fn(transcript) for field, fn in EXTRACTORS.items()}
        highlights = find_highlights(transcript, rep_first_name=rep_first_name)
        extracted["highlights"] = highlights
        # Prefer the most recent caller-name over whatever extract_firstname
        # returned: a rep's self-intro like "This is Doug" often wins the raw
        # regex race even though the caller's name is what we actually want.
        caller_names = [h["text"] for h in highlights if h["ruleId"] == "caller-name"]
        if caller_names:
            extracted["firstname"] = caller_names[-1]
        store.set_extracted(sid, extracted)


async def run_extraction_worker(store: CallStore, interval: float = 3.0) -> None:
    """Forever loop: run one cycle every `interval` seconds. Swallows per-cycle
    errors so a single bad transcript doesn't take down the worker.
    """
    logger.info("Extraction worker starting (interval=%ss)", interval)
    while True:
        try:
            run_extraction_cycle(store)
        except Exception:
            logger.exception("Extraction worker cycle failed")
        await asyncio.sleep(interval)
