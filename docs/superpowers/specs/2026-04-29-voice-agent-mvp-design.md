# Voice Agent MVP — Design Spec

## Problem

The existing SFW Call Intelligence Bridge captures live call audio for human reps and feeds transcripts/extracted state into ChatGPT for coaching. We now want to flip the configuration: an RC extension answered entirely by an AI bot — no human rep involved — that holds a coherent voice conversation with the caller.

For MVP, the bot is a **sandbox**: a fixed persona that greets callers, holds a natural conversation driven by ChatGPT, and ends gracefully. No business goal beyond proving the audio loop. Real use cases (intake, scheduling, FAQ) come later by swapping the system prompt — they do not change the architecture.

## Confirmed Constraints

- `pjsua2` (the SIP library used in earlier exploration) is not viable on Windows. SIP/RTP work must live in a container we don't maintain.
- The bot runs on the user's local Windows box for MVP. Cloud GPU and hosted TTS are anticipated future targets — the TTS layer must be pluggable from day one.
- RingCentral SIP registration uses outbound REGISTER from the bot's SIP endpoint. No public IP / port forwarding is required: RC sends INVITEs over the registered transport.
- The dedicated bot extension is a separate RC extension from any human rep extension. The existing `call_monitor` / dashboard pipeline is unaffected.

## Architecture

### Responsibility split

| Concern | Owner |
|---|---|
| SIP registration with RC, NAT traversal, RTP, codec conversion | Jambonz |
| Caller silence detection, utterance boundary | Jambonz (`gather` verb) |
| Speech-to-text on caller audio | Jambonz → Deepgram (existing key, native Jambonz vendor) |
| Text-to-speech on bot replies | Jambonz → app's `/tts` endpoint (custom HTTP TTS vendor) |
| Conversation state (history, system prompt) | Voice Agent app |
| OpenAI ChatGPT call (streaming) | Voice Agent app |
| Sentence chunking and verb pacing | Voice Agent app |
| TTS backend selection (Kokoro / OpenAI / hosted) | Voice Agent app |

### Component diagram

```
┌──────────────────┐   SIP REGISTER (outbound)    ┌──────────────────────┐
│   RingCentral    │ ───────────────────────────▶ │  Jambonz (Docker)    │
│  bot extension   │ ◀─── INVITE on inbound ────  │  on Windows box      │
└──────────────────┘                              │                      │
                                                  │  - SIP / RTP / codec │
                                                  │  - VAD / silence det │
                                                  │  - STT vendor call   │
                                                  │  - TTS vendor call   │
                                                  └──────┬───────────────┘
                                                         │
                          ┌──────────────────────────────┼──────────────────────────┐
                          │                              │                          │
                          ▼ WS app per call              ▼ STT API (Deepgram)       ▼ /voice/{bot_id}/tts
                  ┌───────────────────┐          ┌────────────────┐         ┌──────────────────┐
                  │ Voice Agent app   │          │   Deepgram     │         │ TTS dispatch     │
                  │ (FastAPI)         │          │ (existing key) │         │ → KokoroLocal    │
                  │ /voice/{id}/ws    │          └────────────────┘         │ → OpenAITTS stub │
                  │ /voice/{id}/tts   │                                     └──────────────────┘
                  └─────────┬─────────┘
                            │ stream=True
                            ▼
                  ┌───────────────────┐
                  │   OpenAI API      │
                  │ ChatCompletion    │
                  └───────────────────┘
```

### Components

#### 1. Jambonz container (new infra)

- One `docker-compose.yml` at `infra/jambonz/` using the `jambonz/all-in-one` image.
- Configured manually via Jambonz's admin UI (one-time setup, documented in `docs/voice-agent-setup.md`):
  - **Carrier** named `ringcentral` — outbound REGISTER with the bot extension's SIP credentials.
  - **Speech credentials** — Deepgram (existing API key) as STT vendor; **custom HTTP** TTS vendor pointed at `http://host.docker.internal:8000/voice/{bot_id}/tts`.
  - **Application** per bot — WebSocket app type, URL `ws://host.docker.internal:8000/voice/{bot_id}/ws`.
  - **Phone number / extension binding** — the bot's RC extension is bound to its Application.

