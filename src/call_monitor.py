import asyncio
import json
import logging
from datetime import datetime, timezone

from ringcentral import SDK
from ringcentral.websocket.web_socket_client import WebSocketEvents

from src.config import Config
from src.redis_store import CallStore

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {"Proceeding", "Answered", "Hold"}
END_STATUSES = {"Disconnected", "Gone", "VoiceMail"}
# A party is *connected* (actually on the call) only once it answers — a
# Proceeding (ringing) leg in a simulring/queue session is not yet on the call.
CONNECTED_STATUSES = {"Answered", "Hold"}


def _connected_ext_ids(parties) -> list[str]:
    """Extension IDs of parties currently connected (Answered/Hold).

    Used to tell which reps are *actually on* a multi-leg/queue session vs.
    merely being rung. Without this, every rung rep's rep:{ext}:current points
    at the still-active session and would read as "on call".
    """
    out: list[str] = []
    for p in parties:
        if p.get("status", {}).get("code", "") not in CONNECTED_STATUSES:
            continue
        for ext_id in filter(None, [
            p.get("extensionId"),
            (p.get("to") or {}).get("extensionId"),
            (p.get("from") or {}).get("extensionId"),
        ]):
            if ext_id not in out:
                out.append(ext_id)
    return out


def process_telephony_event(event: dict, store: CallStore,
                            sidecar=None, monitored_extensions=None,
                            ext_number_map=None,
                            ext_display_map=None) -> None:
    """Process a single RC telephony session notification and update store.

    Supervision trigger scans ALL parties (not just parties[0]) because
    queue/IVR-routed calls put the actual rep beyond index 0.
    """
    body = event.get("body", {})
    # RC's WS notifications carry the session id as `telephonySessionId`, but
    # the REST snapshot (GET /telephony/sessions/{sid}) returns it as `id`.
    # Accept either so snapshot-hydrated calls aren't silently dropped.
    session_id = body.get("telephonySessionId") or body.get("id")
    if not session_id:
        logger.warning("Event missing session id: %s", event)
        return

    parties = body.get("parties", [])
    if not parties:
        return

    party = parties[0]
    direction = party.get("direction", "Unknown")
    from_info = party.get("from", {})
    to_info = party.get("to", {})

    # Displayed status prefers the monitored rep's own leg: on multi-leg
    # sessions parties[0] is often a queue leg or a stale earlier leg whose
    # state says nothing about whether the rep is talking.
    status = party.get("status", {}).get("code", "Unknown")
    if monitored_extensions:
        for p in parties:
            p_ext = (p.get("extensionId")
                     or (p.get("to") or {}).get("extensionId")
                     or (p.get("from") or {}).get("extensionId"))
            if p_ext and p_ext in monitored_extensions:
                status = p.get("status", {}).get("code", status)
                break

    rep_first_name = _resolve_rep_first_name(parties, monitored_extensions, ext_display_map)
    connected_ext_ids = _connected_ext_ids(parties)

    call_data = {
        "sessionId": session_id,
        "status": status,
        "direction": direction,
        "from": from_info,
        "to": to_info,
        "rep_first_name": rep_first_name,
        # Reps whose own leg is connected right now (drives the overview's
        # on-call state; excludes rung-but-unanswered simulring legs).
        "activeExtIds": connected_ext_ids,
        # Age guard for the periodic reconcile: RC's active-calls listing can
        # lag on brand-new calls, so only event-silent sessions get swept.
        "lastEventAt": datetime.now(timezone.utc).isoformat(),
    }

    # A session ends only when EVERY leg has ended: one stale leg going Gone
    # in parties[0] must not flap a still-talking rep to idle.
    statuses = [p.get("status", {}).get("code", "Unknown") for p in parties]
    if all(s in END_STATUSES for s in statuses):
        logger.info("Call ended: %s (status=%s)", session_id, status)
        store.complete_call(session_id)
    else:
        # One call fires many mid-state events (Proceeding -> Setup -> Answered
        # -> Hold -> ...); at INFO they drown out the interesting lines.
        logger.debug("Call event: %s (status=%s)", session_id, status)
        store.store_call(session_id, call_data)
        # Queue-routed / multi-leg calls put the actual rep in parties[1+].
        # Point rep:{extId}:current at this session for EVERY extensionId
        # seen, not just the primary party — otherwise the dashboard at
        # ?rep=<doug> fetches a stale earlier call because Doug's leg was
        # never the primary party. BUT a merely-ringing session must not
        # steal the pointer from a call the rep is connected to: queue
        # simulrings would flap the overview to idle and switch the rep's
        # live dashboard away from the in-progress transcript.
        for p in parties:
            for ext_id in filter(None, [
                p.get("extensionId"),
                (p.get("to")   or {}).get("extensionId"),
                (p.get("from") or {}).get("extensionId"),
            ]):
                if ext_id in connected_ext_ids \
                        or not _pointer_holds_live_call(store, ext_id, session_id):
                    store.set_rep_pointer(ext_id, session_id)

    # Supervision trigger — two-pass scan over ALL monitored parties.
    # START wins over STOP: one monitored rep's ended leg (an abandoned
    # simulring leg, a colleague dropping off a conference) must not kill
    # supervision while another monitored rep is still on the call. The
    # bridge 409s duplicate starts, so re-sending START on snapshot
    # hydration is also how supervision RESUMES after a WS reconnect.
    if not (sidecar and monitored_extensions):
        return
    scanned = []
    answered = []   # (ext_id, ext_number) for monitored reps currently Answered
    ended = []      # ext_ids for monitored reps whose leg has ended
    for p in parties:
        p_status = p.get("status", {}).get("code", "")
        p_ext_id = (p.get("extensionId")
                    or p.get("to", {}).get("extensionId")
                    or p.get("from", {}).get("extensionId"))
        scanned.append((p_ext_id, p_status))
        if not p_ext_id or p_ext_id not in monitored_extensions:
            continue
        if p_status == "Answered":
            p_ext_number = (p.get("to", {}).get("extensionNumber")
                            or p.get("from", {}).get("extensionNumber"))
            if not p_ext_number and ext_number_map:
                p_ext_number = ext_number_map.get(p_ext_id)
            if p_ext_number:
                answered.append((p_ext_id, p_ext_number))
            else:
                logger.warning("Supervision match %s but no ext_number for rep %s",
                               session_id, p_ext_id)
        elif p_status in END_STATUSES:
            ended.append(p_ext_id)
    logger.debug("Supervision scan %s: parties=%s monitored=%s",
                 session_id, scanned, monitored_extensions)
    if answered:
        ext_id, ext_number = answered[0]
        logger.info("Supervision START %s (rep %s, ext number %s)",
                    session_id, ext_id, ext_number)
        asyncio.create_task(sidecar.start_supervision(session_id, ext_number))
    elif ended:
        logger.info("Supervision STOP %s (rep %s)", session_id, ended[0])
        asyncio.create_task(sidecar.stop_supervision(session_id))


