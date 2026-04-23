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


def process_telephony_event(event: dict, store: CallStore,
                            sidecar=None, monitored_extensions=None,
                            ext_number_map=None,
                            ext_display_map=None) -> None:
    """Process a single RC telephony session notification and update store.

    Supervision trigger scans ALL parties (not just parties[0]) because
    queue/IVR-routed calls put the actual rep beyond index 0.
    """
    body = event.get("body", {})
    session_id = body.get("telephonySessionId")
    if not session_id:
        logger.warning("Event missing telephonySessionId: %s", event)
        return

    parties = body.get("parties", [])
    if not parties:
        return

    # Primary party drives state storage (back-compat with Phase 1 behavior).
    party = parties[0]
    status = party.get("status", {}).get("code", "Unknown")
    direction = party.get("direction", "Unknown")
    from_info = party.get("from", {})
    to_info = party.get("to", {})

    rep_first_name = _resolve_rep_first_name(parties, monitored_extensions, ext_display_map)

    call_data = {
        "sessionId": session_id,
        "status": status,
        "direction": direction,
        "from": from_info,
        "to": to_info,
        "rep_first_name": rep_first_name,
    }

    if status in END_STATUSES:
        logger.info("Call ended: %s (status=%s)", session_id, status)
        store.complete_call(session_id)
    else:
        # One call fires many mid-state events (Proceeding -> Setup -> Answered
        # -> Hold -> ...); at INFO they drown out the interesting lines.
        logger.debug("Call event: %s (status=%s)", session_id, status)
        store.store_call(session_id, call_data)

    # Supervision trigger — scan all parties for a monitored rep.
    if not (sidecar and monitored_extensions):
        return
    scanned = []
    for p in parties:
        p_status = p.get("status", {}).get("code", "")
        p_ext_id = (p.get("extensionId")
                    or p.get("to", {}).get("extensionId")
                    or p.get("from", {}).get("extensionId"))
        scanned.append((p_ext_id, p_status))
    logger.debug("Supervision scan %s: parties=%s monitored=%s",
                 session_id, scanned, monitored_extensions)
    for p in parties:
        p_status = p.get("status", {}).get("code", "")
        p_ext_id = (p.get("extensionId")
                    or p.get("to", {}).get("extensionId")
                    or p.get("from", {}).get("extensionId"))
        if not p_ext_id or p_ext_id not in monitored_extensions:
            continue
        if p_status in END_STATUSES:
            logger.info("Supervision STOP %s (rep %s)", session_id, p_ext_id)
            asyncio.create_task(sidecar.stop_supervision(session_id))
            return
        if p_status == "Answered":
            p_ext_number = (p.get("to", {}).get("extensionNumber")
                            or p.get("from", {}).get("extensionNumber"))
            if not p_ext_number and ext_number_map:
                p_ext_number = ext_number_map.get(p_ext_id)
            if p_ext_number:
                logger.info("Supervision START %s (rep %s, ext number %s)",
                            session_id, p_ext_id, p_ext_number)
                asyncio.create_task(sidecar.start_supervision(session_id, p_ext_number))
                return
            else:
                logger.warning("Supervision match %s but no ext_number for rep %s",
                               session_id, p_ext_id)


def _is_monitored(ext_id: str, monitored: list[str] | None) -> bool:
    if not monitored:
        return False  # Phase 2: empty = disabled (conservative)
    return ext_id in monitored


def _fetch_active_session_events(platform) -> list[dict]:
    """Return WS-shaped events for every telephony session currently in progress.

    RC's WebSocket only emits state-transition events, so calls that are already
    live when we connect (or that transition during a WS reconnect gap) are
    invisible. We hydrate by listing active calls, then fetching each session's
    full detail — which comes back in the same shape the WS emits — and wrapping
    it as `{"body": ...}` so callers can reuse `process_telephony_event`.
    """
    try:
        records = platform.get(
            "/restapi/v1.0/account/~/active-calls",
            {"perPage": 100, "view": "Detailed"},
        ).json_dict().get("records", [])
    except Exception as e:
        logger.warning("Active-calls snapshot failed: %s", e)
        return []

    events = []
    for rec in records:
        sid = rec.get("telephonySessionId")
        if not sid:
            continue
        try:
            session = platform.get(
                f"/restapi/v1.0/account/~/telephony/sessions/{sid}"
            ).json_dict()
        except Exception as e:
            logger.warning("Session fetch failed for %s: %s", sid, e)
            continue
        events.append({"body": session})
    return events