#### 2. Voice Agent app (new module within existing FastAPI app)

Lives at `src/voice_agent/`, mounted from `src/api/main.py`. Handlers:

- `WS /voice/{bot_id}/ws` — Jambonz's WebSocket application interface. One WS per call. Handles:
  - `session:new` — load profile, create `ConversationSession`, push greeting `say` + initial `gather`.
  - `verb:hook` (gather result) — invoke conversation engine with streaming GPT, push `say` verbs per sentence as they emerge, push next `gather` on stream end.
  - `session:done` — cancel in-flight tasks, drop session.
- `POST /voice/{bot_id}/tts` — custom TTS vendor endpoint. Receives `{text, voice, language}`, dispatches to the profile's TTS backend, returns `audio/wav` bytes.

Internal pieces:

- **Verb pump** — `asyncio.Queue` per call. A coroutine awaits items and `ws.send_json`s them. Decouples GPT streaming from WS writing.
- **Sentence chunker** — state machine over GPT delta tokens. Emits a chunk on `[.!?]` followed by whitespace, or on a force-flush at >80 chars, or on stream end if a fragment remains.

#### 3. Conversation engine

`src/voice_agent/conversation.py`

- `ConversationSession(call_sid, profile)`:
  - `history: list[dict]` — `[{"role":"system","content":<prompt>}, ...]`, system prompt at index 0.
  - `async stream_response(user_text)` — async generator yielding GPT delta tokens. Internally: appends user message, opens `client.chat.completions.create(..., stream=True)`, yields delta content. On stream end appends assembled assistant message to history.
- Sessions held in an in-memory `dict[call_sid, ConversationSession]`. Cleared on `session:done`. No Redis for MVP (single host, calls are short-lived).

#### 4. TTS adapter layer (pluggable)

`src/voice_agent/tts/`

- `base.py` — `class TTSBackend(Protocol): async def synthesize(self, text: str, voice: str) -> tuple[bytes, str]` returning `(audio_bytes, mime_type)`.
- `kokoro_local.py` — `KokoroLocal` backend using `kokoro-onnx`. ONNX runtime, no PyTorch dependency on Windows. Returns 16-bit PCM WAV at 16 kHz; Jambonz handles resampling to 8 kHz μ-law for SIP.
- `openai_tts.py` — `OpenAITTS` scaffold. Raises `NotImplementedError` in MVP. Exists so the pluggable seam is real, not aspirational.
- `__init__.py` — `get_tts_backend(name: str) -> TTSBackend` factory. Backends registered by string key (`"kokoro_local"`, `"openai_tts"`).

#### 5. Bot profile loader

`src/voice_agent/profiles.py`

- Loads YAML files from `src/voice_agent/bots/*.yaml` at startup. Missing or malformed files fail fast.
- Profile schema:
  ```yaml
  id: sandbox
  display_name: "SFW Sandbox Bot"
  system_prompt_path: prompts/sandbox.md
  voice: af_bella
  tts_backend: kokoro_local
  openai_model: gpt-4o-mini
  ```
- Adding a bot: drop a YAML in `bots/`, create a Jambonz Application pointing at `/voice/{new_id}/ws`, bind a new RC extension. No code changes.
- MVP ships exactly one profile: `sandbox.yaml`.

#### 6. Config additions

`src/config.py` gets:

- `OPENAI_API_KEY`
- `KOKORO_MODEL_PATH` — path to ONNX model file
- `KOKORO_VOICES_PATH` — path to voices file (kokoro-onnx requires both)
- `BOT_PROFILES_DIR` — default `src/voice_agent/bots`
- `JAMBONZ_WEBHOOK_SECRET` — declared, not enforced in MVP (see Error Handling)

### Data flow