def _pointer_holds_live_call(store: CallStore, ext_id: str,
                             new_session_id: str) -> bool:
    """True when rep:{ext_id}:current points at a DIFFERENT session the rep
    is actively connected to — i.e. repointing would hijack a live call."""
    current = store.get_rep_current_call(ext_id)
    if not current or current.get("sessionId") == new_session_id:
        return False
    if ext_id not in (current.get("activeExtIds") or []):
        return False
    return store.is_active(current["sessionId"])


def _is_monitored(ext_id: str, monitored: list[str] | None) -> bool:
    if not monitored:
        return False  # Phase 2: empty = disabled (conservative)
    return ext_id in monitored


def _fetch_active_session_events(platform) -> tuple[list[dict], set[str]] | None:
    """Return (WS-shaped events, RC-active session ids) for in-progress calls,
    or None when the active-calls listing itself fails.

    RC's WebSocket only emits state-transition events, so calls that are already
    live when we connect (or that transition during a WS reconnect gap) are
    invisible. We hydrate by listing active calls, then fetching each session's
    full detail — which comes back in the same shape the WS emits — and wrapping
    it as `{"body": ...}` so callers can reuse `process_telephony_event`.

    The id set includes sessions whose detail fetch failed (they are still
    RC-active) and is the authority for `_reconcile_active_sessions`. None —
    as opposed to an empty set — means "unknown": callers must skip the
    reconcile rather than treat every Redis-active call as stale.
    """
    try:
        records = platform.get(
            "/restapi/v1.0/account/~/active-calls",
            {"perPage": 100, "view": "Detailed"},
        ).json_dict().get("records", [])
    except Exception as e:
        logger.warning("Active-calls snapshot failed: %s", e)
        return None

    events = []
    active_sids = set()
    for rec in records:
        sid = rec.get("telephonySessionId")
        if not sid:
            continue
        active_sids.add(sid)
        try:
            session = platform.get(
                f"/restapi/v1.0/account/~/telephony/sessions/{sid}"
            ).json_dict()
        except Exception as e:
            logger.warning("Session fetch failed for %s: %s", sid, e)
            continue
        events.append({"body": session})
    return events, active_sids


