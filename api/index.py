"""Vercel serverless entrypoint — exposes the FastAPI app as a handler."""
import os
import sys

# Ensure the project root is on sys.path so "from src.xxx import ..." works
# when Vercel runs this file from inside /var/task/api/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from upstash_redis import Redis

from src.api.main import create_app
from src.config import Config
from src.redis_store import CallStore

redis = Redis(url=Config.REDIS_URL, token=Config.REDIS_TOKEN)
store = CallStore(redis)
app = create_app(store)
