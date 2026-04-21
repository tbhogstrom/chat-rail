# Phase 2: Live Call Transcripts Implementation Plan

> **Rev 2 — 2026-04-21.** This supersedes Rev 1 (pjsua2 + WSL), which was invalidated by RingCentral support feedback on 2026-04-21 (support case 31250629).
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture live audio from active RingCentral calls, stream it to Deepgram for real-time speech-to-text, and accumulate transcripts in Redis so the GPT Action API can return the live conversation to ChatGPT as calls happen.

**Architecture:** A Node/TypeScript **softphone sidecar** process holds a SIP registration via RingCentral's official [`ringcentral-softphone-ts`](https://github.com/ringcentral/ringcentral-softphone-ts) SDK. When the existing Python call monitor sees `status=Answered` on a monitored extension, it POSTs `{sessionId, agentExtNumber}` to the sidecar. The sidecar dials `*80` (RC's in-call monitoring feature code), sends the agent's extension number as DTMF, and receives RTP audio packets via the SDK's `audioPacket` event. Audio frames stream to Deepgram's real-time WebSocket. Finalized transcript chunks append to `call:{sessionId}:transcript` in Redis — the same key the existing FastAPI endpoints already read.

**Tech stack:** Node 20+, TypeScript, `ringcentral-softphone` (npm), `@deepgram/sdk`, `ioredis` / upstash-redis client, Fastify (tiny HTTP surface for intake). Python stack (Phase 1) changes minimally.

**Why this shape (vs. Rev 1):**
- RC told us `/client-info/sip-provision` cannot make a SoftPhone-type device (only WebPhone). Rev 1's provisioning path was a dead end.
- RC told us pjsua2 is out of support scope and to use their official TS SDK instead. Rev 1's entire SIP stack is now unsupported.
- Tyler Liu's [rc-softphone-monitor-demo](https://github.com/tylerlong/rc-softphone-monitor-demo) shows supervision is `softphone.call("*80")` + DTMF — no REST `/supervise` call needed.
- TS SDK runs on Node anywhere (incl. Windows native) — **WSL is no longer required**.

---

## Prerequisites (User must complete before Task 1)

1. **RC admin:** Ext 120 (Tyler Falcon) is a monitor in the "Sales" call monitoring group. ✅ **DONE**
2. **Verify device `677386052` is type `OtherPhone`** (Task 0 below). If not, create a new device of type "Existing Phone" in the RC admin portal and use that device ID instead.
3. **Node.js 20+** installed on whatever box will run the sidecar. Windows, macOS, or Linux all fine.

**Obsoleted from Rev 1:**
- ❌ WSL install — no longer needed. `docs/WSL_SETUP.md` should be deleted in Task 1.
- ❌ pjsua2 build from source — SDK is pure npm install.
- ❌ `/client-info/sip-provision` script — use `readDeviceSipInfo` on an existing device instead.

---

## File Structure

```
callrail-chatgpt/
├── softphone-bridge/                     # NEW — Node/TS sidecar
│   ├── package.json
│   ├── tsconfig.json
│   ├── .env.example
│   └── src/
│       ├── index.ts                      # entrypoint: registers softphone, starts HTTP
│       ├── server.ts                     # Fastify app: POST /sessions, DELETE /sessions/:id
│       ├── supervisor.ts                 # per-session: *80 → DTMF → audio → Deepgram → Redis
│       ├── deepgram.ts                   # Deepgram live client wrapper
│       ├── redis.ts                      # same key schema as Python CallStore
│       └── config.ts                     # env var parsing
├── src/
│   ├── call_monitor.py                   # MODIFY: capture extensionNumber, POST to sidecar
│   ├── config.py                         # MODIFY: add SOFTPHONE_BRIDGE_URL, MONITORED_EXTENSIONS
│   └── sidecar_client.py                 # NEW — thin httpx wrapper for sidecar calls
├── tests/
│   ├── test_call_monitor.py              # MODIFY: sidecar invocation tests
│   └── test_sidecar_client.py            # NEW
└── scripts/
    └── get_device_sip_info.py            # NEW — one-time: list devices, fetch SIP creds
```

