# WSL Development Setup for Phase 2 (Live Transcripts)

Phase 2 requires a Linux environment because `pjsua2` (the SIP/RTP library) is hard to build on Windows.
Phase 1 code continues to work on Windows; only `run_live.py` (and the SIP monitor it uses) needs Linux.

## 1. Install WSL (if not already)

From PowerShell (Administrator):

```powershell
wsl --install -d Ubuntu-24.04
```

Reboot if prompted. Open "Ubuntu 24.04" from the Start menu, set a username and password. You should land in a bash prompt.

## 2. System packages

```bash
sudo apt update
sudo apt install -y \
    python3 python3-venv python3-pip python3-dev \
    build-essential pkg-config swig \
    libasound2-dev libssl-dev \
    git curl \
    ffmpeg
```

## 3. Clone the repo inside WSL

**Don't work on `/mnt/c/...`** — it's slow and causes file-permission issues. Clone into the Linux home dir:

```bash
cd ~
git clone https://github.com/tbhogstrom/chat-rail.git
cd chat-rail
```

## 4. Python virtualenv + deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 5. Install pjsua2 Python bindings

Pre-built wheels are not reliably available on PyPI; build from source:

```bash
# Build PJSIP (the C library)
cd /tmp
wget https://github.com/pjsip/pjproject/archive/refs/tags/2.14.tar.gz
tar xzf 2.14.tar.gz
cd pjproject-2.14

./configure --enable-shared CFLAGS="-fPIC"
make dep
make
sudo make install
sudo ldconfig

# Build Python bindings against the venv's Python
cd pjsip-apps/src/swig
make python
cd python
python3 setup.py install
```

Back in the chat-rail repo, verify:

```bash
cd ~/chat-rail && source .venv/bin/activate
python3 -c "import pjsua2; print('pjsua2 OK')"
```

If it prints `pjsua2 OK` without errors, you're good.

## 6. Copy .env from Windows into WSL

From WSL (the Windows `C:\` drive is mounted at `/mnt/c`):

```bash
cp /mnt/c/Users/tfalcon/callrail-chatgpt/.env ~/chat-rail/.env
```

Or edit it fresh inside WSL with `nano ~/chat-rail/.env`.

## 7. Run the Phase 1 pieces to verify the env

```bash
cd ~/chat-rail && source .venv/bin/activate
python run_local.py
```

You should see the same WebSocket / call-event logs you got on Windows. Ctrl+C to stop.

## 8. Provision SIP credentials for ext 120 (one-time, Phase 2 only)

RC issues SIP credentials per-device. Run this helper to provision ours (file is temporary — delete after):

```python
# provision_sip.py
import os, json
from dotenv import load_dotenv
from ringcentral import SDK

load_dotenv()
sdk = SDK(os.environ['RC_CLIENT_ID'], os.environ['RC_CLIENT_SECRET'], os.environ['RC_SERVER'])
platform = sdk.platform()
platform.login(jwt=os.environ['RC_JWT'])

body = {
    "sipInfo": [{"transport": "UDP"}],
    "device": {"computerName": "sfw-call-bridge"},
}
resp = platform.post("/restapi/v1.0/client-info/sip-provision", body=body)
data = resp.json_dict()
print(json.dumps(data, indent=2))

info = data["sipInfo"][0]
print("\nAdd these to .env:")
print(f"SIP_DOMAIN={info['domain']}")
print(f"SIP_USERNAME={info['username']}")
print(f"SIP_PASSWORD={info['password']}")
print(f"SIP_AUTH_ID={info['authorizationId']}")
print(f"SIP_DEVICE_ID={data['device']['id']}")
```

```bash
python provision_sip.py
```

Copy the five `SIP_*=...` lines it prints into your `.env`, then delete the script:

```bash
rm provision_sip.py
```

**Security note:** The SIP password in `.env` is effectively a permanent credential. Treat it like the RC JWT — never commit it.
