"""Vercel serverless entrypoint — exposes the FastAPI app as a handler."""
from upstash_redis import Redis

from src.api.main import create_app
from src.config import Config
from src.redis_store import CallStore

redis = Redis(url=Config.REDIS_URL, token=Config.REDIS_TOKEN)
store = CallStore(redis)
app = create_app(store)
