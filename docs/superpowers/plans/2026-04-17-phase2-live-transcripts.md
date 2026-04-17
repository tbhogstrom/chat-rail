# Phase 2: Live Call Transcripts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture live audio from active RingCentral calls via the Supervision API, stream it to Deepgram for real-time speech-to-text, and accumulate transcripts in Redis so the GPT Action API can return the live conversation to ChatGPT as calls are happening.

**Architecture:** A Linux process (WSL for dev, Linux VPS for prod) registers with RingCentral as a SIP client using ext 120's credentials. When the Phase 1 call monitor sees `status=Answered` on a monitored extension, it calls the RC Supervision API with that session's ID — RC invites the SIP client into the call as a silent listener. The SIP client's audio pipeline feeds raw PCM frames to Deepgram's real-time WebSocket, which streams back transcript chunks. Each chunk is appended to the call's transcript in Redis. The existing FastAPI endpoints (unchanged) then serve that live transcript to ChatGPT.

**Tech Stack:** Python 3.12+, pjsua2 (PJSIP Python bindings), `websockets` (async WS client for Deepgram), asyncio, existing Redis + call_monitor

---

## Prerequisites (User must complete before Task 1)

1. **RC admin:** Ext 120 (Tyler Falcon) added as a monitor in the "Sales" call monitoring group. ✅ **DONE**
2. **WSL installed:** Windows user needs WSL 2 with Ubuntu 22.04 or 24.04. Run `wsl --install -d Ubuntu-24.04` from PowerShell if not already present.

---

## File Structure

```
callrail-chatgpt/
├── docs/
│   └── WSL_SETUP.md                     # Developer guide for WSL environment
├── src/
│   ├── supervision.py                   # RC Supervision API client (start/stop)
│   ├── sip_monitor.py                   # pjsua2 wrapper: register, accept invites, emit audio frames
│   ├── deepgram_stream.py               # Deepgram realtime WS client: stream audio in, get transcripts out
│   ├── live_transcriber.py              # Orchestrator: Supervision → SIP → Deepgram → Redis
│   ├── call_monitor.py                  # MODIFY: invoke live_transcriber on Answered events
│   └── config.py                        # MODIFY: add SFW_MONITORED_EXTENSIONS, SIP_* settings
├── tests/
│   ├── test_supervision.py              # Mock RC API calls
│   ├── test_deepgram_stream.py          # Mock Deepgram WebSocket
│   └── test_live_transcriber.py         # Integration with all mocks
└── run_live.py                          # WSL/Linux entrypoint: FastAPI + call_monitor + SIP registrar
```

---

### Task 1: WSL Environment Setup Guide

**Files:**
- Create: `docs/WSL_SETUP.md`

This isn't code — it's a reference document so the developer can get a reproducible WSL dev environment. Consolidates everything needed to install pjsua2, Python deps, and run the project.

- [ ] **Step 1: Create `docs/WSL_SETUP.md`**

```markdown
# WSL Development Setup for Phase 2 (Live Transcripts)

Phase 2 requires a Linux environment because `pjsua2` is hard to build on Windows.
Phase 1 code continues to work on Windows; only `run_live.py` (and the SIP monitor it uses) needs Linux.

## 1. Install WSL (if not already)

From PowerShell (Admin):
```
wsl --install -d Ubuntu-24.04
```

Reboot, open Ubuntu, set a username/password. You should land in a bash prompt.

## 2. System packages

```bash
sudo apt update
sudo apt install -y \
    python3 python3-venv python3-pip python3-dev \
    build-essential pkg-config \
    libasound2-dev libssl-dev \
    git curl \
    ffmpeg
```

## 3. Clone the repo inside WSL

Don't work on `/mnt/c/...` — slow and file-permission issues. Clone into the Linux home dir:

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

Pre-built wheel is not reliably available on PyPI; build from source:

```bash
# One-time: install PJSIP and its Python bindings
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
# Point at the venv's Python explicitly:
python3 setup.py install
```

Verify:
```bash
python3 -c "import pjsua2; print(pjsua2.Endpoint())"
```

If it prints something non-erroring, you're good.

## 6. Copy your .env into WSL

From Windows bash in this repo:
```bash
cp /mnt/c/Users/tfalcon/callrail-chatgpt/.env ~/chat-rail/.env
```

Or create it fresh inside WSL using your editor.

## 7. Run it

```bash
cd ~/chat-rail
source .venv/bin/activate
python run_live.py
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/WSL_SETUP.md
git commit -m "docs: add WSL development setup guide for Phase 2"
```

---

### Task 2: Config — Add monitored-extensions and SIP settings

**Files:**
- Modify: `src/config.py`
- Modify: `.env.example`

We need to know which extensions to monitor (to avoid supervising non-sales calls) and hold SIP connection config. The SIP settings are read after device provisioning (Task 3).

- [ ] **Step 1: Modify `src/config.py` to add three settings**

Append inside the `Config` class (after `CALL_TTL`):

```python
    # SIP device credentials (from RC device provisioning — see docs/WSL_SETUP.md)
    SIP_DOMAIN: str = os.environ.get("SIP_DOMAIN", "sip.ringcentral.com")
    SIP_USERNAME: str = os.environ.get("SIP_USERNAME", "")
    SIP_PASSWORD: str = os.environ.get("SIP_PASSWORD", "")
    SIP_AUTH_ID: str = os.environ.get("SIP_AUTH_ID", "")
    SIP_DEVICE_ID: str = os.environ.get("SIP_DEVICE_ID", "")

    # Comma-separated list of extension IDs whose calls should be transcribed.
    # Empty string = monitor all.
    MONITORED_EXTENSIONS: list[str] = [
        e.strip() for e in os.environ.get("MONITORED_EXTENSIONS", "").split(",") if e.strip()
    ]