Gone from Rev 1: `src/supervision.py`, `src/sip_monitor.py`, `src/deepgram_stream.py`, `src/live_transcriber.py`, `run_live.py`, `docs/WSL_SETUP.md`.

---

### Task 0: Verify device + pull SIP credentials (one-time)

**Files:** Create `scripts/get_device_sip_info.py`

Tyler's `readDeviceSipInfo` path requires a device of type `OtherPhone` (API type) / "Existing Phone" (GUI label). Our existing device `677386052` is probably already this type — confirm, and fetch SIP credentials.

- [ ] **Step 1: Create `scripts/get_device_sip_info.py`**

```python
"""One-time: list devices under ext 120, confirm type, and pull SIP credentials.

Usage:
    python scripts/get_device_sip_info.py [--extension-id 120]
"""
import argparse, json, os
from dotenv import load_dotenv
from ringcentral import SDK

load_dotenv()

ap = argparse.ArgumentParser()
ap.add_argument("--extension-id", default="~", help="Extension ID (default: authed user)")
args = ap.parse_args()

sdk = SDK(os.environ["RC_CLIENT_ID"], os.environ["RC_CLIENT_SECRET"], os.environ["RC_SERVER"])
platform = sdk.platform()
platform.login(jwt=os.environ["RC_JWT"])

# 1. List devices
devices = platform.get(
    f"/restapi/v1.0/account/~/extension/{args.extension_id}/device"
).json_dict()

print("\n=== Devices on this extension ===")
usable = []
for d in devices["records"]:
    print(f"  id={d['id']}  type={d['type']}  name={d.get('name','')}  "
          f"computerName={d.get('computerName','')}")
    if d["type"] == "OtherPhone":
        usable.append(d)

if not usable:
    print("\n❌ No device of type 'OtherPhone' found.")
    print("   Go to service.ringcentral.com → user → Devices & Numbers → add an")
    print("   'Existing Phone' device, then re-run this script.")
    raise SystemExit(1)

device = usable[0]
print(f"\n✅ Using device {device['id']} ({device.get('name','')})")

# 2. Get SIP info
sip_info = platform.get(
    f"/restapi/v1.0/account/~/device/{device['id']}/sip-info"
).json_dict()

# Pick NA proxyTLS (sip20.ringcentral.com:5096 typically)
proxy_tls = None
for p in sip_info.get("outboundProxies", []):
    if p.get("region") == "NA":
        proxy_tls = p["proxyTLS"]
        break
if not proxy_tls:
    proxy_tls = sip_info["outboundProxies"][0]["proxyTLS"]

print("\n=== Add to softphone-bridge/.env ===\n")
print(f"SIP_INFO_DOMAIN={sip_info['domain']}")
print(f"SIP_INFO_OUTBOUND_PROXY={proxy_tls}")
print(f"SIP_INFO_USERNAME={sip_info['userName']}")
print(f"SIP_INFO_PASSWORD={sip_info['password']}")
print(f"SIP_INFO_AUTHORIZATION_ID={sip_info['authorizationId']}")
print(f"RC_DEVICE_ID={device['id']}  # reference only")
```

- [ ] **Step 2: Run it and capture output**

```bash
python scripts/get_device_sip_info.py --extension-id 120
```

**Pass criteria:** the script prints five `SIP_INFO_*` lines. Save them for Task 2.
**Fail case:** no `OtherPhone` device — follow the GUI instructions the script prints, then re-run. Do not proceed.

- [ ] **Step 3: Commit the script (not the credentials)**

```bash
git add scripts/get_device_sip_info.py
git commit -m "feat: one-time device/SIP-info fetcher for softphone sidecar"
```

---

### Task 1: Scaffold the sidecar project

**Files:** Create `softphone-bridge/{package.json, tsconfig.json, .env.example, src/config.ts, src/index.ts}`; delete `docs/WSL_SETUP.md`.

- [ ] **Step 1: Create `softphone-bridge/package.json`**

