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

    # Timezone for metrics day/week boundaries (IANA name). Week starts Monday.
    METRICS_TIMEZONE: str = os.environ.get("METRICS_TIMEZONE") or "America/Los_Angeles"

    # Claude project buttons on the dashboard. Add a tool = add one entry here.
    CLAUDE_TOOLS: list[dict] = [
        {"label": "🧠 Sales Script Claude",
         "url": "https://claude.ai/project/019eaedf-52bd-775e-a012-0fb929726061"},
        {"label": "📄 Service Agreement Generator",
         "url": "https://claude.ai/project/019eb27a-006c-7101-8f28-2a205e8c9fee"},
        {"label": "🔄 Cancellation Recovery",
         "url": "https://claude.ai/project/019f1ff1-02b7-72bd-a18b-97d6ab294876"},
        {"label": "🎯 SFW Post-Call Sales Coach",
         "url": "https://claude.ai/project/019eb25f-f9c2-7534-a4fe-35e35db785dd"},
        {"label": "🎓 SFW Sales Training Lab",
         "url": "https://claude.ai/project/019eb3ee-249a-74ef-9a81-b23ef6bb1636"},
        {"label": "📐 SFW Inspection Estimate Builder",
         "url": "https://claude.ai/project/019ec255-52ba-7767-aee4-c78e1b135025"},
        {"label": "🗓️ SFW Painting Schedule Agent",
         "url": "https://claude.ai/project/019edbfa-f73c-7498-a433-76f619e2c47f"},
    ]

    # Shared password gating the web app. Empty = gate disabled.
    APP_PASSWORD: str = os.environ.get("APP_PASSWORD") or ""