```

- [ ] **Step 2: Update `.env.example`**

Append to the bottom of `.env.example`:

```env
# SIP device credentials for Supervision API (see docs/WSL_SETUP.md to provision)
SIP_DOMAIN=sip.ringcentral.com
SIP_USERNAME=
SIP_PASSWORD=
SIP_AUTH_ID=
SIP_DEVICE_ID=

# Comma-separated extension IDs to monitor. Leave empty to monitor all.
MONITORED_EXTENSIONS=
```

- [ ] **Step 3: Commit**

```bash
git add src/config.py .env.example
git commit -m "feat: add SIP and monitored-extensions config for Phase 2"
```

---

### Task 3: Supervision API Client

**Files:**
- Create: `src/supervision.py`
- Create: `tests/test_supervision.py`

The Supervision API is a single REST endpoint: `POST /restapi/v1.0/account/~/telephony/sessions/{sessionId}/supervise`. Given a live session ID and our device ID, it inserts us as a silent listening party. This task wraps that call and its error handling.

- [ ] **Step 1: Write failing test for `start_supervision`**

File: `tests/test_supervision.py`

```python
import pytest
from unittest.mock import MagicMock
from src.supervision import start_supervision, SupervisionError


def test_start_supervision_posts_correct_payload():
    platform = MagicMock()
    resp = MagicMock()
    resp.json_dict.return_value = {"id": "party-abc", "status": {"code": "Setup"}}
    platform.post.return_value = resp

    result = start_supervision(
        platform,
        session_id="s-123",
        device_id="dev-xyz",
        agent_extension_id="119",
    )

    platform.post.assert_called_once()
    call_args = platform.post.call_args
    assert "/supervise" in call_args[0][0]
    assert "s-123" in call_args[0][0]
    body = call_args.kwargs.get("body") or call_args[0][1]
    assert body["mode"] == "Listen"
    assert body["supervisorDeviceId"] == "dev-xyz"
    assert body["agentExtensionId"] == "119"
    assert result["id"] == "party-abc"


def test_start_supervision_raises_on_error():
    platform = MagicMock()
    platform.post.side_effect = Exception("403 Forbidden")

    with pytest.raises(SupervisionError):
        start_supervision(platform, "s-123", "dev-xyz", "119")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_supervision.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `start_supervision`**

File: `src/supervision.py`

```python
import logging

logger = logging.getLogger(__name__)


class SupervisionError(Exception):
    """Raised when the RC Supervision API rejects our request."""


def start_supervision(platform, session_id: str, device_id: str,
                      agent_extension_id: str) -> dict:
    """Silently join a telephony session as a listener.

    Args:
        platform: an authenticated ringcentral SDK platform instance
        session_id: the telephonySessionId of the call to supervise
        device_id: our SIP device ID (SIP_DEVICE_ID in Config)
        agent_extension_id: the extensionId of the agent whose audio we want

    Returns the new party record on success. Raises SupervisionError on failure.
    """
    url = f"/restapi/v1.0/account/~/telephony/sessions/{session_id}/supervise"
    body = {
        "mode": "Listen",
        "supervisorDeviceId": device_id,
        "agentExtensionId": agent_extension_id,
    }
    try:
        resp = platform.post(url, body=body)
        return resp.json_dict()
    except Exception as e:
        logger.exception("Supervision API failed for session %s", session_id)
        raise SupervisionError(str(e)) from e
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_supervision.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/supervision.py tests/test_supervision.py
git commit -m "feat: RC Supervision API client for silent call monitoring"
```

---

### Task 4: Deepgram Realtime WebSocket Client

**Files:**
- Create: `src/deepgram_stream.py`
- Create: `tests/test_deepgram_stream.py`

Deepgram's real-time API is a WebSocket. You open it with your API key + model params, send binary audio frames (e.g., 20ms PCM chunks), and receive JSON messages with interim + final transcript chunks. This class manages the connection and exposes `send_audio()` and an async iterator of transcript strings.

