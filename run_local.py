"""Local dev entrypoint — runs API + monitor in one process.

Uses real Upstash when KV_REST_API_URL/TOKEN are set; otherwise falls back to
fakeredis. Real Upstash is required when the softphone sidecar is also running,
since the sidecar writes transcripts to Upstash and the API reads from the same
store.

Usage: python run_local.py
"""
import asyncio
import logging

import fakeredis
import uvicorn
from upstash_redis import Redis

from src.api.main import create_app
from src.call_monitor import run_monitor
from src.config import Config
from src.extraction_worker import run_extraction_worker
from src.redis_store import CallStore
from src.sidecar_client import SidecarClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    if Config.REDIS_URL and Config.REDIS_TOKEN:
        store = CallStore(Redis(url=Config.REDIS_URL, token=Config.REDIS_TOKEN))
        logger.info("Using Upstash Redis at %s", Config.REDIS_URL)
    else:
        store = CallStore(fakeredis.FakeRedis(decode_responses=True))
        logger.info("Using fakeredis (in-process). Sidecar transcripts won't be visible here.")

    app = create_app(store)

    sidecar = None
    if Config.SOFTPHONE_BRIDGE_URL and Config.SOFTPHONE_BRIDGE_API_KEY:
        sidecar = SidecarClient(Config.SOFTPHONE_BRIDGE_URL, Config.SOFTPHONE_BRIDGE_API_KEY)
        logger.info("Live transcription enabled via %s", Config.SOFTPHONE_BRIDGE_URL)
    else:
        logger.info("Live transcription disabled (no sidecar configured)")

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    logger.info("Local server starting on http://localhost:8000")
    logger.info("Monitor listening for RC telephony events")
    await asyncio.gather(
        server.serve(),
        run_monitor(store, sidecar=sidecar),
        run_extraction_worker(store),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
