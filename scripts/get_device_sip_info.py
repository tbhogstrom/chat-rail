"""One-time: list devices under an extension, confirm one is type OtherPhone,
and pull SIP credentials for the softphone-bridge sidecar.

Context:
    Phase 2 uses RingCentral's official ringcentral-softphone-ts SDK, which
    requires a device of type 'OtherPhone' (labeled 'Existing Phone' in the
    admin GUI). Devices of type 'SoftPhone' (the 'RingCentral Phone app'
    entries) are NOT usable with this SDK — confirmed by RC support
    (case 31250629, 2026-04-21, Tyler Liu).

Usage:
    python scripts/get_device_sip_info.py                  # authenticated user
    python scripts/get_device_sip_info.py --extension-id 120
    python scripts/get_device_sip_info.py --region NA      # outbound proxy region
"""
import argparse
import os
import sys

from dotenv import load_dotenv
from ringcentral import SDK


def main() -> int:
    load_dotenv()

    ap = argparse.ArgumentParser()
    ap.add_argument("--extension-id", default="~",
                    help="Extension ID (default: ~ = authenticated user)")
    ap.add_argument("--region", default="NA",
                    help="Outbound-proxy region: NA, EMEA, APAC, LATAM (default: NA)")
    args = ap.parse_args()

    for k in ("RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT", "RC_SERVER"):
        if not os.environ.get(k):
            print(f"Missing env var: {k}", file=sys.stderr)
            return 2

    sdk = SDK(os.environ["RC_CLIENT_ID"],
              os.environ["RC_CLIENT_SECRET"],
              os.environ["RC_SERVER"])
    platform = sdk.platform()
    platform.login(jwt=os.environ["RC_JWT"])

    # 1) List devices on the extension
    devices = platform.get(
        f"/restapi/v1.0/account/~/extension/{args.extension_id}/device"
    ).json_dict()

    print("\n=== Devices on extension", args.extension_id, "===")
    usable = []
    for d in devices.get("records", []):
        name = d.get("name") or d.get("computerName") or ""
        print(f"  id={d['id']:<12}  type={d['type']:<12}  name={name}")
        if d["type"] == "OtherPhone":
            usable.append(d)

    if not usable:
        print("\nNo device of type 'OtherPhone' found on this extension.")
        print("Fix:")
        print("  1. Log in to https://service.ringcentral.com")
        print("  2. Find the user/extension, open 'Devices & Numbers'")
        print("  3. Add a new device with type 'Existing Phone'")
        print("  4. Re-run this script")
        return 1

    device = usable[0]
    print(f"\nUsing device {device['id']} ({device.get('name') or device.get('computerName','')})")

    # 2) Fetch SIP credentials for that device
    sip_info = platform.get(
        f"/restapi/v1.0/account/~/device/{device['id']}/sip-info"
    ).json_dict()

    # 3) Pick the TLS outbound proxy for the requested region
    proxy_tls = None
    for p in sip_info.get("outboundProxies", []):
        if p.get("region") == args.region:
            proxy_tls = p.get("proxyTLS")
            break
    if not proxy_tls and sip_info.get("outboundProxies"):
        proxy_tls = sip_info["outboundProxies"][0].get("proxyTLS")
    if not proxy_tls:
        print("\nNo outbound proxy found in SIP info response — aborting.", file=sys.stderr)
        return 3

    print("\n=== Add these to softphone-bridge/.env ===\n")
    print(f"SIP_INFO_DOMAIN={sip_info['domain']}")
    print(f"SIP_INFO_OUTBOUND_PROXY={proxy_tls}")
    print(f"SIP_INFO_USERNAME={sip_info['userName']}")
    print(f"SIP_INFO_PASSWORD={sip_info['password']}")
    print(f"SIP_INFO_AUTHORIZATION_ID={sip_info['authorizationId']}")
    print(f"# Reference only: RC_DEVICE_ID={device['id']}")
    print()
    print("Keep these credentials secret — treat the password like the RC JWT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