- [ ] **Step 1: Write failing test**

File: `tests/test_deepgram_stream.py`

```python
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch
from src.deepgram_stream import DeepgramStream


@pytest.mark.asyncio
async def test_stream_yields_finalized_transcripts():
    messages = [
        json.dumps({
            "channel": {"alternatives": [{"transcript": "Hello there"}]},
            "is_final": True,
        }),
        json.dumps({
            "channel": {"alternatives": [{"transcript": "partial"}]},
            "is_final": False,
        }),
        json.dumps({
            "channel": {"alternatives": [{"transcript": "How can I help"}]},
            "is_final": True,
        }),
    ]

    fake_ws = AsyncMock()
    fake_ws.__aiter__.return_value = iter(messages)

    with patch("src.deepgram_stream.websockets.connect", AsyncMock(return_value=fake_ws)):
        stream = DeepgramStream(api_key="test")
        await stream.open()
        finals = [t async for t in stream.transcripts()]

    assert finals == ["Hello there", "How can I help"]


@pytest.mark.asyncio
async def test_send_audio_writes_to_ws():
    fake_ws = AsyncMock()
    fake_ws.__aiter__.return_value = iter([])

    with patch("src.deepgram_stream.websockets.connect", AsyncMock(return_value=fake_ws)):
        stream = DeepgramStream(api_key="test")
        await stream.open()
        await stream.send_audio(b"\x00\x01\x02\x03")

    fake_ws.send.assert_awaited_once_with(b"\x00\x01\x02\x03")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_deepgram_stream.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `DeepgramStream`**

File: `src/deepgram_stream.py`

```python
import logging
import json
from urllib.parse import urlencode

import websockets

logger = logging.getLogger(__name__)

DEEPGRAM_WS = "wss://api.deepgram.com/v1/listen"


class DeepgramStream:
    """Real-time speech-to-text via Deepgram WebSocket.

    Usage:
        stream = DeepgramStream(api_key=...)
        await stream.open()
        # Feed audio in one coroutine:
        await stream.send_audio(pcm_bytes)
        # Consume transcripts in another:
        async for line in stream.transcripts():
            print(line)
        await stream.close()
    """

    def __init__(self, api_key: str, sample_rate: int = 8000,
                 encoding: str = "mulaw", model: str = "nova-3"):
        self.api_key = api_key
        self.sample_rate = sample_rate
        self.encoding = encoding
        self.model = model
        self._ws = None

    async def open(self) -> None:
        params = urlencode({
            "model": self.model,
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
        })
        url = f"{DEEPGRAM_WS}?{params}"
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {self.api_key}"},
        )
        logger.info("Deepgram WS connected")

    async def send_audio(self, audio_bytes: bytes) -> None:
        if self._ws is None:
            raise RuntimeError("open() must be called before send_audio()")
        await self._ws.send(audio_bytes)

    async def transcripts(self):
        """Async generator yielding finalized transcript strings."""
        if self._ws is None:
            raise RuntimeError("open() must be called before transcripts()")
        async for msg in self._ws:
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if not data.get("is_final"):
                continue
            alts = data.get("channel", {}).get("alternatives", [])
            if not alts:
                continue
            text = alts[0].get("transcript", "").strip()
            if text:
                yield text

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
            logger.info("Deepgram WS closed")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_deepgram_stream.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/deepgram_stream.py tests/test_deepgram_stream.py
git commit -m "feat: Deepgram real-time WebSocket client for live STT"
```

---

### Task 5: SIP Monitor (pjsua2 wrapper) — registration only

**Files:**
- Create: `src/sip_monitor.py`

This task only gets SIP registration working. Audio capture and the Supervision flow integration come in Task 6. Splitting keeps each piece independently verifiable.

`pjsua2` doesn't lend itself to unit testing (it's a C++ wrapper that opens real UDP sockets and talks to a real SIP server). We verify by running against live RC.

- [ ] **Step 1: Create `src/sip_monitor.py` with registration-only logic**

```python
"""pjsua2-based SIP client that registers with RC and accepts incoming supervision calls.

Must be run inside WSL or Linux — pjsua2 doesn't install cleanly on Windows.
"""
import logging
import threading
import time

import pjsua2 as pj

from src.config import Config

logger = logging.getLogger(__name__)


