import os
from dotenv import load_dotenv

load_dotenv()


def _redis_url() -> str:
    # Vercel KV injects KV_REST_API_URL; direct Upstash setups use UPSTASH_REDIS_URL
    return os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_URL", "")


def _redis_token() -> str:
    return os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_TOKEN", "")


class Config:
    # RingCentral
    RC_CLIENT_ID: str = os.environ.get("RC_CLIENT_ID", "")
    RC_CLIENT_SECRET: str = os.environ.get("RC_CLIENT_SECRET", "")
    RC_JWT: str = os.environ.get("RC_JWT", "")
    RC_SERVER: str = os.environ.get("RC_SERVER", "https://platform.ringcentral.com")

    # Redis (Vercel KV or direct Upstash)
    REDIS_URL: str = _redis_url()
    REDIS_TOKEN: str = _redis_token()

    # Deepgram
    DEEPGRAM_API_KEY: str = os.environ.get("DEEPGRAM_API_KEY", "")

    # API
    API_KEY: str = os.environ.get("CALL_BRIDGE_API_KEY", "")

    # TTLs (seconds)
    CALL_TTL: int = 3600  # 1 hour after call ends

    # Softphone sidecar (Phase 2)
    SOFTPHONE_BRIDGE_URL: str = os.environ.get("SOFTPHONE_BRIDGE_URL", "")
    SOFTPHONE_BRIDGE_API_KEY: str = os.environ.get("SOFTPHONE_BRIDGE_API_KEY", "")

    # HubSpot (Phase 3)
    HUBSPOT_PRIVATE_APP_TOKEN: str = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "")

    # Anthropic (Service Agreement Generator)
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    # `or` (not a default arg) so a present-but-empty env var still falls back.
    ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-6"

    # CallRail
    CALLRAIL_API_KEY: str = os.environ.get("CALLRAIL_API_KEY", "")
    CALLRAIL_ACCOUNT_ID: str = os.environ.get("CALLRAIL_ACCOUNT_ID", "")

    # HubSpot extras
    HUBSPOT_PORTAL_ID: str = os.environ.get("HUBSPOT_PORTAL_ID", "")
    HUBSPOT_SCOPE_PROPERTY: str = os.environ.get("HUBSPOT_SCOPE_PROPERTY", "scope_of_work")

    # Extension IDs to live-transcribe. Empty = disabled.
    MONITORED_EXTENSIONS: list[str] = [
        e.strip() for e in os.environ.get("MONITORED_EXTENSIONS", "").split(",") if e.strip()
    ]
