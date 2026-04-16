import asyncio
import json
import logging

from ringcentral import SDK
from ringcentral.websocket.web_socket_client import WebSocketEvents

from src.config import Config
from src.redis_store import CallStore

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {"Proceeding", "Answered", "Hold"}
END_STATUSES = {"Disconnected", "Gone", "VoiceMail"}


def process_telephony_event(event: dict, store: CallStore) -> None:
    """Process a single RC telephony session notification and update store."""
    body = event.get("body", {})
    session_id = body.get("telephonySessionId")
    if not session_id:
        logger.warning("Event missing telephonySessionId: %s", event)
        return

    parties = body.get("parties", [])
    if not parties:
        return

    party = parties[0]
    status = party.get("status", {}).get("code", "Unknown")
    direction = party.get("direction", "Unknown")
    from_info = party.get("from", {})
    to_info = party.get("to", {})

    call_data = {
        "sessionId": session_id,
        "status": status,
        "direction": direction,
        "from": from_info,
        "to": to_info,
    }

    if status in END_STATUSES:
        logger.info("Call ended: %s (status=%s)", session_id, status)
        store.complete_call(session_id)
    else:
        logger.info("Call event: %s (status=%s)", session_id, status)
        store.store_call(session_id, call_data)


async def run_monitor(store: CallStore) -> None:
    """Connect to RC WebSocket and process telephony events indefinitely."""
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    platform.login(jwt=Config.RC_JWT)
    logger.info("Authenticated with RingCentral")

    ext = platform.get("/restapi/v1.0/account/~/extension/~").json_dict()
    logger.info("Monitoring as: %s (ext %s)", ext["name"], ext["extensionNumber"])

    event_filters = ["/restapi/v1.0/account/~/telephony/sessions"]

    ws_client = sdk.create_web_socket_client()
    token_data = ws_client.get_web_socket_token()

    def on_notification(msg):
        if isinstance(msg, dict):
            data = msg
        elif hasattr(msg, "json_dict"):
            data = msg.json_dict()
        else:
            try:
                data = json.loads(str(msg))
            except (json.JSONDecodeError, TypeError):
                logger.warning("Could not parse notification: %s", msg)
                return
        logger.debug("Raw event: %s", json.dumps(data, default=str)[:500])
        process_telephony_event(data, store)

    async def on_connected(client):
        logger.info("WebSocket connected, creating subscription...")
        await client.create_subscription(event_filters)
        logger.info("Subscription active — listening for call events")

    ws_client.on(WebSocketEvents.connectionCreated,
                 lambda c: asyncio.create_task(on_connected(c)))
    ws_client.on("notification", on_notification)

    logger.info("Opening WebSocket connection...")
    await ws_client.open_connection(token_data["uri"], token_data["ws_access_token"])
