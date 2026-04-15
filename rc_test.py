"""
RingCentral API Test Harness
Tests authentication, account info, active calls, telephony subscriptions,
and explores available real-time data.
"""
import os
import json
import time
from dotenv import load_dotenv
from ringcentral import SDK

load_dotenv()

CLIENT_ID = os.getenv("RC_CLIENT_ID")
CLIENT_SECRET = os.getenv("RC_CLIENT_SECRET")
JWT = os.getenv("RC_JWT")
SERVER = os.getenv("RC_SERVER")


def create_sdk():
    sdk = SDK(CLIENT_ID, CLIENT_SECRET, SERVER)
    platform = sdk.platform()
    platform.login(jwt=JWT)
    print("[OK] Authenticated with RingCentral")
    return sdk, platform


def test_account_info(platform):
    print("\n" + "=" * 60)
    print("ACCOUNT INFO")
    print("=" * 60)
    resp = platform.get("/restapi/v1.0/account/~")
    data = resp.json_dict()
    print(json.dumps(data, indent=2))
    return data


def test_extension_info(platform):
    print("\n" + "=" * 60)
    print("CURRENT EXTENSION INFO")
    print("=" * 60)
    resp = platform.get("/restapi/v1.0/account/~/extension/~")
    data = resp.json_dict()
    print(json.dumps(data, indent=2))
    return data


def test_active_calls(platform):
    print("\n" + "=" * 60)
    print("ACTIVE CALLS (current extension)")
    print("=" * 60)
    resp = platform.get("/restapi/v1.0/account/~/extension/~/active-calls")
    data = resp.json_dict()
    if data.get("records"):
        for call in data["records"]:
            print(json.dumps(call, indent=2))
    else:
        print("No active calls right now.")
    return data


def test_call_log_recent(platform):
    print("\n" + "=" * 60)
    print("RECENT CALL LOG (last 5 calls)")
    print("=" * 60)
    resp = platform.get("/restapi/v1.0/account/~/extension/~/call-log", {
        "perPage": 5,
        "view": "Detailed"
    })
    data = resp.json_dict()
    if data.get("records"):
        for call in data["records"]:
            print(f"\n--- Call: {call.get('id', 'N/A')} ---")
            print(f"  Direction: {call.get('direction')}")
            print(f"  Type: {call.get('type')}")
            print(f"  Result: {call.get('result')}")
            print(f"  From: {call.get('from', {}).get('phoneNumber', 'N/A')}")
            print(f"  To: {call.get('to', {}).get('phoneNumber', 'N/A')}")
            print(f"  Duration: {call.get('duration', 0)}s")
            print(f"  Start: {call.get('startTime')}")
            if call.get("recording"):
                print(f"  Recording: {json.dumps(call['recording'], indent=4)}")
            # Check for any transcript/AI fields
            for key in call:
                if any(term in key.lower() for term in ["transcript", "ai", "insight", "caption", "text"]):
                    print(f"  ** {key}: {call[key]}")
    else:
        print("No recent calls.")
    return data


def test_telephony_sessions(platform):
    print("\n" + "=" * 60)
    print("TELEPHONY SESSIONS (account-level)")
    print("=" * 60)
    try:
        resp = platform.get("/restapi/v1.0/account/~/telephony/sessions")
        data = resp.json_dict()
        print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"  Error: {e}")


def test_features(platform):
    print("\n" + "=" * 60)
    print("EXTENSION FEATURES (checking for supervision/monitoring)")
    print("=" * 60)
    try:
        resp = platform.get("/restapi/v1.0/account/~/extension/~/features")
        data = resp.json_dict()
        interesting = []
        for feature in data.get("records", []):
            name = feature.get("id", "").lower()
            if any(term in name for term in [
                "monitor", "supervis", "transcript", "ai", "record",
                "caption", "barge", "whisper", "call-control"
            ]):
                interesting.append(feature)
        if interesting:
            for f in interesting:
                print(f"  {f.get('id')}: available={f.get('available')} enabled={f.get('enabled', 'N/A')}")
                if f.get("params"):
                    print(f"    params: {json.dumps(f['params'], indent=6)}")
        else:
            print("  No monitoring/supervision/transcript features found.")
            print(f"  Total features checked: {len(data.get('records', []))}")
    except Exception as e:
        print(f"  Error: {e}")


def test_call_monitoring_groups(platform):
    print("\n" + "=" * 60)
    print("CALL MONITORING GROUPS")
    print("=" * 60)
    try:
        resp = platform.get("/restapi/v1.0/account/~/call-monitoring-groups")
        data = resp.json_dict()
        if data.get("records"):
            for group in data["records"]:
                print(json.dumps(group, indent=2))
        else:
            print("  No call monitoring groups configured.")
            print("  (These are required for the Supervision API)")
    except Exception as e:
        print(f"  Error: {e}")


def test_available_subscription_filters(platform):
    print("\n" + "=" * 60)
    print("TESTING SUBSCRIPTION FILTER AVAILABILITY")
    print("=" * 60)
    filters = [
        "/restapi/v1.0/account/~/telephony/sessions",
        "/restapi/v1.0/account/~/extension/~/telephony/sessions",
        "/restapi/v1.0/account/~/extension/~/presence",
        "/restapi/v1.0/account/~/presence",
    ]
    for f in filters:
        print(f"  Filter: {f}")
        print(f"    (will test via WebSocket subscription)")


def list_all_api_endpoints(platform):
    """Try to discover any transcript/AI related endpoints."""
    print("\n" + "=" * 60)
    print("PROBING TRANSCRIPT/AI ENDPOINTS")
    print("=" * 60)
    endpoints = [
        ("/restapi/v1.0/account/~/telephony/sessions", "Telephony sessions"),
        ("/ai/audio/v1/async/speech-to-text", "AI Speech-to-Text"),
        ("/ai/insights/v1/async/analyze-interaction", "AI Interaction Analysis"),
        ("/restapi/v1.0/account/~/call-monitoring-groups", "Call Monitoring Groups"),
    ]
    for endpoint, name in endpoints:
        try:
            resp = platform.get(endpoint)
            print(f"\n  [OK] {name}: {endpoint}")
            data = resp.json_dict()
            # Print keys only to see structure
            if isinstance(data, dict):
                print(f"    Top-level keys: {list(data.keys())}")
        except Exception as e:
            error_str = str(e)
            if "404" in error_str:
                print(f"  [404] {name}: {endpoint} — not available")
            elif "403" in error_str:
                print(f"  [403] {name}: {endpoint} — forbidden (may need permissions)")
            else:
                print(f"  [ERR] {name}: {endpoint} — {error_str[:100]}")


if __name__ == "__main__":
    print("RingCentral API Test Harness")
    print("=" * 60)

    sdk, platform = create_sdk()

    test_account_info(platform)
    test_extension_info(platform)
    test_active_calls(platform)
    test_call_log_recent(platform)
    test_telephony_sessions(platform)
    test_features(platform)
    test_call_monitoring_groups(platform)
    list_all_api_endpoints(platform)

    print("\n" + "=" * 60)
    print("DONE — Review output above to see what's available.")
    print("Next step: WebSocket subscription test for real-time events.")
    print("=" * 60)