class SipMonitor:
    """Maintains a SIP registration with RC and dispatches incoming supervise calls."""

    def __init__(self, on_incoming_call=None):
        self.on_incoming_call = on_incoming_call
        self._ep: pj.Endpoint | None = None
        self._account: pj.Account | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        """Start the SIP endpoint in a background thread and block until registered."""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("SIP registration timed out")

    def _run(self) -> None:
        ep = pj.Endpoint()
        ep.libCreate()
        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.userAgent = "sfw-call-bridge/1.0"
        ep.libInit(ep_cfg)

        transport_cfg = pj.TransportConfig()
        transport_cfg.port = 5060
        ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, transport_cfg)
        ep.libStart()
        self._ep = ep

        acc_cfg = pj.AccountConfig()
        acc_cfg.idUri = f"sip:{Config.SIP_USERNAME}@{Config.SIP_DOMAIN}"
        acc_cfg.regConfig.registrarUri = f"sip:{Config.SIP_DOMAIN}"
        cred = pj.AuthCredInfo("digest", "*", Config.SIP_AUTH_ID, 0, Config.SIP_PASSWORD)
        acc_cfg.sipConfig.authCreds.append(cred)

        self._account = _MonitorAccount(self.on_incoming_call, self._ready)
        self._account.create(acc_cfg)
        logger.info("SIP account created, awaiting registration...")

        # Keep the thread alive running pjsua2's event loop
        while True:
            ep.libHandleEvents(100)

    def stop(self) -> None:
        if self._ep:
            self._ep.libDestroy()
            self._ep = None


class _MonitorAccount(pj.Account):
    """pjsua2 Account callback subclass — notified on registration and incoming calls."""

    def __init__(self, on_incoming_call, ready_event):
        super().__init__()
        self._on_incoming = on_incoming_call
        self._ready = ready_event

    def onRegState(self, prm):
        info = self.getInfo()
        logger.info("SIP reg state: %s (code=%s)", info.regIsActive, prm.code)
        if info.regIsActive:
            self._ready.set()

    def onIncomingCall(self, prm):
        logger.info("SIP incoming call: id=%s", prm.callId)
        if self._on_incoming:
            self._on_incoming(self, prm.callId)
```

- [ ] **Step 2: Run registration test in WSL**

You need provisioned SIP credentials in `.env` first (see `docs/WSL_SETUP.md` — this will become part of the guide in Task 7). For now, create a small manual test:

Temporary file `verify_sip_reg.py`:
```python
import logging
import time
from src.sip_monitor import SipMonitor

logging.basicConfig(level=logging.INFO)

monitor = SipMonitor()
monitor.start()
print("Registered successfully. Keeping alive for 10s.")
time.sleep(10)
monitor.stop()
```

Run:
```bash
cd ~/chat-rail && source .venv/bin/activate
python verify_sip_reg.py
```

Expected output includes:
```
SIP account created, awaiting registration...
SIP reg state: True (code=200)
Registered successfully. Keeping alive for 10s.
```

Then delete `verify_sip_reg.py`:
```bash
rm verify_sip_reg.py
```

- [ ] **Step 3: Commit**

```bash
git add src/sip_monitor.py
git commit -m "feat: SIP monitor with pjsua2 — registration only"
```

---

### Task 6: SIP Monitor — audio frame extraction

**Files:**
- Modify: `src/sip_monitor.py`

Extend the SIP monitor to auto-answer incoming calls (Supervision pushes them to us) and expose a callback that receives raw audio frames.

pjsua2 delivers audio through the `AudioMedia` abstraction. We create a custom `AudioMediaPort` that inherits from pjsua2's `AudioMedia`, receives frames in `onFrameRequested`/`onFrameReceived`, and forwards raw bytes to a user-provided callback.

- [ ] **Step 1: Add `_MonitorCall` and `_AudioSink` classes to `src/sip_monitor.py`**

Append to `src/sip_monitor.py`:

```python
class _AudioSink(pj.AudioMediaPort):
    """Custom audio sink that forwards frames to a Python callback."""

    def __init__(self, on_frame):
        super().__init__()
        self._on_frame = on_frame

    def onFrameReceived(self, frame):
        # frame.buf is a bytearray of PCM audio; forward to consumer
        try:
            self._on_frame(bytes(frame.buf))
        except Exception:
            logger.exception("Frame callback raised")


class _MonitorCall(pj.Call):
    """pjsua2 Call subclass that auto-answers and pipes audio to an _AudioSink."""

    def __init__(self, account, call_id, on_audio_frame):
        super().__init__(account, call_id)
        self._on_frame = on_audio_frame
        self._sink: _AudioSink | None = None

    def onCallState(self, prm):
        info = self.getInfo()
        logger.info("Call state: %s", info.stateText)

    def onCallMediaState(self, prm):
        info = self.getInfo()
        for mi in info.media:
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                # Create an audio sink port and connect the call's audio media to it
                fmt = pj.MediaFormatAudio()
                fmt.type = pj.PJMEDIA_TYPE_AUDIO
                fmt.clockRate = 8000
                fmt.channelCount = 1
                fmt.bitsPerSample = 16
                fmt.frameTimeUsec = 20000
                self._sink = _AudioSink(self._on_frame)
                self._sink.createPort("audio_sink", fmt)
                call_audio = self.getMedia(mi.index)
                pj.AudioMedia.typecastFromMedia(call_audio).startTransmit(self._sink)
                logger.info("Audio transmit started on this call")