```
1. Caller dials the bot's RC extension.
   RC routes to Jambonz; Jambonz answers and opens WS to
   /voice/sandbox/ws (one WS per call, key = call_sid in open frame).

2. App receives session:new frame.
   → loads sandbox profile
   → creates ConversationSession(call_sid)
   → enqueues:
       {"verb":"say","text":"Hi, this is the SFW sandbox. Talk to me."}
       {"verb":"gather","input":["speech"],
        "recognizer":{"vendor":"deepgram","language":"en-US"},
        "timeout":8,"bargein":false,
        "actionHook":"turn"}
   → verb pump writes them to WS.

3. Caller speaks. Jambonz runs Deepgram, fires verb:hook with
   {speech:{alternatives:[{transcript:"..."}]}}.

4. App handles the turn:
   a. session.history append user message.
   b. Open OpenAI ChatCompletion with stream=True.
   c. Sentence chunker consumes deltas. On each completed sentence
      (or 80-char force-flush), enqueue:
         {"verb":"say","text":"<sentence>"}
   d. On stream end, enqueue next gather verb.
   e. session.history append assistant message (full reply).

5. Jambonz, for each say verb, calls /voice/sandbox/tts to render
   audio (sequentially in order received). Plays into the call.
   First audio reaches caller ~1.2–1.8 s after they stopped speaking.

6. After all says play, gather runs → loop to step 3.

7. Caller hangs up.
   → Jambonz sends session:done frame.
   → app cancels in-flight GPT/TTS tasks via Task.cancel(),
     drops session, closes WS.
```

### Latency budget (per turn)

- Deepgram finalize: ~300 ms
- OpenAI first token (gpt-4o-mini, streaming): ~600 ms
- Kokoro local synth, first sentence on CPU: ~400–700 ms
- Jambonz play start: ~100 ms

**First-audio gap target: 1.2–1.8 s** after caller silence detected. Subsequent sentences play with no perceptible gap because synth + playback overlap with continued GPT streaming.

## Error handling

| Failure | Detection | Response |
|---|---|---|
| OpenAI request fails / times out (10 s cap) | Exception in stream loop | Push fallback `say`: "Sorry, having trouble — could you repeat that?" + fresh `gather`. No call drop. |
| Kokoro synth fails / hangs | 5 s timeout in `/tts` handler | Return 1-second silent WAV. Log error. Bot effectively skips that sentence; conversation continues. |
| WS to Jambonz drops mid-call | `WebSocketDisconnect` in handler | Cancel GPT stream, drop session. Jambonz tears down the call independently. Log loud. |
| Deepgram returns empty transcript | `verb:hook` with no alternatives | Push `say` "Didn't catch that — try again?" + fresh `gather`. Two empties in a row → polite goodbye + `{"verb":"hangup"}`. |
| Caller silent for full `gather` timeout (8 s) | Hook fires with `reason: "timeout"` | Same handling as empty transcript. |
| OpenAI rate-limited (429) | Exception status code | One bounded retry after 500 ms. If still failing, fallback say + gather. |
| Bot extension SIP REGISTER fails | Jambonz UI / logs | Surfaces before any call exists. Manual problem, out of runtime scope. |
| Concurrent calls to the same bot | Multiple WS sessions | Each gets its own `ConversationSession`. In-memory dict keyed by `call_sid`, no collision. |
| Call ends while GPT is mid-stream | `session:done` frame | `asyncio.Task.cancel()` on stream task. Don't push remaining sentences. |
| Profile YAML missing or malformed | App startup | Fail fast. App refuses to start. |

### Intentionally not handled in MVP

- **Audio quality / codec mismatches** — Jambonz negotiates codecs; if it can't, the call doesn't connect. No partial-failure path to defend against.
- **Barge-in** — `bargein: false` on every gather. Adding it later: cancel pending verbs in the queue and send a Jambonz `verb:cancel` on a caller-speaking event.
- **Persistence** — if the app restarts mid-call, in-flight calls die. The dashboard does not surface bot calls, so no orphaned UI state. Acceptable for sandbox.
- **Call recording / post-call transcript storage** — out of scope. Can be added later by emitting events to the existing `CallStore` from `session:done`.
- **HMAC / auth on Jambonz webhooks** — both services bind to localhost on the same Windows box. `/voice/*` is not publicly exposed. `JAMBONZ_WEBHOOK_SECRET` config is declared for the day Jambonz moves off-box; not enforced in v1.

## Testing strategy

### Unit tests (pytest, no Jambonz, no telephony)

