import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # RingCentral
    RC_CLIENT_ID: str = os.environ["RC_CLIENT_ID"]
    RC_CLIENT_SECRET: str = os.environ["RC_CLIENT_SECRET"]
    RC_JWT: str = os.environ["RC_JWT"]
    RC_SERVER: str = os.environ.get("RC_SERVER", "https://platform.ringcentral.com")

    # Upstash Redis
    UPSTASH_REDIS_URL: str = os.environ["UPSTASH_REDIS_URL"]
    UPSTASH_REDIS_TOKEN: str = os.environ["UPSTASH_REDIS_TOKEN"]

    # Deepgram
    DEEPGRAM_API_KEY: str = os.environ.get("DEEPGRAM_API_KEY", "")

    # API
    API_KEY: str = os.environ["CALL_BRIDGE_API_KEY"]

    # TTLs (seconds)
    CALL_TTL: int = 3600  # 1 hour after call ends