```

- [ ] **Step 2: Modify `SipMonitor.__init__` to accept an audio callback**

Replace the `__init__` signature near the top of the file:

```python
    def __init__(self, on_audio_frame=None):
        self.on_audio_frame = on_audio_frame
        self._ep: pj.Endpoint | None = None
        self._account: pj.Account | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
```

And remove the now-unused `on_incoming_call` param — the call handling happens inside the account subclass.

- [ ] **Step 3: Modify `_MonitorAccount.onIncomingCall` to instantiate `_MonitorCall`**

Replace:

```python
    def __init__(self, on_audio_frame, ready_event):
        super().__init__()
        self._on_frame = on_audio_frame
        self._ready = ready_event

    def onRegState(self, prm):
        info = self.getInfo()
        logger.info("SIP reg state: %s (code=%s)", info.regIsActive, prm.code)
        if info.regIsActive:
            self._ready.set()

    def onIncomingCall(self, prm):
        logger.info("SIP incoming call: id=%s", prm.callId)
        call = _MonitorCall(self, prm.callId, self._on_frame)
        call_prm = pj.CallOpParam()
        call_prm.statusCode = 200  # auto-answer
        call.answer(call_prm)
```

And update `_run()` to pass `self.on_audio_frame` when creating `_MonitorAccount`:

```python
        self._account = _MonitorAccount(self.on_audio_frame, self._ready)
```

- [ ] **Step 4: Live verification in WSL**

Temporary file `verify_sip_audio.py`:
```python
import logging, time
from src.sip_monitor import SipMonitor

logging.basicConfig(level=logging.INFO)

total = 0
def on_frame(buf):
    global total
    total += len(buf)

monitor = SipMonitor(on_audio_frame=on_frame)
monitor.start()
print("Registered. Now trigger a supervised call via the Supervision API.")
print("Will run for 60s and print byte count every 5s.")
for i in range(12):
    time.sleep(5)
    print(f"  [{i*5+5}s] received {total} audio bytes total")
monitor.stop()
```

Run in WSL (with SIP credentials in `.env`). Trigger supervision via another terminal (Task 8 covers the full integration — for this test, use a Python REPL calling `start_supervision()` against a real active call).

Expected: once supervised, byte count grows; otherwise stays at 0. Delete the verify file after.

- [ ] **Step 5: Commit**

```bash
git add src/sip_monitor.py
git commit -m "feat: SIP monitor captures audio frames via pjsua2 AudioMediaPort"
```

---

### Task 7: Document SIP Device Provisioning

**Files:**
- Modify: `docs/WSL_SETUP.md`

We need a device ID and SIP credentials on ext 120 to register. RC provisions these via the /client-info/sip-provision endpoint. Document the one-time bootstrap.

- [ ] **Step 1: Append to `docs/WSL_SETUP.md`**

Add a new section:

```markdown
## 8. Provision SIP credentials for ext 120 (one-time)

RC issues SIP credentials to a specific device. Run this helper to provision ours:

```python
# provision_sip.py (delete after you copy the output to .env)
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

Copy the five lines it prints into your `.env`, then delete the script:
```bash
rm provision_sip.py
```

**Security note:** The password in `.env` is effectively a permanent credential. Treat it like the RC JWT.
```

- [ ] **Step 2: Commit**

```bash
git add docs/WSL_SETUP.md
git commit -m "docs: SIP device provisioning for Phase 2"
```

---

### Task 8: Live Transcriber Orchestrator

**Files:**
- Create: `src/live_transcriber.py`
- Create: `tests/test_live_transcriber.py`

Given a `telephonySessionId` and `agentExtensionId`, this orchestrator:
1. Opens a Deepgram stream
2. Calls Supervision API to pull the call's audio to our SIP monitor
3. Routes audio frames from the SIP monitor → Deepgram
4. Accumulates finalized transcripts into Redis for that session
5. Cleans up when the call ends

The SIP monitor is shared across all sessions (it runs one registration), so we need a dispatcher that maps incoming SIP calls → session IDs → Deepgram streams. For Phase 2 MVP, we support **one active supervision at a time** — simplest correct behavior, can be generalized in Phase 3.

- [ ] **Step 1: Write failing test for `LiveTranscriber` basic flow**

File: `tests/test_live_transcriber.py`

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.redis_store import CallStore
from src.live_transcriber import LiveTranscriber


@pytest.fixture
def store(fake_redis):
    return CallStore(fake_redis)


