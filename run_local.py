"""Local dev entrypoint — runs API + monitor in one process, backed by fakeredis.

No Upstash or Vercel required. Lets you smoke-test end-to-end before deploying.

Usage: python run_local.py
"""
import asyncio
import logging

import fakeredis
import uvicorn

from src.api.main import create_app
from src.call_monitor import run_monitor
from src.redis_store import CallStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    store = CallStore(fakeredis.FakeRedis(decode_responses=True))
    app = create_app(store)

    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    logger.info("Local server starting on http://localhost:8000")
    logger.info("Monitor listening for RC telephony events")
    await asyncio.gather(server.serve(), run_monitor(store))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