- `test_sentence_chunker.py` — delta token sequences → expected sentence emissions: normal punctuation, 80-char force-flush, whitespace-only deltas, trailing fragment on stream end.
- `test_conversation_session.py` — mock OpenAI client. System prompt is at index 0; history accumulates user + assistant messages; `stream_response` yields deltas and assembles full reply correctly.
- `test_tts_backend.py` — `KokoroLocal.synthesize("hello")` returns non-empty `audio/wav` bytes. Marked `@pytest.mark.slow` (real model invocation).
- `test_bot_profile_loader.py` — valid YAML loads; missing required keys raise; unknown TTS backend name raises.

### Integration tests (FastAPI TestClient + mocked Jambonz)

- `test_voice_routes.py` — exercise the WS endpoint with a fake Jambonz peer:
  - `session:new` → app emits greeting `say` + initial `gather`.
  - `verb:hook` with transcript → app calls a mocked streaming OpenAI client (yields canned deltas) and emits the expected sequence of `say` verbs followed by a `gather`.
  - `session:done` → app cancels in-flight tasks and closes cleanly.
- `test_tts_endpoint.py` — POST `/voice/sandbox/tts` with sample text; backend mocked to return known bytes; assert status, content-type, body.

### Manual smoke (run before declaring MVP done)

Documented in `docs/voice-agent-smoke.md`:

1. Bring up Jambonz, REGISTER the bot extension, confirm green status in Jambonz UI.
2. Start the FastAPI app, hit `/voice/sandbox/tts` with curl, get audio.
3. From a phone, dial the bot extension. Confirm:
   - Greeting plays in <2 s of pickup.
   - Speak a sentence; bot replies coherently within ~2 s of you stopping.
   - Speak two more turns; conversation history holds (bot remembers turn 1).
   - Hang up; logs show clean session teardown.
4. Repeat with two simultaneous calls (second phone) — confirm independent conversations.

### Not covered

- End-to-end SIP/RTP — Jambonz is the source of truth.
- Latency benchmarks — variance from network + OpenAI rules out meaningful CI numbers. Manual smoke is sufficient.
- Voice quality — subjective.

## Deployment

- **Host:** existing Windows dev box, same one running `call_monitor` and the FastAPI app.
- **New runtime dependencies:**
  - Docker Desktop (for Jambonz).
  - Python deps: `kokoro-onnx`, `openai` (already in repo if not present), `pyyaml`.
  - Kokoro ONNX model + voices files downloaded once to a local path (referenced by `KOKORO_MODEL_PATH` / `KOKORO_VOICES_PATH`).
- **Network:** Jambonz container uses host networking (or explicit UDP port mapping for SIP) so REGISTER reaches RC. App binds `localhost:8000`. Jambonz reaches the app via `host.docker.internal`.

## Future work (out of MVP scope)

- **Barge-in** — switch `bargein: true` and add verb-cancellation on caller-speech detection.
- **Hosted TTS** — implement `OpenAITTS` backend; flip `tts_backend` in profile.
- **Cloud GPU host** — move Kokoro to a remote service; introduce `KokoroRemote` backend (HTTP client to a small Kokoro service on a GPU VM).
- **Real use cases** — intake, scheduling, FAQ. Each is a new bot profile + system prompt + (optionally) a tool-calling integration. No architectural change required.
- **Persistence / observability** — emit bot-call events into the existing `CallStore`; surface bot calls in the dashboard.

## MVP scope summary

In MVP, ship:

1. Jambonz Docker stack with one Carrier (RC), one Application (sandbox), bot extension bound.
2. `src/voice_agent/` module with WS endpoint, `/tts` endpoint, conversation engine, sentence chunker, verb pump, profile loader, KokoroLocal backend, OpenAITTS scaffold.
3. One bot profile: `sandbox.yaml` + `prompts/sandbox.md`.
4. Unit + integration tests as listed.
5. Setup doc (`docs/voice-agent-setup.md`) and smoke checklist (`docs/voice-agent-smoke.md`).

Out: barge-in, persistence, dashboard integration, additional bot profiles, hosted TTS, cloud GPU.