```json
{
  "name": "sfw-softphone-bridge",
  "version": "0.1.0",
  "description": "RC softphone sidecar — supervises calls and streams audio to Deepgram",
  "type": "module",
  "private": true,
  "scripts": {
    "dev": "tsx watch src/index.ts",
    "build": "tsc",
    "start": "node dist/index.js",
    "typecheck": "tsc --noEmit",
    "test": "vitest run"
  },
  "dependencies": {
    "@deepgram/sdk": "^3.9.0",
    "@upstash/redis": "^1.34.0",
    "dotenv": "^16.4.5",
    "fastify": "^5.0.0",
    "ringcentral-softphone": "^2.0.0"
  },
  "devDependencies": {
    "@types/node": "^22.0.0",
    "tsx": "^4.19.0",
    "typescript": "^5.6.0",
    "vitest": "^2.1.0"
  }
}
```

- [ ] **Step 2: Create `softphone-bridge/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true
  },
  "include": ["src/**/*"]
}
```

- [ ] **Step 3: Create `softphone-bridge/.env.example`**

```env
# From scripts/get_device_sip_info.py
SIP_INFO_DOMAIN=
SIP_INFO_OUTBOUND_PROXY=
SIP_INFO_USERNAME=
SIP_INFO_PASSWORD=
SIP_INFO_AUTHORIZATION_ID=

# Deepgram
DEEPGRAM_API_KEY=

# Redis (share with Python stack — same Vercel KV / Upstash instance)
KV_REST_API_URL=
KV_REST_API_TOKEN=

# Sidecar's own HTTP listener (Python posts here)
BRIDGE_PORT=8787
BRIDGE_API_KEY=   # shared secret with Python stack
```

- [ ] **Step 4: Create `softphone-bridge/src/config.ts`**

```typescript
import "dotenv/config";

function required(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}

export const config = {
  sip: {
    domain: required("SIP_INFO_DOMAIN"),
    outboundProxy: required("SIP_INFO_OUTBOUND_PROXY"),
    username: required("SIP_INFO_USERNAME"),
    password: required("SIP_INFO_PASSWORD"),
    authorizationId: required("SIP_INFO_AUTHORIZATION_ID"),
  },
  deepgramKey: required("DEEPGRAM_API_KEY"),
  redis: {
    url: required("KV_REST_API_URL"),
    token: required("KV_REST_API_TOKEN"),
  },
  bridge: {
    port: Number(process.env.BRIDGE_PORT ?? 8787),
    apiKey: required("BRIDGE_API_KEY"),
  },
};
```

- [ ] **Step 5: Create a minimal `softphone-bridge/src/index.ts` (registration-only smoke test)**

```typescript
import Softphone from "ringcentral-softphone";
import { config } from "./config.js";

const softphone = new Softphone({
  domain: config.sip.domain,
  outboundProxy: config.sip.outboundProxy,
  username: config.sip.username,
  password: config.sip.password,
  authorizationId: config.sip.authorizationId,
  codec: "PCMU/8000",
});
softphone.enableDebugMode();

await softphone.register();
console.log("[bridge] softphone registered");

// Keep process alive
process.stdin.resume();
```

- [ ] **Step 6: Install deps and run the smoke test**

```bash
cd softphone-bridge
npm install
cp .env.example .env
# fill in SIP_INFO_*, DEEPGRAM_API_KEY, KV_*, BRIDGE_API_KEY
npm run dev
```

**Pass criteria:** console shows SIP REGISTER → 200 OK (debug mode prints the SIP exchange). Process stays alive.

- [ ] **Step 7: Delete obsoleted WSL guide and commit scaffold**

```bash
git rm docs/WSL_SETUP.md
git add softphone-bridge/
git commit -m "feat: scaffold softphone-bridge TS sidecar; drop obsolete WSL guide"
```

---

### Task 2: Python config — sidecar URL + monitored extensions

**Files:** Modify `src/config.py`, `.env.example`

- [ ] **Step 1: Append to `Config` class in `src/config.py`**

```python
    # Softphone sidecar (Phase 2)
    SOFTPHONE_BRIDGE_URL: str = os.environ.get("SOFTPHONE_BRIDGE_URL", "")
    SOFTPHONE_BRIDGE_API_KEY: str = os.environ.get("SOFTPHONE_BRIDGE_API_KEY", "")

    # Extension IDs to live-transcribe. Empty = disabled.
    MONITORED_EXTENSIONS: list[str] = [
        e.strip() for e in os.environ.get("MONITORED_EXTENSIONS", "").split(",") if e.strip()
    ]
```

