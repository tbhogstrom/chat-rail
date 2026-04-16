import logging
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