def _load_ext_number_map(platform) -> dict[str, str]:
    """Return {extensionId: extensionNumber} for every extension on the account.

    Party records in telephony events often omit extensionNumber, so we resolve
    it from a startup snapshot of the account's extensions.
    """
    try:
        resp = platform.get("/restapi/v1.0/account/~/extension", {"perPage": 250}).json_dict()
    except Exception as e:
        logger.warning("Failed to load extension list for ext-number map: %s", e)
        return {}
    out = {}
    for e in resp.get("records", []):
        num = e.get("extensionNumber")
        if num is not None:
            out[str(e["id"])] = str(num)
    return out


def _load_ext_display_map(platform) -> dict[str, str]:
    """Return {extensionId: displayName} for every extension on the account.

    Outbound telephony events commonly omit the rep's own name on the `from`
    party (only extensionId is present), so we resolve to display name via a
    startup snapshot. Used to tag transcript highlights as rep-vs-caller.
    """
    try:
        resp = platform.get("/restapi/v1.0/account/~/extension", {"perPage": 250}).json_dict()
    except Exception as e:
        logger.warning("Failed to load extension list for ext-display map: %s", e)
        return {}
    out = {}
    for e in resp.get("records", []):
        name = e.get("name")
        if name:
            out[str(e["id"])] = name
    return out


def _resolve_rep_first_name(parties, monitored_extensions, ext_display_map):
    """First word of the monitored rep's display name, or None.

    Scans each party; the first one whose extensionId is in `monitored_extensions`
    wins. Display name comes from any of: party.name, party.to.name, party.from.name,
    or the ext_display_map fallback.
    """
    if not monitored_extensions:
        return None
    for p in parties:
        p_ext_id = (p.get("extensionId")
                    or p.get("to", {}).get("extensionId")
                    or p.get("from", {}).get("extensionId"))
        if not p_ext_id or p_ext_id not in monitored_extensions:
            continue
        name = (p.get("name")
                or p.get("to", {}).get("name")
                or p.get("from", {}).get("name"))
        if not name and ext_display_map:
            name = ext_display_map.get(p_ext_id)
        if name:
            return name.split()[0]
    return None


async def run_monitor(store: CallStore, sidecar=None) -> None:
    """Connect to RC WebSocket and process telephony events, reconnecting on disconnect."""
    event_filters = ["/restapi/v1.0/account/~/telephony/sessions"]

    backoff = 1
    while True:
        try:
            # Fresh SDK + JWT login on every iteration — avoids stale access/refresh tokens.
            # JWT (server-to-server) has no user-driven refresh flow; each login mints fresh tokens.
            sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
            platform = sdk.platform()
            platform.login(jwt=Config.RC_JWT)
            logger.info("Authenticated with RingCentral")

            ext = platform.get("/restapi/v1.0/account/~/extension/~").json_dict()
            logger.info("Monitoring as: %s (ext %s)", ext["name"], ext["extensionNumber"])

            ext_number_map = _load_ext_number_map(platform)
            logger.info("Loaded ext-number map for %d extensions", len(ext_number_map))
            ext_display_map = _load_ext_display_map(platform)
            logger.info("Loaded ext-display map for %d extensions", len(ext_display_map))

            snapshot = _fetch_active_session_events(platform)
            if snapshot:
                logger.info("Hydrating %d in-progress call(s) from snapshot", len(snapshot))
                for event in snapshot:
                    process_telephony_event(
                        event, store, sidecar=sidecar,
                        monitored_extensions=Config.MONITORED_EXTENSIONS,
                        ext_number_map=ext_number_map,
                        ext_display_map=ext_display_map,
                    )

            await _run_ws_session(sdk, event_filters, store, sidecar=sidecar,
                                  ext_number_map=ext_number_map,
                                  ext_display_map=ext_display_map)
            backoff = 1  # reset on clean exit
        except Exception as e:
            logger.warning("WebSocket session ended: %s. Reconnecting in %ds...", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _run_ws_session(sdk, event_filters, store: CallStore, sidecar=None,
                          ext_number_map=None, ext_display_map=None) -> None:
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

        # Full JSON dump is invaluable for debugging but drowns the log during
        # a real call — ~1 event/second with a 2KB payload each.
        logger.debug("RC notification: %s", json.dumps(event, default=str)[:2000])
        process_telephony_event(event, store, sidecar=sidecar,
                                monitored_extensions=Config.MONITORED_EXTENSIONS,
                                ext_number_map=ext_number_map,
                                ext_display_map=ext_display_map)

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