- [ ] **Step 2: Append to root `.env.example`**

```env
# Phase 2 — softphone sidecar
SOFTPHONE_BRIDGE_URL=http://localhost:8787
SOFTPHONE_BRIDGE_API_KEY=
MONITORED_EXTENSIONS=
```

- [ ] **Step 3: Commit**

```bash
git add src/config.py .env.example
git commit -m "feat: add softphone-bridge config for Phase 2"
```

---

### Task 3: Sidecar — Redis client mirroring the Python key schema

**Files:** Create `softphone-bridge/src/redis.ts`

The sidecar writes to the same Redis keys the Python `CallStore` already owns. Exact schema lives in `src/redis_store.py`.

- [ ] **Step 1: Create `softphone-bridge/src/redis.ts`**

```typescript
import { Redis } from "@upstash/redis";
import { config } from "./config.js";

const redis = new Redis({ url: config.redis.url, token: config.redis.token });

const CALL_TTL_SECONDS = 3600;

export async function appendTranscript(sessionId: string, chunk: string): Promise<void> {
  const key = `call:${sessionId}:transcript`;
  const existing = (await redis.get<string>(key)) ?? "";
  const next = existing ? `${existing} ${chunk}`.trim() : chunk;
  await redis.set(key, next, { ex: CALL_TTL_SECONDS });
}

export async function clearTranscript(sessionId: string): Promise<void> {
  await redis.del(`call:${sessionId}:transcript`);
}
```

Note: we do not write `call:{id}:state` or manipulate `calls:active` — Python still owns those.

- [ ] **Step 2: Commit**

```bash
git add softphone-bridge/src/redis.ts
git commit -m "feat(bridge): Redis transcript writer matching Python CallStore schema"
```

---

### Task 4: Sidecar — Deepgram live client wrapper

**Files:** Create `softphone-bridge/src/deepgram.ts`

One connection per supervised session. Sends mu-law 8 kHz RTP payload bytes; emits finalized transcript chunks.

- [ ] **Step 1: Create `softphone-bridge/src/deepgram.ts`**

```typescript
import { createClient, LiveTranscriptionEvents } from "@deepgram/sdk";
import { config } from "./config.js";

export interface DeepgramSession {
  sendAudio(payload: Buffer): void;
  close(): Promise<void>;
}

export function openDeepgram(opts: {
  onFinal: (text: string) => void;
  onError?: (err: Error) => void;
}): DeepgramSession {
  const dg = createClient(config.deepgramKey);
  const live = dg.listen.live({
    model: "nova-3",
    encoding: "mulaw",
    sample_rate: 8000,
    interim_results: true,
    smart_format: true,
    punctuate: true,
  });

  live.on(LiveTranscriptionEvents.Transcript, (msg) => {
    if (!msg.is_final) return;
    const text = msg.channel?.alternatives?.[0]?.transcript?.trim();
    if (text) opts.onFinal(text);
  });
  live.on(LiveTranscriptionEvents.Error, (err) => {
    opts.onError?.(err as Error);
  });

  return {
    sendAudio(payload) {
      live.send(payload);
    },
    async close() {
      live.finish();
    },
  };
}
```

- [ ] **Step 2: Commit**

```bash
git add softphone-bridge/src/deepgram.ts
git commit -m "feat(bridge): Deepgram live STT wrapper (mulaw/8k)"
```

---

### Task 5: Sidecar — per-session supervisor

**Files:** Create `softphone-bridge/src/supervisor.ts`

One instance per supervised call. Dials `*80`, sends `<agentExtNumber>#` on answer, pipes RTP payload to Deepgram, writes finals to Redis.

- [ ] **Step 1: Create `softphone-bridge/src/supervisor.ts`**