@pytest.mark.asyncio
async def test_start_session_opens_deepgram_and_calls_supervision(store):
    sip_monitor = MagicMock()
    platform = MagicMock()

    fake_stream = AsyncMock()
    fake_stream.open = AsyncMock()
    fake_stream.close = AsyncMock()

    async def no_transcripts():
        if False:
            yield

    fake_stream.transcripts = no_transcripts

    with patch("src.live_transcriber.DeepgramStream", return_value=fake_stream), \
         patch("src.live_transcriber.start_supervision") as start_sup:
        start_sup.return_value = {"id": "party-x"}

        lt = LiveTranscriber(store=store, sip_monitor=sip_monitor, platform=platform,
                             device_id="dev-1", deepgram_api_key="k")
        await lt.start_session("s-100", "119")

    fake_stream.open.assert_awaited()
    start_sup.assert_called_once_with(platform, "s-100", "dev-1", "119")
    assert lt._active_session == "s-100"


@pytest.mark.asyncio
async def test_transcript_chunks_appended_to_redis(store):
    sip_monitor = MagicMock()
    platform = MagicMock()

    fake_stream = AsyncMock()
    fake_stream.open = AsyncMock()
    fake_stream.close = AsyncMock()

    async def gen():
        yield "Hi this is Doug"
        yield "from SFW Construction"

    fake_stream.transcripts = gen

    with patch("src.live_transcriber.DeepgramStream", return_value=fake_stream), \
         patch("src.live_transcriber.start_supervision", return_value={"id": "party-x"}):
        lt = LiveTranscriber(store=store, sip_monitor=sip_monitor, platform=platform,
                             device_id="dev-1", deepgram_api_key="k")
        await lt.start_session("s-100", "119")
        # Let the consumer task run
        await asyncio.sleep(0.05)
        await lt.stop_session("s-100")

    transcript = store.get_transcript("s-100")
    assert transcript is not None
    assert "Doug" in transcript
    assert "SFW Construction" in transcript
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_live_transcriber.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `LiveTranscriber`**

File: `src/live_transcriber.py`

```python
import asyncio
import logging

from src.deepgram_stream import DeepgramStream
from src.redis_store import CallStore
from src.supervision import start_supervision, SupervisionError

logger = logging.getLogger(__name__)


class LiveTranscriber:
    """Orchestrates Supervision API + SIP audio + Deepgram + Redis for one call at a time.

    Phase 2 MVP: single active session. Extend to a dict of sessions for Phase 3.
    """

    def __init__(self, store: CallStore, sip_monitor, platform,
                 device_id: str, deepgram_api_key: str):
        self.store = store
        self.sip_monitor = sip_monitor
        self.platform = platform
        self.device_id = device_id
        self.deepgram_api_key = deepgram_api_key

        self._active_session: str | None = None
        self._stream: DeepgramStream | None = None
        self._consumer_task: asyncio.Task | None = None

    async def start_session(self, session_id: str, agent_extension_id: str) -> None:
        if self._active_session is not None:
            logger.warning("Already monitoring %s, skipping %s",
                           self._active_session, session_id)
            return

        logger.info("Starting live transcription for session %s", session_id)
        self._stream = DeepgramStream(api_key=self.deepgram_api_key)
        await self._stream.open()

        try:
            start_supervision(self.platform, session_id, self.device_id, agent_extension_id)
        except SupervisionError:
            logger.error("Supervision failed for %s — aborting", session_id)
            await self._stream.close()
            self._stream = None
            return

        self._active_session = session_id
        # Wire SIP audio frames into Deepgram
        self.sip_monitor.on_audio_frame = self._audio_callback
        self._consumer_task = asyncio.create_task(self._consume_transcripts(session_id))

    def _audio_callback(self, frame: bytes) -> None:
        """Called from the SIP thread on each audio frame. Schedules send on event loop."""
        if self._stream is None:
            return
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(self._stream.send_audio(frame), loop)

    async def _consume_transcripts(self, session_id: str) -> None:
        assert self._stream is not None
        accumulated = self.store.get_transcript(session_id) or ""
        try:
            async for chunk in self._stream.transcripts():
                accumulated = (accumulated + " " + chunk).strip() if accumulated else chunk
                self.store.store_transcript(session_id, accumulated)
                logger.info("Transcript chunk for %s: %s", session_id, chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Transcript consumer error")

    async def stop_session(self, session_id: str) -> None:
        if self._active_session != session_id:
            return
        logger.info("Stopping live transcription for session %s", session_id)
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None
        if self._stream:
            await self._stream.close()
            self._stream = None
        self.sip_monitor.on_audio_frame = None
        self._active_session = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_live_transcriber.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/live_transcriber.py tests/test_live_transcriber.py
git commit -m "feat: live transcriber orchestrator — Supervision + SIP + Deepgram + Redis"
```

---

### Task 9: Wire Call Monitor to Live Transcriber

**Files:**
- Modify: `src/call_monitor.py`
- Modify: `tests/test_call_monitor.py`

When the Phase 1 call monitor sees `status=Answered` and the call's rep extension is in `MONITORED_EXTENSIONS`, it should invoke `LiveTranscriber.start_session()`. When the call disconnects, `stop_session()`.

Pass the transcriber as an optional parameter so all existing tests keep working.

