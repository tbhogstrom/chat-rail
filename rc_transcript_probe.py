"""
Probe every possible transcript-related endpoint on RingCentral.
Using real session IDs from active/recent calls.
"""
import os
import json
from dotenv import load_dotenv
from ringcentral import SDK

load_dotenv()

sdk = SDK(os.getenv("RC_CLIENT_ID"), os.getenv("RC_CLIENT_SECRET"), os.getenv("RC_SERVER"))
platform = sdk.platform()
platform.login(jwt=os.getenv("RC_JWT"))
print("[OK] Authenticated\n", flush=True)

# Get recent calls with session IDs
resp = platform.get("/restapi/v1.0/account/~/call-log", {"perPage": 5, "view": "Detailed"})
calls = resp.json_dict().get("records", [])
print(f"Recent calls to test with:", flush=True)
for c in calls[:3]:
    print(f"  session={c.get('sessionId')} telSession={c.get('telephonySessionId')} from={c.get('from',{}).get('name','')} rec={c.get('recording',{}).get('id','none')}", flush=True)

# Also get active calls
resp2 = platform.get("/restapi/v1.0/account/~/active-calls", {"perPage": 10})
active = resp2.json_dict().get("records", [])
print(f"\nActive calls:", flush=True)
for c in active:
    print(f"  session={c.get('sessionId')} telSession={c.get('telephonySessionId')} from={c.get('from',{}).get('name','')} result={c.get('result')}", flush=True)

# Collect session IDs to probe
session_ids = []
tel_session_ids = []
recording_ids = []
for c in calls + active:
    if c.get("sessionId"):
        session_ids.append(c["sessionId"])
    if c.get("telephonySessionId"):
        tel_session_ids.append(c["telephonySessionId"])
    if c.get("recording", {}).get("id"):
        recording_ids.append(c["recording"]["id"])

def probe(label, endpoint, method="GET", body=None):
    try:
        if method == "GET":
            resp = platform.get(endpoint)
        elif method == "POST":
            resp = platform.post(endpoint, body=body)
        data = resp.json_dict()
        print(f"\n[OK] {label}", flush=True)
        print(f"  Endpoint: {endpoint}", flush=True)
        text = json.dumps(data, indent=2, default=str)
        # Truncate large responses
        if len(text) > 2000:
            print(text[:2000] + "\n  ... (truncated)", flush=True)
        else:
            print(text, flush=True)
        return data
    except Exception as e:
        err = str(e)
        if "404" in err:
            print(f"[404] {label}: {endpoint}", flush=True)
        elif "403" in err:
            print(f"[403] {label}: {endpoint} — forbidden", flush=True)
        elif "405" in err:
            print(f"[405] {label}: {endpoint} — method not allowed", flush=True)
        elif "400" in err:
            print(f"[400] {label}: {endpoint} — bad request", flush=True)
            print(f"  {err[:200]}", flush=True)
        else:
            print(f"[ERR] {label}: {endpoint}", flush=True)
            print(f"  {err[:300]}", flush=True)
        return None

print(f"\n{'='*60}", flush=True)
print("PROBING TRANSCRIPT ENDPOINTS", flush=True)
print('='*60, flush=True)

# Telephony session details
if tel_session_ids:
    tsid = tel_session_ids[0]
    probe("Telephony session details", f"/restapi/v1.0/account/~/telephony/sessions/{tsid}")
    probe("Telephony session parties", f"/restapi/v1.0/account/~/telephony/sessions/{tsid}/parties")

# Transcript/caption variations on sessions
if tel_session_ids:
    tsid = tel_session_ids[0]
    probe("Session transcription", f"/restapi/v1.0/account/~/telephony/sessions/{tsid}/transcription")
    probe("Session transcript", f"/restapi/v1.0/account/~/telephony/sessions/{tsid}/transcript")
    probe("Session captions", f"/restapi/v1.0/account/~/telephony/sessions/{tsid}/captions")
    probe("Session live-transcription", f"/restapi/v1.0/account/~/telephony/sessions/{tsid}/live-transcription")
    probe("Session insights", f"/restapi/v1.0/account/~/telephony/sessions/{tsid}/insights")

# AI endpoints
probe("AI speech-to-text", "/ai/audio/v1/async/speech-to-text")
probe("AI insights", "/ai/insights/v1/async/analyze-interaction")
probe("AI text summary", "/ai/text/v1/async/summarize")

# RingSense endpoints
if tel_session_ids:
    tsid = tel_session_ids[0]
    probe("RingSense call insights", f"/ai/ringsense/v1/public/accounts/~/domains/pbx/records/{tsid}/insights")

# Recording-based transcript
if recording_ids:
    rid = recording_ids[0]
    probe("Recording content", f"/restapi/v1.0/account/~/recording/{rid}")
    probe("Recording transcript", f"/restapi/v1.0/account/~/recording/{rid}/transcript")
    probe("Recording transcription", f"/restapi/v1.0/account/~/recording/{rid}/transcription")
    probe("Recording insights", f"/restapi/v1.0/account/~/recording/{rid}/insights")
    probe("Recording content-info", f"/restapi/v1.0/account/~/recording/{rid}/content")

# v2 API variations
probe("v2 telephony sessions", "/restapi/v2/account/~/telephony/sessions")
if tel_session_ids:
    tsid = tel_session_ids[0]
    probe("v2 session detail", f"/restapi/v2/account/~/telephony/sessions/{tsid}")

# Call log with transcription view
if calls:
    cid = calls[0]["id"]
    probe("Call log detailed+transcript", f"/restapi/v1.0/account/~/call-log/{cid}?view=Detailed")

# Account-level AI features
probe("AI features", "/restapi/v1.0/account/~/ai")
probe("AI status", "/ai/status/v1/accounts/~")

# Extension-level transcript settings
ext_id = platform.get("/restapi/v1.0/account/~/extension/~").json_dict()["id"]
probe("Extension AI settings", f"/restapi/v1.0/account/~/extension/{ext_id}/ai")
probe("Extension transcript settings", f"/restapi/v1.0/account/~/extension/{ext_id}/transcription")

# WebSocket subscription with transcript filters
print(f"\n{'='*60}", flush=True)
print("TESTING SUBSCRIPTION FILTERS FOR TRANSCRIPT EVENTS", flush=True)
print('='*60, flush=True)

# Try subscribing to transcript-related event filters
transcript_filters = [
    "/restapi/v1.0/account/~/telephony/sessions?withTranscript=true",
    "/restapi/v1.0/account/~/telephony/transcription",
    "/restapi/v1.0/account/~/ai/transcription",
    "/restapi/v1.0/account/~/extension/~/telephony/sessions/transcription",
]
for f in transcript_filters:
    print(f"  Filter: {f}", flush=True)
    try:
        resp = platform.post("/restapi/v1.0/subscription", body={
            "eventFilters": [f],
            "deliveryMode": {"transportType": "WebHook", "address": "https://example.com/webhook"}
        })
        data = resp.json_dict()
        print(f"    [OK] Subscription created! Filter accepted.", flush=True)
        # Clean up
        sub_id = data.get("id")
        if sub_id:
            platform.delete(f"/restapi/v1.0/subscription/{sub_id}")
    except Exception as e:
        err = str(e)
        if "CMN-120" in err or "invalid" in err.lower():
            print(f"    [REJECTED] Invalid filter", flush=True)
        else:
            print(f"    [ERROR] {str(e)[:200]}", flush=True)

print(f"\n{'='*60}", flush=True)
print("PROBE COMPLETE", flush=True)
print('='*60, flush=True)
