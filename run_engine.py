"""Production engine entrypoint — monitor + extraction + metrics workers.

No HTTP API (that runs on Vercel). Requires real Upstash (KV_REST_API_*).
"""
import asyncio
import logging

from upstash_redis import Redis

from src.call_monitor import run_monitor
from src.config import Config
from src.extraction_worker import run_extraction_worker
from src.metrics_worker import run_metrics_worker
from src.redis_store import CallStore
from src.sellometer_worker import run_sellometer_worker
from src.sidecar_client import SidecarClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def build_store() -> CallStore:
    if not (Config.REDIS_URL and Config.REDIS_TOKEN):
        raise RuntimeError("Engine requires Upstash Redis (KV_REST_API_URL/TOKEN)")
    return CallStore(Redis(url=Config.REDIS_URL, token=Config.REDIS_TOKEN))


def build_sidecar() -> SidecarClient | None:
    if Config.SOFTPHONE_BRIDGE_URL and Config.SOFTPHONE_BRIDGE_API_KEY:
        return SidecarClient(Config.SOFTPHONE_BRIDGE_URL,
                             Config.SOFTPHONE_BRIDGE_API_KEY)
    return None


async def main():
    store = build_store()
    sidecar = build_sidecar()
    logger.info("Live transcription %s",
                "enabled via " + Config.SOFTPHONE_BRIDGE_URL if sidecar
                else "disabled (no sidecar configured)")
    logger.info("Engine starting: monitor + extraction + metrics")
    await asyncio.gather(
        run_monitor(store, sidecar=sidecar),
        run_extraction_worker(store),
        run_sellometer_worker(store),
        run_metrics_worker(store),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEngine stopped.")
