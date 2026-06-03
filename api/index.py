"""Vercel serverless entrypoint — exposes the FastAPI app as a handler.

Redis is optional: the agreement tool needs no store. When KV/Upstash env
vars are present, the call-intelligence endpoints get a live store too.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.main import create_app
from src.config import Config

store = None
if Config.REDIS_URL and Config.REDIS_TOKEN:
    from upstash_redis import Redis
    from src.redis_store import CallStore
    store = CallStore(Redis(url=Config.REDIS_URL, token=Config.REDIS_TOKEN))

app = create_app(store)