```typescript
import type Softphone from "ringcentral-softphone";
import { openDeepgram, DeepgramSession } from "./deepgram.js";
import { appendTranscript, clearTranscript } from "./redis.js";

export interface Supervisor {
  sessionId: string;
  stop(): Promise<void>;
}

export async function superviseCall(
  softphone: Softphone,
  sessionId: string,
  agentExtNumber: string,
): Promise<Supervisor> {
  console.log(`[sup:${sessionId}] starting — agent ext ${agentExtNumber}`);

  await clearTranscript(sessionId);

  const dg: DeepgramSession = openDeepgram({
    onFinal: (text) => {
      console.log(`[sup:${sessionId}] final: ${text}`);
      void appendTranscript(sessionId, text);
    },
    onError: (err) => console.error(`[sup:${sessionId}] deepgram error`, err),
  });

  // *80 is RC's in-call monitoring feature code. DTMF-in the agent extension number
  // with a trailing # to select which agent to listen to.
  const callSession = await softphone.call("*80");

  let stopped = false;
  const stop = async () => {
    if (stopped) return;
    stopped = true;
    try {
      await callSession.hangup();
    } catch (e) { /* may already be gone */ }
    await dg.close();
    console.log(`[sup:${sessionId}] stopped`);
  };

  callSession.once("answered", async () => {
    await callSession.sendDTMFs(`${agentExtNumber}#`, 500);
    console.log(`[sup:${sessionId}] monitoring active`);
  });

  callSession.on("audioPacket", (rtp: { payload: Buffer }) => {
    dg.sendAudio(rtp.payload);
  });

  callSession.once("disposed", () => {
    void stop();
  });

  callSession.once("busy", () => {
    console.warn(`[sup:${sessionId}] busy — supervision refused`);
    void stop();
  });

  return { sessionId, stop };
}
```

**Note on codec:** the sidecar is configured with `codec: "PCMU/8000"`. For PCMU the SDK emits raw mu-law RTP payload bytes (confirmed in SDK README) — which is exactly what Deepgram wants with `encoding=mulaw&sample_rate=8000`. No transcoding.

- [ ] **Step 2: Commit**

```bash
git add softphone-bridge/src/supervisor.ts
git commit -m "feat(bridge): per-session supervisor (*80 + DTMF + audio → Deepgram → Redis)"
```

---

### Task 6: Sidecar — HTTP intake server

**Files:** Create `softphone-bridge/src/server.ts`; replace `softphone-bridge/src/index.ts`

Python POSTs session start/stop to the sidecar. Fastify + shared API-key header.

- [ ] **Step 1: Create `softphone-bridge/src/server.ts`**

```typescript
import Fastify from "fastify";
import type Softphone from "ringcentral-softphone";
import { config } from "./config.js";
import { superviseCall, type Supervisor } from "./supervisor.js";

export function buildServer(softphone: Softphone) {
  const app = Fastify({ logger: true });
  const active = new Map<string, Supervisor>();

  app.addHook("onRequest", async (req, reply) => {
    if (req.headers["x-bridge-key"] !== config.bridge.apiKey) {
      reply.code(401).send({ error: "unauthorized" });
    }
  });

  app.post<{ Body: { sessionId: string; agentExtNumber: string } }>(
    "/sessions",
    async (req, reply) => {
      const { sessionId, agentExtNumber } = req.body;
      if (!sessionId || !agentExtNumber) {
        return reply.code(400).send({ error: "sessionId and agentExtNumber required" });
      }
      if (active.has(sessionId)) {
        return reply.code(409).send({ error: "already supervising", sessionId });
      }
      try {
        const sup = await superviseCall(softphone, sessionId, agentExtNumber);
        active.set(sessionId, sup);
        return reply.code(202).send({ sessionId, status: "supervising" });
      } catch (err) {
        req.log.error({ err }, "supervision start failed");
        return reply.code(500).send({ error: String(err) });
      }
    },
  );

  app.delete<{ Params: { id: string } }>("/sessions/:id", async (req, reply) => {
    const sup = active.get(req.params.id);
    if (!sup) return reply.code(404).send({ error: "not found" });
    await sup.stop();
    active.delete(req.params.id);
    return reply.code(204).send();
  });

  app.get("/health", async () => ({ ok: true, active: [...active.keys()] }));

  return app;
}
```

- [ ] **Step 2: Replace `softphone-bridge/src/index.ts` with the real entrypoint**

```typescript
import Softphone from "ringcentral-softphone";
import { config } from "./config.js";
import { buildServer } from "./server.js";