- [ ] **Step 1: Add test for transcriber invocation on Answered**

Append to `tests/test_call_monitor.py`. These tests are async because `process_telephony_event` schedules transcriber calls via `asyncio.create_task`, which requires a running event loop.

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_answered_call_triggers_transcriber_when_ext_is_monitored(store):
    transcriber = MagicMock()
    transcriber.start_session = AsyncMock()

    event = make_event("s-200", "Answered",
                       to_info={"extensionId": "119", "name": "Doug Stoker"})
    process_telephony_event(event, store, transcriber=transcriber,
                            monitored_extensions=["119"])
    # Let the scheduled task run
    await asyncio.sleep(0)

    transcriber.start_session.assert_awaited_once_with("s-200", "119")


@pytest.mark.asyncio
async def test_answered_call_skipped_when_ext_not_monitored(store):
    transcriber = MagicMock()
    transcriber.start_session = AsyncMock()

    event = make_event("s-200", "Answered",
                       to_info={"extensionId": "999", "name": "Other"})
    process_telephony_event(event, store, transcriber=transcriber,
                            monitored_extensions=["119"])
    await asyncio.sleep(0)

    transcriber.start_session.assert_not_called()


@pytest.mark.asyncio
async def test_disconnected_call_stops_transcriber(store):
    transcriber = MagicMock()
    transcriber.start_session = AsyncMock()
    transcriber.stop_session = AsyncMock()

    process_telephony_event(make_event("s-200", "Answered",
                                       to_info={"extensionId": "119"}),
                            store, transcriber=transcriber,
                            monitored_extensions=["119"])
    await asyncio.sleep(0)
    process_telephony_event(make_event("s-200", "Disconnected",
                                       to_info={"extensionId": "119"}),
                            store, transcriber=transcriber,
                            monitored_extensions=["119"])
    await asyncio.sleep(0)

    transcriber.stop_session.assert_awaited_once_with("s-200")
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `pytest tests/test_call_monitor.py -v`
Expected: FAIL on the three new tests — `process_telephony_event() got an unexpected keyword argument 'transcriber'`

- [ ] **Step 3: Modify `process_telephony_event` in `src/call_monitor.py`**

Replace the full function:

```python
def process_telephony_event(event: dict, store: CallStore,
                            transcriber=None, monitored_extensions=None) -> None:
    """Process a single RC telephony session notification and update store.

    If `transcriber` is provided and the call is on a monitored extension,
    invoke live transcription on Answered / teardown on Disconnected.
    """
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
    ext_id = to_info.get("extensionId") or from_info.get("extensionId")

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
        if transcriber and ext_id and _is_monitored(ext_id, monitored_extensions):
            asyncio.create_task(transcriber.stop_session(session_id))
    else:
        logger.info("Call event: %s (status=%s)", session_id, status)
        store.store_call(session_id, call_data)
        if (transcriber and status == "Answered" and ext_id
                and _is_monitored(ext_id, monitored_extensions)):
            asyncio.create_task(transcriber.start_session(session_id, ext_id))


def _is_monitored(ext_id: str, monitored: list[str] | None) -> bool:
    """An empty / None monitored list means 'monitor everything'."""
    if not monitored:
        return True
    return ext_id in monitored
```

- [ ] **Step 4: Modify `on_message` inside `_run_ws_session` to pass the new args**

Find the `process_telephony_event(event, store)` call inside `on_message` and replace with:

```python
        process_telephony_event(event, store,
                                transcriber=transcriber,
                                monitored_extensions=Config.MONITORED_EXTENSIONS)
```

Then modify `run_monitor` and `_run_ws_session` to accept/forward a `transcriber` parameter:

```python
async def run_monitor(store: CallStore, transcriber=None) -> None:
```
(pass through to `_run_ws_session(sdk, event_filters, store, transcriber)`)

