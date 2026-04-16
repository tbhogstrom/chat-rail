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
    """Connect to RC WebSocket and process telephony events, reconnecting on disconnect."""
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    platform.login(jwt=Config.RC_JWT)
    logger.info("Authenticated with RingCentral")

    ext = platform.get("/restapi/v1.0/account/~/extension/~").json_dict()
    logger.info("Monitoring as: %s (ext %s)", ext["name"], ext["extensionNumber"])

    event_filters = ["/restapi/v1.0/account/~/telephony/sessions"]

    backoff = 1
    while True:
        try:
            await _run_ws_session(sdk, event_filters, store)
            backoff = 1  # reset on clean exit
        except Exception as e:
            logger.warning("WebSocket session ended: %s. Reconnecting in %ds...", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _run_ws_session(sdk, event_filters, store: CallStore) -> None:
    """Run a single WebSocket session until it disconnects or errors."""
    ws_client = sdk.create_web_socket_client()
    token_data = ws_client.get_web_socket_token()

    def on_message(msg):
        # RC WS sends every frame as a JSON string containing [meta, payload]
        # or a single JSON object. Notification frames have meta.type == "ServerNotification".
        try:
            if isinstance(msg, (bytes, bytearray)):
                msg = msg.decode("utf-8")
            if isinstance(msg, str):
                parsed = json.loads(msg)
            elif isinstance(msg, dict):
                parsed = msg
            else:
                logger.debug("Ignoring non-parseable frame: %s", type(msg))
                return
        except (json.JSONDecodeError, TypeError):
            logger.warning("Could not parse WS frame: %s", str(msg)[:200])
            return

        # RC frames arrive as [metadata, body] arrays
        if isinstance(parsed, list) and len(parsed) >= 2:
            meta, body = parsed[0], parsed[1]
            msg_type = meta.get("type") if isinstance(meta, dict) else None
            if msg_type != "ServerNotification":
                logger.debug("WS non-notification frame: type=%s", msg_type)
                return
            event = {"body": body.get("body", body), "event": body.get("event")}
        elif isinstance(parsed, dict):
            event = parsed
        else:
            return

        logger.info("RC notification: %s", json.dumps(event, default=str)[:300])
        process_telephony_event(event, store)

    async def on_connected(client):
        logger.info("WebSocket connected, creating subscription...")
        await client.create_subscription(event_filters)
        logger.info("Subscription active — listening for call events")

    ws_client.on(WebSocketEvents.connectionCreated,
                 lambda c: asyncio.create_task(on_connected(c)))
    ws_client.on(WebSocketEvents.receiveMessage, on_message)

    logger.info("Opening WebSocket connection...")
    try:
        await ws_client.open_connection(token_data["uri"], token_data["ws_access_token"])
    except Exception:
        logger.exception("WebSocket session crashed")
        raise
