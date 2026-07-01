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


async def run_metrics_worker(store: CallStore, interval: int = 60) -> None:
    """Every `interval`s: fetch each monitored rep's call-log, derive counts +
    recent feed, write to Redis. Retains last-good values on any failure.
    """
    tz = ZoneInfo(Config.METRICS_TIMEZONE)
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    logger.info("Metrics worker starting (interval=%ss, tz=%s)",
                interval, Config.METRICS_TIMEZONE)
    while True:
        try:
            if not platform.logged_in():
                platform.login(jwt=Config.RC_JWT)
            now = datetime.now(timezone.utc)
            metrics, recent = compute_metrics_and_recent(
                platform, Config.MONITORED_EXTENSIONS, store.get_rep_roster(),
                now, tz, limit=15)
            store.set_rep_metrics(metrics)
            store.set_recent_calls(recent)
            logger.info("Metrics refreshed for %d rep(s), %d recent call(s)",
                        len(metrics), len(recent))
        except Exception:
            # Retain last-good metrics/recent; re-login defensively for next cycle.
            logger.exception("Metrics worker cycle failed")
            try:
                platform.login(jwt=Config.RC_JWT)
            except Exception:
                logger.exception("Metrics worker re-login failed")
        await asyncio.sleep(interval)