const softphone = new Softphone({
  domain: config.sip.domain,
  outboundProxy: config.sip.outboundProxy,
  username: config.sip.username,
  password: config.sip.password,
  authorizationId: config.sip.authorizationId,
  codec: "PCMU/8000",
});

await softphone.register();
console.log("[bridge] softphone registered");

const server = buildServer(softphone);
await server.listen({ host: "0.0.0.0", port: config.bridge.port });
console.log(`[bridge] HTTP listening on :${config.bridge.port}`);

async function shutdown() {
  console.log("[bridge] shutting down");
  await server.close();
  process.exit(0);
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
```

- [ ] **Step 3: Typecheck**

```bash
cd softphone-bridge && npm run typecheck
```

**Pass criteria:** no errors.

- [ ] **Step 4: Commit**

```bash
git add softphone-bridge/src/server.ts softphone-bridge/src/index.ts
git commit -m "feat(bridge): HTTP intake server (POST/DELETE /sessions, /health)"
```

---

### Task 7: Python — sidecar client

**Files:** Create `src/sidecar_client.py` and `tests/test_sidecar_client.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_sidecar_client.py
import pytest, respx, httpx
from src.sidecar_client import SidecarClient

@pytest.mark.asyncio
@respx.mock
async def test_start_supervision_posts_expected_payload():
    route = respx.post("http://bridge.local/sessions").mock(
        return_value=httpx.Response(202, json={"sessionId": "s-1", "status": "supervising"})
    )
    client = SidecarClient("http://bridge.local", api_key="k")
    await client.start_supervision("s-1", "101")
    assert route.called
    assert route.calls.last.request.headers["x-bridge-key"] == "k"

@pytest.mark.asyncio
@respx.mock
async def test_stop_supervision_deletes():
    route = respx.delete("http://bridge.local/sessions/s-1").mock(
        return_value=httpx.Response(204)
    )
    client = SidecarClient("http://bridge.local", api_key="k")
    await client.stop_supervision("s-1")
    assert route.called
```

- [ ] **Step 2: Run test — expect `ModuleNotFoundError`**

```bash
pytest tests/test_sidecar_client.py -v
```

- [ ] **Step 3: Implement `src/sidecar_client.py`**

```python
import logging
import httpx

logger = logging.getLogger(__name__)


class SidecarClient:
    """Thin async client for the softphone-bridge sidecar."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._headers = {"x-bridge-key": api_key, "content-type": "application/json"}
        self._timeout = timeout

    async def start_supervision(self, session_id: str, agent_ext_number: str) -> None:
        url = f"{self._base}/sessions"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.post(url, headers=self._headers,
                             json={"sessionId": session_id,
                                   "agentExtNumber": agent_ext_number})
            if r.status_code not in (202, 409):
                logger.warning("bridge start_supervision %s -> %s %s",
                               session_id, r.status_code, r.text)

    async def stop_supervision(self, session_id: str) -> None:
        url = f"{self._base}/sessions/{session_id}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.delete(url, headers=self._headers)
            if r.status_code not in (204, 404):
                logger.warning("bridge stop_supervision %s -> %s %s",
                               session_id, r.status_code, r.text)
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_sidecar_client.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/sidecar_client.py tests/test_sidecar_client.py
git commit -m "feat: async client for softphone-bridge sidecar"
```

---

### Task 8: Wire `call_monitor` to trigger supervision

**Files:** Modify `src/call_monitor.py`, `tests/test_call_monitor.py`

Two changes: (1) capture `extensionNumber` (not just `extensionId`) in stored call data, because the sidecar needs the dialed number; (2) on `Answered` for a monitored extension, fire `start_supervision`; on end, `stop_supervision`.

- [ ] **Step 1: Add failing tests to `tests/test_call_monitor.py`**

```python
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_answered_triggers_sidecar_when_monitored(store):
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    event = make_event("s-200", "Answered",
                       to_info={"extensionId": "119", "extensionNumber": "101",
                                "name": "Doug Stoker"})
    process_telephony_event(event, store, sidecar=sidecar, monitored_extensions=["119"])
    await asyncio.sleep(0)
    sidecar.start_supervision.assert_awaited_once_with("s-200", "101")


@pytest.mark.asyncio
async def test_answered_skipped_when_not_monitored(store):
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    event = make_event("s-200", "Answered",
                       to_info={"extensionId": "999", "extensionNumber": "999"})
    process_telephony_event(event, store, sidecar=sidecar, monitored_extensions=["119"])
    await asyncio.sleep(0)
    sidecar.start_supervision.assert_not_called()


@pytest.mark.asyncio
async def test_disconnected_stops_sidecar(store):
    sidecar = MagicMock()
    sidecar.start_supervision = AsyncMock()
    sidecar.stop_supervision = AsyncMock()
    process_telephony_event(make_event("s-200", "Answered",
                                       to_info={"extensionId": "119",
                                                "extensionNumber": "101"}),
                            store, sidecar=sidecar, monitored_extensions=["119"])
    await asyncio.sleep(0)
    process_telephony_event(make_event("s-200", "Disconnected",
                                       to_info={"extensionId": "119",
                                                "extensionNumber": "101"}),
                            store, sidecar=sidecar, monitored_extensions=["119"])
    await asyncio.sleep(0)
    sidecar.stop_supervision.assert_awaited_once_with("s-200")
```

- [ ] **Step 2: Run — expect FAIL (`sidecar` kwarg not accepted)**

```bash
pytest tests/test_call_monitor.py -v
```

- [ ] **Step 3: Modify `process_telephony_event` in `src/call_monitor.py`**

```python
def process_telephony_event(event: dict, store: CallStore,
                            sidecar=None, monitored_extensions=None) -> None:
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
    ext_number = to_info.get("extensionNumber") or from_info.get("extensionNumber")

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
        if sidecar and ext_id and _is_monitored(ext_id, monitored_extensions):
            asyncio.create_task(sidecar.stop_supervision(session_id))
    else:
        logger.info("Call event: %s (status=%s)", session_id, status)
        store.store_call(session_id, call_data)
        if (sidecar and status == "Answered" and ext_id and ext_number
                and _is_monitored(ext_id, monitored_extensions)):
            asyncio.create_task(sidecar.start_supervision(session_id, ext_number))


def _is_monitored(ext_id: str, monitored: list[str] | None) -> bool:
    if not monitored:
        return False  # Phase 2: empty = disabled (conservative — don't supervise everyone)
    return ext_id in monitored
```

Note: empty monitored list = disabled (opposite of Rev 1). This prevents accidental blanket supervision if the env var is forgotten.

- [ ] **Step 4: Plumb `sidecar` through `run_monitor` / `_run_ws_session`**

Add an optional `sidecar=None` parameter to both, capture in `on_message`'s closure, and pass `Config.MONITORED_EXTENSIONS` in the `process_telephony_event` call.

- [ ] **Step 5: Run tests**

```bash
pytest -v
```

**Pass criteria:** all Phase 1 tests still pass, plus 3 new ones.

- [ ] **Step 6: Commit**

```bash
git add src/call_monitor.py tests/test_call_monitor.py
git commit -m "feat: call monitor triggers sidecar supervision on Answered for monitored exts"
```

---

### Task 9: Wire sidecar client into local runner

**Files:** Modify `run_local.py` (or wherever the Python process starts) — passes `SidecarClient` into `run_monitor` when configured.

- [ ] **Step 1: Modify runner**

Pseudo-patch (adapt to the actual runner file):

```python
from src.sidecar_client import SidecarClient

sidecar = None
if Config.SOFTPHONE_BRIDGE_URL and Config.SOFTPHONE_BRIDGE_API_KEY:
    sidecar = SidecarClient(Config.SOFTPHONE_BRIDGE_URL, Config.SOFTPHONE_BRIDGE_API_KEY)
    logger.info("Live transcription enabled via %s", Config.SOFTPHONE_BRIDGE_URL)
else:
    logger.info("Live transcription disabled (no sidecar configured)")

await run_monitor(store, sidecar=sidecar)
```

- [ ] **Step 2: Commit**

```bash
git add run_local.py
git commit -m "feat: inject sidecar client into call monitor when configured"
```

---

### Task 10: End-to-end smoke test

Manual — exercises the full pipeline against real RC and Deepgram.

- [ ] **Step 1: All unit tests pass**

```bash
pytest -v
cd softphone-bridge && npm run typecheck && cd ..
```

- [ ] **Step 2: Populate both `.env` files**

Root `.env`: Phase 1 vars + `SOFTPHONE_BRIDGE_URL=http://localhost:8787` + `SOFTPHONE_BRIDGE_API_KEY=<same-as-sidecar>` + `MONITORED_EXTENSIONS=119` (or whichever rep's extension ID you're testing).

`softphone-bridge/.env`: from Task 0 output + Deepgram + Redis + `BRIDGE_API_KEY=<same-as-root>`.

- [ ] **Step 3: Start both processes (two terminals)**

```bash
# Terminal A — sidecar
cd softphone-bridge && npm run dev

# Terminal B — Python stack
python run_local.py
```

Expected in Terminal A:
```
[bridge] softphone registered
[bridge] HTTP listening on :8787
```

Expected in Terminal B:
```
Authenticated with RingCentral
Live transcription enabled via http://localhost:8787
Subscription active — listening for call events
```

- [ ] **Step 4: Make a call to/from a monitored extension**

Expected log sequence:
- Terminal B: `Call event: s-XXX (status=Answered)`
- Terminal A: `[sup:s-XXX] starting — agent ext 101` → `[sup:s-XXX] monitoring active` → `[sup:s-XXX] final: <words>` repeatedly.

- [ ] **Step 5: Query the API during the call**

```bash
curl -H "x-api-key: $CALL_BRIDGE_API_KEY" \
    http://localhost:8000/api/calls/latest?rep=<EXT_ID>
```

Expected: `transcript` field contains what's been said so far.

- [ ] **Step 6: Hang up and verify cleanup**

Terminal A: `[sup:s-XXX] stopped`
Terminal B: `Call ended: s-XXX`

- [ ] **Step 7: Commit any fixes**

---

## Known Risks & Mitigations

1. **Device `677386052` is the wrong type.** Task 0 catches this. If the type is `SoftPhone` (API) / "RingCentral Phone app" (GUI), we need to create a new "Existing Phone" device in the RC admin portal. This is a ~2-minute GUI operation, not a blocker.

2. **Supervision refused by RC** (`busy` event or DTMF reaches dead air). Most common cause: ext 120 is not a monitor in the Sales group, or the target rep isn't a member of the group. Dakota confirmed monitor membership ✅, but double-check the target rep is in the group too. Second cause: feature code `*80` is not enabled on our account — verify via RC admin → Phone System → Auto Receptionist → Dialing Plan / Feature Codes.

3. **Codec mismatch (silent transcripts).** Sidecar must be configured `codec: "PCMU/8000"` AND Deepgram must be `encoding=mulaw&sample_rate=8000`. If either drifts (e.g. someone changes the softphone codec to OPUS), Deepgram sees garbage and emits empty transcripts. Both are set in Task 4/5 — don't change without changing both.

4. **Concurrent supervised calls.** The SDK's outbound-call limit per registration isn't documented precisely. For MVP we assume N≤3 concurrent sessions (SFW has 3 active reps typically). The supervisor map in Task 6 is already per-session keyed, so multiple concurrent supervisions work in principle — but if RC enforces a per-device concurrency cap, we'll see failures at the `*80` call step. Fallback: multiple devices on ext 120 (create N "Existing Phone" devices, round-robin through their credentials).

5. **Sidecar deployment — where does it live?** Vercel serverless is wrong for a long-lived SIP client. Simplest path: same box as the Python call monitor (which is also long-lived). If that box is the user's laptop during development, production needs a small VPS or container. This is the same deployment question Phase 1's call monitor already has — not a new problem, but worth surfacing.

6. **Redis write contention.** Sidecar and Python both write to `call:{id}:transcript` — but only the sidecar writes it, and only Python reads it. No contention. `call:{id}:state` is Python-only. Confirmed safe.

7. **Latency budget.** Audio packet → Deepgram → finalize → Redis → GPT fetch = roughly 1–3s end-to-end. Deepgram final-result latency (~300–800ms on nova-3) is the dominant term. Acceptable for in-call coaching.
