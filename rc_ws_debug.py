"""
RingCentral WebSocket Debug — capture ALL messages on the socket.
"""
import os
import json
import time
import asyncio
from dotenv import load_dotenv
from ringcentral import SDK
from ringcentral.websocket.web_socket_client import WebSocketEvents

load_dotenv()

sdk = SDK(os.getenv("RC_CLIENT_ID"), os.getenv("RC_CLIENT_SECRET"), os.getenv("RC_SERVER"))
platform = sdk.platform()
platform.login(jwt=os.getenv("RC_JWT"))
print("[OK] Authenticated", flush=True)

ext_data = platform.get("/restapi/v1.0/account/~/extension/~").json_dict()
ext_id = ext_data["id"]

event_filters = [
    "/restapi/v1.0/account/~/telephony/sessions",
    f"/restapi/v1.0/account/~/extension/{ext_id}/telephony/sessions",
    f"/restapi/v1.0/account/~/extension/{ext_id}/presence?detailedTelephonyState=true&sipData=true",
]

msg_count = 0

def on_any_event(event_name):
    def handler(*args):
        global msg_count
        msg_count += 1
        print(f"\n[MSG #{msg_count}] event={event_name} time={time.strftime('%H:%M:%S')}", flush=True)
        for arg in args:
            if isinstance(arg, (dict, list)):
                print(json.dumps(arg, indent=2, default=str), flush=True)
            elif isinstance(arg, str):
                try:
                    parsed = json.loads(arg)
                    print(json.dumps(parsed, indent=2, default=str), flush=True)
                except:
                    print(f"  raw: {arg[:500]}", flush=True)
            else:
                print(f"  type={type(arg).__name__}", flush=True)
    return handler


async def main():
    ws = sdk.create_web_socket_client()
    tok = ws.get_web_socket_token()
    print(f"[OK] Token acquired", flush=True)

    # Listen to ALL known event types
    for evt in [e for e in dir(WebSocketEvents) if not e.startswith("_")]:
        val = getattr(WebSocketEvents, evt)
        ws.on(val, on_any_event(evt))
        print(f"  Listening for: {evt} = {val}", flush=True)

    # Also listen for generic event names
    for name in ["notification", "message", "receiveMessage", "data"]:
        ws.on(name, on_any_event(name))

    print(f"\nConnecting...", flush=True)
    task = asyncio.create_task(ws.open_connection(tok["uri"], tok["ws_access_token"]))

    # Wait for connection, then subscribe
    await asyncio.sleep(2)
    print(f"\nis_ready: {ws._is_ready}", flush=True)

    if ws._is_ready:
        await ws.create_subscription(event_filters)
        print("[OK] Subscription created", flush=True)
    else:
        print("[WARN] WebSocket not ready yet", flush=True)

    print(f"\nListening for 2 minutes... ({time.strftime('%H:%M:%S')})\n", flush=True)

    # Also poll active calls every 15s to compare
    for i in range(8):
        await asyncio.sleep(15)
        try:
            resp = platform.get("/restapi/v1.0/account/~/active-calls", {"perPage": 10})
            calls = resp.json_dict().get("records", [])
            if calls:
                print(f"\n[POLL {time.strftime('%H:%M:%S')}] {len(calls)} active call(s):", flush=True)
                for c in calls:
                    print(f"  {c.get('direction')} {c.get('from', {}).get('name', '?')} -> result={c.get('result')} session={c.get('sessionId')}", flush=True)
            else:
                print(f"[POLL {time.strftime('%H:%M:%S')}] No active calls", flush=True)
        except Exception as e:
            print(f"[POLL ERROR] {e}", flush=True)

    print(f"\nDone. {msg_count} WebSocket messages received.", flush=True)
    task.cancel()

asyncio.run(main())
