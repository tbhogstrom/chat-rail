"""Background worker: refresh per-rep call metrics + recent-calls feed from RC."""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ringcentral import SDK

from src.config import Config
from src.rc_metrics import compute_metrics_and_recent
from src.redis_store import CallStore

logger = logging.getLogger(__name__)


def _resolve_tz(name: str):
    """ZoneInfo for `name`, falling back to UTC on a bad/missing tz.

    A metrics misconfiguration must never take the whole process down (this
    worker shares an event loop with the API and call monitor).
    """
    try:
        return ZoneInfo(name)
    except Exception:
        logger.exception("Invalid METRICS_TIMEZONE %r; falling back to UTC", name)
        return timezone.utc


def refresh_metrics_once(store: CallStore, platform, tz, monitored,
                         now: datetime | None = None, limit: int = 15):
    """One refresh: fetch call-logs, compute counts + recent feed, persist.

    Raises before writing anything if the fetch/compute fails, so a failed
    cycle leaves the last-good metrics/recent untouched in Redis (the store
    setters are only reached after a successful compute). Returns
    `(num_reps, num_recent)`.
    """
    now = now or datetime.now(timezone.utc)
    metrics, recent = compute_metrics_and_recent(
        platform, monitored, store.get_rep_roster(), now, tz, limit=limit)
    store.set_rep_metrics(metrics)
    store.set_recent_calls(recent)
    return len(metrics), len(recent)


async def run_metrics_worker(store: CallStore, interval: int = 60) -> None:
    """Every `interval`s: fetch each monitored rep's call-log, derive counts +
    recent feed, write to Redis. Retains last-good values on any failure.
    """
    tz = _resolve_tz(Config.METRICS_TIMEZONE)
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    logger.info("Metrics worker starting (interval=%ss, tz=%s)",
                interval, Config.METRICS_TIMEZONE)
    while True:
        try:
            if not platform.logged_in():
                platform.login(jwt=Config.RC_JWT)
            n_metrics, n_recent = refresh_metrics_once(
                store, platform, tz, Config.MONITORED_EXTENSIONS, limit=15)
            logger.info("Metrics refreshed for %d rep(s), %d recent call(s)",
                        n_metrics, n_recent)
        except Exception:
            # Retain last-good metrics/recent; re-login defensively for next cycle.
            logger.exception("Metrics worker cycle failed")
            try:
                platform.login(jwt=Config.RC_JWT)
            except Exception:
                logger.exception("Metrics worker re-login failed")
        await asyncio.sleep(interval)
