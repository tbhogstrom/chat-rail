"""
RingCentral Data Dump — pull all available data from the account.
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


def section(title):
    print(f"\n{'=' * 60}", flush=True)
    print(title, flush=True)
    print('=' * 60, flush=True)


def safe_get(endpoint, params=None):
    try:
        resp = platform.get(endpoint, params or {})
        return resp.json_dict()
    except Exception as e:
        print(f"  [ERROR] {e}", flush=True)
        return None


# 1. All extensions
section("ALL EXTENSIONS ON ACCOUNT")
data = safe_get("/restapi/v1.0/account/~/extension", {"perPage": 100})
if data:
    for ext in data.get("records", []):
        print(f"  Ext {ext.get('extensionNumber', '?'):>5} | {ext.get('name', 'N/A'):30} | type={ext.get('type'):15} | status={ext.get('status')}", flush=True)

# 2. Call monitoring group details
section("CALL MONITORING GROUP 'SALES' — MEMBERS")
data = safe_get("/restapi/v1.0/account/~/call-monitoring-groups/1698052/members")
if data:
    for member in data.get("records", []):
        print(json.dumps(member, indent=2, default=str), flush=True)
else:
    print("  No members or could not fetch.", flush=True)

# 3. Account-level call log (all extensions)
section("ACCOUNT-LEVEL CALL LOG (last 10 calls)")
data = safe_get("/restapi/v1.0/account/~/call-log", {"perPage": 10, "view": "Detailed"})
if data:
    records = data.get("records", [])
    if records:
        for call in records:
            print(f"\n  --- Call: {call.get('id', 'N/A')} ---", flush=True)
            print(f"  Direction: {call.get('direction')}", flush=True)
            print(f"  Type: {call.get('type')}", flush=True)
            print(f"  Result: {call.get('result')}", flush=True)
            print(f"  From: {call.get('from', {}).get('name', '')} {call.get('from', {}).get('phoneNumber', 'N/A')}", flush=True)
            print(f"  To: {call.get('to', {}).get('name', '')} {call.get('to', {}).get('phoneNumber', 'N/A')}", flush=True)
            print(f"  Duration: {call.get('duration', 0)}s", flush=True)
            print(f"  Start: {call.get('startTime')}", flush=True)
            if call.get("recording"):
                print(f"  Recording: {json.dumps(call['recording'], indent=4, default=str)}", flush=True)
            if call.get("legs"):
                print(f"  Legs: {len(call['legs'])}", flush=True)
                for i, leg in enumerate(call["legs"]):
                    print(f"    Leg {i}: {leg.get('direction')} {leg.get('from', {}).get('phoneNumber', '?')} -> {leg.get('to', {}).get('phoneNumber', '?')} result={leg.get('result')} duration={leg.get('duration', 0)}s", flush=True)
            # Dump full record for first call to see ALL available fields
            if call == records[0]:
                print(f"\n  [FULL PAYLOAD for first call]:", flush=True)
                print(json.dumps(call, indent=2, default=str), flush=True)
    else:
        print("  No calls in log.", flush=True)

# 4. Active calls (account-wide)
section("ACTIVE CALLS (account-wide)")
data = safe_get("/restapi/v1.0/account/~/active-calls", {"perPage": 50})
if data:
    records = data.get("records", [])
    if records:
        for call in records:
            print(json.dumps(call, indent=2, default=str), flush=True)
    else:
        print("  No active calls.", flush=True)

# 5. Presence for all extensions
section("PRESENCE (all extensions)")
data = safe_get("/restapi/v1.0/account/~/presence", {"detailedTelephonyState": "true", "perPage": 50})
if data:
    for record in data.get("records", []):
        ext_num = record.get("extension", {}).get("extensionNumber", "?")
        tel_status = record.get("telephonyStatus", "N/A")
        presence = record.get("presenceStatus", "N/A")
        user_status = record.get("userStatus", "N/A")
        print(f"  Ext {ext_num}: presence={presence}, telephony={tel_status}, userStatus={user_status}", flush=True)
        if record.get("activeCalls"):
            print(f"    Active calls: {json.dumps(record['activeCalls'], indent=4, default=str)}", flush=True)

# 6. Phone numbers on the account
section("PHONE NUMBERS ON ACCOUNT")
data = safe_get("/restapi/v1.0/account/~/phone-number", {"perPage": 50})
if data:
    for num in data.get("records", []):
        print(f"  {num.get('phoneNumber', 'N/A'):15} | type={num.get('type', '?'):10} | usageType={num.get('usageType', '?'):20} | label={num.get('label', '')}", flush=True)

# 7. Service info (plan details)
section("SERVICE INFO (subscription/plan)")
data = safe_get("/restapi/v1.0/account/~/service-info")
if data:
    print(json.dumps(data, indent=2, default=str)[:3000], flush=True)

print(f"\n{'=' * 60}", flush=True)
print("DATA DUMP COMPLETE", flush=True)
print('=' * 60, flush=True)
