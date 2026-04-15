# SFW Call Intelligence Bridge — Design Spec

## Problem

SFW Construction's sales team uses ChatGPT with a Sales Script Builder GPT for live call coaching. They need real-time call context — caller identity, rep assignment, and live transcript — fed into their ChatGPT conversations so the GPT can generate relevant coaching, objection handling, and next-step scripts during active calls.

RingCentral (RingEX Advanced) is the phone system. The API provides real-time call events via WebSocket and call audio via the Supervision API, but does not expose the live transcript text that appears in the RC app. We bridge this gap by capturing call audio through the Supervision API and running our own real-time speech-to-text via Deepgram.

## Confirmed API Capabilities

Validated against SFW's RingCentral account (ID: 259471052) on 2026-04-15:

| Capability | Available | Method |
|---|---|---|
| Real-time call events (ring, answer, end, caller ID, rep) | Yes | WebSocket subscription |
| Account-wide active call list | Yes | REST polling |
| Telephony session details (parties, status, recording) | Yes | REST GET |
| Call Supervision (silent monitor, live audio stream) | Yes | Supervision API + SIP |
| Call recording download | Yes | REST GET (post-call) |
| Live transcript text | No | Not exposed via any API endpoint |
| Call Monitoring Group "Sales" | Pre-configured | ID: 1698052 |

**Account details:**
- Plan: RingEX Advanced
- 19 user extensions, 5 IVR menus, 10 forwarded numbers across PNW markets
- CallSupervision: enabled
- VoiceCallsLiveTranscriptions: available (app-only, not API-accessible)
- Automatic call recording: enabled on most extensions
- Auth: JWT (server-to-server), scopes include CallControl, SubscriptionWebSocket, ReadCallRecording, RingSense

## Architecture

### Components

**1. Call Monitor Service (persistent process)**
- Maintains WebSocket connection to RingCentral
- Subscribes to `/restapi/v1.0/account/~/telephony/sessions`
- Listens for `receiveSubscriptionNotification` events (confirmed working)
- On call answered: triggers Supervision API connection for that session
- On call ended: marks session complete, stores final state
- Writes call state to Redis

**2. Transcript Service**
- Receives live audio from RingCentral Supervision API via SIP
- Pipes audio stream to Deepgram real-time WebSocket API
- Deepgram returns transcript chunks with speaker diarization
- Accumulates chunks in Redis keyed by telephony session ID
- Supports per-party audio streams for accurate speaker identification

**3. Call Context Store (Upstash Redis)**
- Serverless Redis, free tier sufficient for SFW's call volume
- Key structure:
  - `call:{sessionId}:state` — call metadata (direction, parties, status, timestamps)
  - `call:{sessionId}:transcript` — accumulated transcript chunks
  - `call:{sessionId}:recording` — recording ID and URL (available post-call)
  - `calls:active` — set of currently active session IDs
  - `rep:{extensionId}:current` — pointer to rep's current/latest call
- TTL: active calls persist until 1 hour after completion, then expire

**4. GPT Action API (FastAPI on Vercel)**
- Endpoints:
  - `GET /api/calls/active` — list all active calls with caller info and rep
  - `GET /api/calls/{sessionId}/context` — full call context (metadata + transcript so far)
  - `GET /api/calls/{sessionId}/transcript` — transcript text only
  - `GET /api/calls/latest?rep={extensionId}` — most recent call for a specific rep
- Auth: API key in header (shared across sales team, simple for small team)
- Returns structured JSON that ChatGPT can parse and use for coaching

### Data Flow

```
1. Call rings
   → WebSocket event: status=Proceeding, from={caller}, to={rep/IVR}
   → Store: create call session, record caller ID, direction, rep

2. Call answered
   → WebSocket event: status=Answered
   → Supervision API: connect SIP monitor to session (silent, mixed audio)
   → Deepgram: open real-time WebSocket, start streaming audio
   → Store: update status to "active", start accumulating transcript

3. During call
   → Deepgram: returns transcript chunks every few seconds
   → Store: append chunks to call:{sessionId}:transcript
   → Rep in ChatGPT: "update call context"
   → GPT Action: GET /api/calls/latest?rep=119
   → Response: caller name, phone, source, transcript so far, duration
   → Sales Script Builder GPT: generates coaching based on context

4. Call ends
   → WebSocket event: status=Disconnected
   → Deepgram: final transcript, close connection
   → Supervision API: SIP session ends
   → Store: mark complete, store recording URL when available
```