```python
async def _run_ws_session(sdk, event_filters, store: CallStore, transcriber=None) -> None:
```
(capture `transcriber` in `on_message`'s closure)

- [ ] **Step 5: Run all tests to confirm everything passes**

Run: `pytest -v`
Expected: all existing tests + 3 new ones PASS

- [ ] **Step 6: Commit**

```bash
git add src/call_monitor.py tests/test_call_monitor.py
git commit -m "feat: call monitor triggers live transcriber on Answered for monitored extensions"
```

---

### Task 10: Live Entrypoint for WSL/Linux

**Files:**
- Create: `run_live.py`

Replacement for `run_local.py` when running on Linux. Wires everything together: FastAPI + call monitor + SIP monitor + live transcriber.

- [ ] **Step 1: Create `run_live.py`**

```python
"""Linux/WSL entrypoint — runs API, call monitor, SIP monitor, and live transcription.

Not used on Windows (pjsua2 won't install cleanly). Phase 2 requires this on Linux.
"""
import asyncio
import logging

import fakeredis
import uvicorn
from ringcentral import SDK

from src.api.main import create_app
from src.call_monitor import run_monitor
from src.config import Config
from src.live_transcriber import LiveTranscriber
from src.redis_store import CallStore
from src.sip_monitor import SipMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    store = CallStore(fakeredis.FakeRedis(decode_responses=True))
    app = create_app(store)

    # SIP registration (blocks until registered, runs pjsua2 event loop in its own thread)
    logger.info("Starting SIP monitor...")
    sip = SipMonitor()
    sip.start()
    logger.info("SIP registered; ready to accept supervise calls")

    # Dedicated RC platform for supervision API calls
    sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET, Config.RC_SERVER)
    platform = sdk.platform()
    platform.login(jwt=Config.RC_JWT)

    transcriber = LiveTranscriber(
        store=store,
        sip_monitor=sip,
        platform=platform,
        device_id=Config.SIP_DEVICE_ID,
        deepgram_api_key=Config.DEEPGRAM_API_KEY,
    )

    uvicorn_cfg = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(uvicorn_cfg)

    logger.info("API on http://localhost:8000; monitoring extensions: %s",
                Config.MONITORED_EXTENSIONS or "ALL")
    await asyncio.gather(server.serve(), run_monitor(store, transcriber=transcriber))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
```

- [ ] **Step 2: Commit**

```bash
git add run_live.py
git commit -m "feat: run_live.py entrypoint with SIP + live transcription (Linux/WSL only)"
```

---

### Task 11: End-to-End Smoke Test

This is manual — exercises the full Phase 2 pipeline against real RC and Deepgram.

- [ ] **Step 1: Full test suite**

```bash
cd ~/chat-rail && source .venv/bin/activate
pytest -v
```

Expected: all tests pass (Phase 1's 31 + Phase 2's 5 ≈ 36+).

- [ ] **Step 2: Populate `.env` with all credentials**

Confirm `.env` has:
- All Phase 1 RC vars
- `DEEPGRAM_API_KEY`
- `CALL_BRIDGE_API_KEY`
- `SIP_DOMAIN`, `SIP_USERNAME`, `SIP_PASSWORD`, `SIP_AUTH_ID`, `SIP_DEVICE_ID` (from Task 7 provisioning)
- `MONITORED_EXTENSIONS` — at minimum one rep's extension ID for the test

- [ ] **Step 3: Start `run_live.py`**

```bash
python run_live.py
```

Expected log sequence:
```
Starting SIP monitor...
SIP reg state: True (code=200)
SIP registered; ready to accept supervise calls
API on http://localhost:8000; monitoring extensions: [...]
Authenticated with RingCentral
Subscription active — listening for call events
```

- [ ] **Step 4: Make a test call**

Have a monitored rep answer a call. Watch for log lines:
```
Call event: s-XXX (status=Answered)
Starting live transcription for session s-XXX
Deepgram WS connected
SIP incoming call: id=...
Audio transmit started on this call
Transcript chunk for s-XXX: Hi this is ...
Transcript chunk for s-XXX: how can I help
```

- [ ] **Step 5: Query the API during the call**

```bash
curl -H "x-api-key: $CALL_BRIDGE_API_KEY" \
    http://localhost:8000/api/calls/latest?rep=<MONITORED_EXT>
```

Expected: the response's `transcript` field contains what's been said so far in the call.

- [ ] **Step 6: Hang up and verify cleanup**

Log should show:
```
Call ended: s-XXX
Stopping live transcription for session s-XXX
Deepgram WS closed
```

- [ ] **Step 7: Commit any fixes from the smoke test**

```bash
git add -A
git commit -m "fix: Phase 2 adjustments from end-to-end smoke test"
```
(Only if changes were needed.)

---

## Known Risks & Mitigations

1. **pjsua2 build may not work on the first try** — common issues: missing libasound2-dev, incorrect Python path when running `setup.py install`. If Task 1's setup guide breaks, the fallback is prebuilt Docker images (`andrius/asterisk` family) or `baresip` — but we keep `pjsua2` as the default because it's the reference Python/SIP tool.

2. **Supervision API may reject our first call** — most common cause is ext 120 not being a monitor in the Sales group (user confirmed ✅). Second most common: `supervisorDeviceId` mismatch — make sure you use the device ID from Task 7's provisioning. If you see "403 Not enough permissions," re-check the monitor group membership in RC admin.

3. **Deepgram may require PCM mu-law format while pjsua2 delivers PCM 16-bit linear** — if transcripts come back empty or garbled, the audio format is the likely culprit. Check `_AudioSink`'s format config in Task 6 and Deepgram's `encoding` param in Task 4 — both must agree. Currently set to 8kHz 16-bit linear on both sides.

4. **Single-session limit (Phase 2 MVP)** — if two calls are answered simultaneously on monitored extensions, the second one is skipped. Phase 3 will generalize to N concurrent sessions.
