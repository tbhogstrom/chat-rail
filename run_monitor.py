"""Entrypoint for the persistent call monitor process."""
import asyncio
import logging

from upstash_redis import Redis

from src.call_monitor import run_monitor
from src.config import Config
from src.redis_store import CallStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


async def main():
    redis = Redis(url=Config.UPSTASH_REDIS_URL, token=Config.UPSTASH_REDIS_TOKEN)
    store = CallStore(redis)
    await run_monitor(store)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