### Deployment

**Persistent services (Call Monitor + Transcript):**
- These require long-running WebSocket/SIP connections — cannot run on Vercel serverless
- Deploy on: Railway, Fly.io, or a small VPS/container
- Single Python process managing both WebSocket (RC events) and SIP (audio)
- Auto-reconnect on disconnection

**API layer:**
- FastAPI deployed on Vercel serverless functions
- Reads from Upstash Redis (serverless, no connection management)
- Stateless — just reads the store and returns JSON

**Store:**
- Upstash Redis (serverless, free tier: 10K commands/day, 256MB)
- SFW call volume (~50-100 calls/day) fits comfortably in free tier

### Technology Stack

- **Language:** Python 3.14
- **Framework:** FastAPI
- **RingCentral SDK:** `ringcentral` (Python, v0.9.2) for auth, WebSocket, REST
- **SIP:** `pjsua2` (PJSIP Python bindings) for Supervision API audio capture — mature, well-documented, supports RTP audio streaming
- **STT:** Deepgram real-time WebSocket API (~$0.006/min)
- **Store:** Upstash Redis
- **API hosting:** Vercel (serverless functions)
- **Persistent process:** Railway or Fly.io

### Authentication

- **RingCentral:** JWT auth (server-to-server), already configured
- **Deepgram:** API key (to be provisioned)
- **GPT Action API:** Shared API key in `x-api-key` header
- **Upstash Redis:** Connection URL with token

### Call Monitoring Group

The "Sales" monitoring group (ID: 1698052) already exists with Jacob Hair (ext 118) and Doug Stoker (ext 119) as monitors. For the Supervision API to work, the API's JWT user (Tyler Falcon, ext 120) needs to be added as a monitor in this group, and the reps whose calls should be monitored need to be added as monitored members.

### Error Handling

- WebSocket disconnect: automatic reconnect with exponential backoff
- SIP connection failure: log error, skip transcription for that call, still track call state
- Deepgram connection failure: fall back to post-call recording transcription
- Redis unavailable: API returns 503, clients retry
- Stale call state: TTL-based expiry prevents ghost sessions

### Limitations

- Supervision API adds a silent party to the call — no audio quality impact but visible in session details
- Deepgram latency: ~1-3 seconds for transcript chunks
- Transcript available only while the monitoring process is running
- No live transcript for calls that started before the monitor connected
- Speaker diarization accuracy depends on audio quality and speaker distinctness

## GPT Action OpenAPI Schema

```yaml
openapi: 3.0.0
info:
  title: SFW Call Intelligence Bridge
  version: 1.0.0
servers:
  - url: https://sfw-call-bridge.vercel.app
paths:
  /api/calls/active:
    get:
      operationId: getActiveCalls
      summary: List all active calls across the SFW sales team
      responses:
        '200':
          description: Active calls with caller info and rep assignment
  /api/calls/{sessionId}/context:
    get:
      operationId: getCallContext
      summary: Get full context for a specific call including transcript
      parameters:
        - name: sessionId
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: Call metadata, parties, and transcript so far
  /api/calls/{sessionId}/transcript:
    get:
      operationId: getCallTranscript
      summary: Get live transcript for an active call
      parameters:
        - name: sessionId
          in: path
          required: true
          schema:
            type: string
      responses:
        '200':
          description: Transcript text with timestamps and speaker labels
  /api/calls/latest:
    get:
      operationId: getLatestCall
      summary: Get the most recent or current call for a rep
      parameters:
        - name: rep
          in: query
          required: false
          schema:
            type: string
          description: Extension ID or extension number of the rep
      responses:
        '200':
          description: Latest call context for the specified rep
```

## MVP Scope

Phase 1 (MVP):
- Call Monitor Service with WebSocket events (confirmed working)
- Active calls tracking in Redis
- GPT Action API returning call metadata (caller, rep, direction, duration)
- Post-call recording transcription via Deepgram (batch, not real-time)

Phase 2:
- Supervision API integration for live audio capture
- Deepgram real-time STT pipeline
- Live transcript accumulation in Redis
- GPT Action returns live transcript during active calls

Phase 3:
- CallRail attribution enrichment (source, campaign, tracking number)
- Call scoring and summary generation
- Historical call search