def _fetch_active_call_sids(platform) -> set[str] | None:
    """Just the RC-active session ids (no per-session detail fetches), for the
    periodic reconcile. None means the listing failed — callers must skip."""
    try:
        records = platform.get(
            "/restapi/v1.0/account/~/active-calls",
            {"perPage": 100, "view": "Simple"},
        ).json_dict().get("records", [])
    except Exception as e:
        logger.warning("Active-calls listing failed: %s", e)
        return None
    return {r["telephonySessionId"] for r in records if r.get("telephonySessionId")}


def _reconcile_active_sessions(store: CallStore, rc_active_sids: set[str],
                               min_age_seconds: int = 120,
                               now: datetime | None = None) -> None:
    """Complete Redis-active sessions that RingCentral no longer reports.

    End events that fire while the WS is down (or are simply dropped) are
    lost forever; without this, those sessions stay 'Answered' in
    calls:active indefinitely — frozen dashboards, unbounded sellometer
    timelines, and no finalization. Completing via the normal path runs the
    extraction grace window, so downstream final passes (extraction,
    sellometer history) still happen.

    Sessions with an event in the last `min_age_seconds` are spared: RC's
    active-calls listing can lag on brand-new calls, and a live call must
    never be swept over listing latency. Missing lastEventAt (legacy or
    expired state) counts as old.
    """
    now = now or datetime.now(timezone.utc)
    for sid in store.active_session_ids():
        if sid in rc_active_sids:
            continue
        last_event_at = (store.get_call(sid) or {}).get("lastEventAt")
        if last_event_at:
            try:
                age = (now - datetime.fromisoformat(last_event_at)).total_seconds()
                if age < min_age_seconds:
                    continue
            except ValueError:
                pass  # unparseable timestamp counts as old
        logger.info("Reconcile: completing stale session %s (not in RC active set)", sid)
        store.complete_call(sid)


async def _run_reconcile_loop(platform, store: CallStore,
                              interval: float = 60.0) -> None:
    """Periodic zombie sweep while a WS session is up. Missed end events now
    heal in ~a minute instead of waiting for the next WS reconnect."""
    while True:
        await asyncio.sleep(interval)
        try:
            sids = await asyncio.to_thread(_fetch_active_call_sids, platform)
            if sids is not None:
                _reconcile_active_sessions(store, sids)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reconcile loop failed")


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


def build_monitored_roster(display_map: dict, number_map: dict,
                           monitored: list[str]) -> dict[str, dict]:
    """Roster ({extId: {"name","number"}}) for the monitored extensions only.

    Name/number come from the account snapshot maps; either may be None if the
    map didn't include that extension.
    """
    return {
        ext_id: {
            "name": display_map.get(ext_id),
            "number": number_map.get(ext_id),
        }
        for ext_id in monitored
    }


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

            roster = build_monitored_roster(
                ext_display_map, ext_number_map, Config.MONITORED_EXTENSIONS)
            store.set_rep_roster(roster)
            logger.info("Persisted roster for %d monitored rep(s)", len(roster))

            snapshot = _fetch_active_session_events(platform)
            if snapshot is None:
                logger.warning("Active-calls snapshot unavailable — "
                               "skipping hydration and session reconcile")
            else:
                events, rc_active_sids = snapshot
                if events:
                    logger.info("Hydrating %d in-progress call(s) from snapshot", len(events))
                    for event in events:
                        process_telephony_event(
                            event, store, sidecar=sidecar,
                            monitored_extensions=Config.MONITORED_EXTENSIONS,
                            ext_number_map=ext_number_map,
                            ext_display_map=ext_display_map,
                        )
                _reconcile_active_sessions(store, rc_active_sids)

            reconcile_task = asyncio.create_task(
                _run_reconcile_loop(platform, store))
            try:
                await _run_ws_session(sdk, event_filters, store, sidecar=sidecar,
                                      ext_number_map=ext_number_map,
                                      ext_display_map=ext_display_map)
            finally:
                reconcile_task.cancel()
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
