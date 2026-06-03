# Scope → HubSpot Agreement Tool — Design Spec

**Date:** 2026-06-03
**Status:** Approved, ready for implementation plan
**Supersedes (for this workflow):** the live-extraction dashboard's coupling to the RingCentral/Deepgram/Redis pipeline and `src/scope_summarizer.py` (claude CLI). Those remain in the repo; this tool does not depend on them.

## Problem

SFW Construction's existing "Call Intelligence Bridge" is tightly coupled to a live RingCentral + Deepgram + Redis pipeline that "only sort of works." The day-to-day job a rep actually needs is narrower and does not require that pipeline:

Given a call's transcript and the caller's details, produce a standardized **SFW Service Agreement package** and push the customer into HubSpot — a **Contact**, a **Deal** (with address), and a **PandaDoc service agreement** attached to that deal.

This spec defines a standalone, stateless web tool that does exactly that. Live telephone transcription is explicitly a **follow-up**: when it returns, it becomes just another way to fill the notes box. It is out of scope here.

## Goals / Non-goals

**Goals**
- One page where a rep pulls a CallRail transcript, fills a short form, generates the agreement package with one click, and creates the HubSpot Contact + Deal with one click each.
- The generated **SCOPE** lands on the HubSpot Deal so the rep can create the PandaDoc from HubSpot's PandaDoc integration (tokens auto-fill).
- No dependency on Redis, RingCentral WebSocket, Deepgram, or the softphone sidecar. Request/response only; Vercel-serverless deployable.
- Reuse the existing `src/hubspot_client.py`.

**Non-goals (v1)**
- Live transcription (follow-up).
- Auto-creating the PandaDoc via the PandaDoc API — the rep creates it in HubSpot via the native integration.
- Auto-sending the customer email — the EMAIL section is rendered for the rep to send manually.
- Speaker diarization, multi-rep supervisor views, auth beyond a shared API key.

## Confirmed decisions (from brainstorming)

1. **Interface:** a webpage showing the CallRail/RingCentral transcript plus a form (name, email, phone, address, and the fields below).
2. **Service agreement = a PandaDoc** attached to the HubSpot Deal, created via PandaDoc's **native HubSpot integration** (not our API). Our tool's job ends at putting the SCOPE on the Deal.
3. **Transcript source for v1:** the **CallRail API** (not the live Redis pipeline).
4. **The "claude workflow"** is a **single Anthropic Messages API call** using the SFW Service Agreement Generator system prompt (Appendix A). One call emits all four sections; the SCOPE section feeds the PandaDoc. (Not two chained calls.)
5. **After generate:** render four editable boxes, then **one-click Create Contact** and **one-click Create Deal (with address)**; SCOPE is written to the Deal for the PandaDoc step.
6. **Model is config-driven**, defaulting to the current Sonnet. The spec the user supplied pinned `claude-sonnet-4-20250514`; this is overridable via env.

## Architecture

A new self-contained module, mounted into the existing FastAPI app. No new persistent process.

```
┌───────────────────────────────────────────────────────────────────┐
│  Browser — /agreement                                              │
│  ┌───────────────────────┐  ┌───────────────────────────────────┐ │
│  │ Transcript panel       │  │ Form: name/email/phone/address,   │ │
│  │ + "Pull from CallRail" │  │ issue type, active-leak toggle,   │ │
│  │                        │  │ delivery toggle, NOTES textarea   │ │
│  └───────────────────────┘  └───────────────────────────────────┘ │
│  [Generate]  → 4 editable boxes: HEADER / DEAL DESC / SCOPE / EMAIL │
│  [Create Contact]   [Create Deal]   (→ then PandaDoc in HubSpot)    │
└───────────────────────────────┬───────────────────────────────────┘
                                │  fetch (x-api-key header)
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│  FastAPI  (src/agreement_tool/, mounted from src/api/main.py)       │
│  GET  /agreement                          serves the page           │
│  POST /api/callrail/transcript            fetch transcript + caller │
│  POST /api/agreement/generate             Claude call + 4-way split │
│  POST /api/hubspot/contacts               upsert (hubspot_client)   │
│  POST /api/hubspot/deals                  create + associate        │
└───────┬───────────────────┬───────────────────┬────────────────────┘
        ▼                   ▼                   ▼
  ┌───────────┐      ┌──────────────┐    ┌───────────────┐
  │ CallRail  │      │  Anthropic   │    │   HubSpot     │
  │   API     │      │ Messages API │    │   CRM v3      │
  └───────────┘      └──────────────┘    └───────────────┘
```

The Anthropic, CallRail, and HubSpot keys live only on the server. The browser holds the shared `CALL_BRIDGE_API_KEY` (in `localStorage`, as the existing dashboard does) and sends it as `x-api-key`.

## Module layout

```
src/agreement_tool/
  __init__.py
  routes.py          # APIRouter: the four endpoints above
  generator.py       # Anthropic call + 4-section parser
  callrail.py        # thin CallRail API client
  models.py          # Pydantic request/response models
  prompts/
    service_agreement.md   # the system prompt, verbatim (Appendix A)
  static/
    agreement.html         # the single page (form + transcript + output boxes)
```

Reused as-is: `src/hubspot_client.py`. Extended: `src/config.py` (new keys), `src/api/main.py` (mount the router + `GET /agreement`).

## Components

### 1. `generator.py` — the SFW Service Agreement Generator

- `async generate_package(fields: AgreementInput) -> AgreementPackage`
  - Builds the **user message** from form fields (Appendix B).
  - Calls Anthropic Messages API: model from config, `max_tokens: 1500`, system prompt loaded from `prompts/service_agreement.md`, `anthropic-version: 2023-06-01`.
  - Uses `httpx.AsyncClient` directly (no SDK dependency — consistent with `hubspot_client.py`). Header `x-api-key: <ANTHROPIC_API_KEY>`.
  - Parses the single returned text block into four sections via the regexes the user specified:
    - `HEADER:` → up to `DEAL DESCRIPTION:`
    - `DEAL DESCRIPTION:` → up to `SCOPE:`
    - `SCOPE:` → up to `EMAIL:`
    - `EMAIL:` → end
  - Returns `AgreementPackage{header, deal_description, scope, email}`.
- **Parse robustness:** if any section is missing (model deviated), return what parsed and surface a `partial: true` flag + the raw text so the rep sees something rather than an opaque failure. The page keeps all boxes editable.

### 2. `callrail.py` — CallRail client

- `async get_recent_calls(limit=20) -> list[CallSummary]` — recent calls (id, caller name, number, tracking source, time, has-transcript flag).
- `async get_call_transcript(call_id) -> CallTranscript` — transcript text + caller fields used to pre-fill the form.
- Auth: `Authorization: Token token=<CALLRAIL_API_KEY>`; account scoped by `CALLRAIL_ACCOUNT_ID`. Base `https://api.callrail.com/v3`.
- Exact endpoint paths and transcript field names confirmed during implementation against CallRail's API; the client isolates that so callers depend only on `CallTranscript`.

### 3. `routes.py` — endpoints (all gated by existing `verify_api_key`)

- `POST /api/callrail/transcript` — body `{call_id}` (or `{}` to list recent) → transcript + caller fields.
- `POST /api/agreement/generate` — body = `AgreementInput` → `AgreementPackage` (+ `partial`/`raw` on deviation).
- `POST /api/hubspot/contacts` — body = contact props → `{contactId, url}`. Wraps `hubspot_client.upsert_contact`.
- `POST /api/hubspot/deals` — body `{contactId, dealname, description, scope, ...}` → `{dealId, url}`. Wraps `hubspot_client.create_deal`, and writes SCOPE to the deal (see HubSpot mapping).

### 4. `static/agreement.html` — the page

- Layout: transcript panel (left) + form (right), output boxes below, action buttons.
- Form fields: customer name, email, phone, **address** (+ city/state/zip), **issue type** (dropdown or free text), **active leak** (yes/no toggle), **delivery method** (email/text toggle), **notes/scope details** (primary textarea).
- Buttons & dependency order:
  1. **Pull from CallRail** → fills name/phone/transcript; rep edits the notes box.
  2. **Generate** → POST `/api/agreement/generate`; fills the four editable boxes.
  3. **Create Contact** → POST `/api/hubspot/contacts`; shows the contact link; enables Create Deal.
  4. **Create Deal** → POST `/api/hubspot/deals` with the created `contactId`, the address, DEAL DESCRIPTION, and SCOPE; shows the deal link.
  5. The rep opens the deal in HubSpot and clicks the **PandaDoc** template (auto-fills from deal/contact incl. SCOPE), then sends. The **EMAIL** box is there to copy/send to the customer.
- API key handled exactly like the existing dashboard: prompt once, store in `localStorage["sfw-bridge-key"]`, send as `x-api-key` on every fetch.

## HubSpot mapping

| Source section / field | HubSpot target |
|---|---|
| Form name/email/phone/address(+city/state/zip) | Contact standard properties (`firstname`, `lastname`, `email`, `phone`, `address`, `city`, `state`, `zip`) |
| HEADER | Deal name (`dealname`) |
| DEAL DESCRIPTION | Deal `description` |
| SCOPE | Deal property the PandaDoc template tokenizes — **exact property name confirmed with the user during implementation** (a dedicated `scope_of_work` property is recommended over reusing `description`, so the PandaDoc token and the short deal description stay independent) |
| EMAIL | Not written to HubSpot; rendered for the rep to send |

The Deal is associated to the Contact via the existing `create_deal` association (deal→contact, type id 3).

## Configuration

New env vars (added to `src/config.py`):

- `ANTHROPIC_API_KEY` — server-side only.
- `ANTHROPIC_MODEL` — default current Sonnet (`claude-sonnet-4-6`); set to `claude-sonnet-4-20250514` to pin the user's original.
- `CALLRAIL_API_KEY`, `CALLRAIL_ACCOUNT_ID`.
- Reused: `HUBSPOT_PRIVATE_APP_TOKEN`, `CALL_BRIDGE_API_KEY`.

All required tokens are already available in the user's **`causal-ai-lab`** environment and will be copied into this project's env (local `.env` / Vercel project env). `.env.example` gets the new keys with comments. `.env` stays gitignored (already is).

## Deployment

- **Local:** runs in the existing FastAPI app; the new routes need none of Redis/RingCentral/Deepgram.
- **Vercel (optional, now viable):** everything is request/response. The one blocker is that the current `api/index.py` entrypoint initializes Upstash Redis at import time. Fix: make the store optional in the entrypoint (construct `CallStore` only when `REDIS_URL` is set; `create_app` already accepts `store=None`). The Anthropic call (~1500 tokens, a few seconds) is well within Vercel's function timeout.

## Error handling

| Failure | Response |
|---|---|
| Anthropic 401 / missing key | 502, "agreement generator auth failed" |
| Anthropic 429 | one bounded retry, then 429 passthrough |
| Anthropic timeout (cap ~60s) | 504, "generator timed out" |
| Model output missing a section | 200 with `partial: true` + `raw`; page shows what parsed, all boxes editable |
| CallRail 401 / not found | 502 / 404 with CallRail's message; page falls back to manual paste |
| HubSpot 401/429/5xx | as existing `hubspot_client` behavior — surfaced as a toast with the body |
| Create Deal before Contact | page disables Create Deal until a `contactId` exists |

## Testing

- `tests/test_agreement_generator.py` — section splitter: well-formed 4-section block; missing EMAIL; reordered/blank sections → `partial`; user-message builder includes every form field (active-leak yes/no, delivery method).
- `tests/test_agreement_routes.py` — `respx`-mocked Anthropic, CallRail, HubSpot. Generate → four sections; CallRail fetch → prefilled fields; contact upsert → `{contactId}`; deal create → association + SCOPE written.
- `tests/test_callrail_client.py` — mocked CallRail responses → `CallTranscript` / `CallSummary` mapping; auth header shape.
- Existing HubSpot client tests remain valid (no signature changes).
- Page is smoke-tested manually (no browser automation in v1).

## Future work (follow-ups)

- **Live transcription** returns and pre-fills the notes box (replaces the CallRail pull or augments it).
- **PandaDoc API** automation (skip the manual HubSpot click) if the integration proves too manual.
- **Auto-send the customer EMAIL** via the chosen delivery method.
- Retire `src/scope_summarizer.py` once this generator is in production.

## Appendix A — System prompt (verbatim)

> You are the SFW Construction Service Agreement Generator. You generate complete, standardized SFW Construction Service Agreement packages. You accept plain English notes, text messages, transcripts, and mixed input formats, then output a full SA package automatically.
>
> OUTPUT PACKAGE — always output all sections in this exact order:
>
> HEADER:
> ACTION REQUIRED | SFW Construction Service Agreement | [Customer Name] | [YYYY-Mmm]
> Rules: current month only in YYYY-Mmm format. Never future months. Never em dashes. Mmm format only (Jan, Feb, Mar, etc.)
>
> DEAL DESCRIPTION:
> One clean simple sentence summarizing the work clearly.
>
> SCOPE:
> Brief customer-friendly scope of work. One short paragraph, 3-8 sentences. No headings, numbered lists, or bullet points. No process-step detail. Describe what SFW will inspect and what SFW will repair or replace. Clear, confident, professional. No technical jargon. No exaggeration. Include investigation, demo, findings, and repair process. Include customer-specific concerns. Active leaks receive priority language.
>
> EMAIL:
> Warm reassuring thank you email. Reinforce customer decision. No signoff. Must end with exactly: Thank you for choosing SFW Construction.
> Use active-leak version only when job involves active leak or emergency.
>
> STYLE RULES — absolute, never violated:
> No em dashes. Warm professional customer-facing language. No corporate clichés. Never mention AI, analysis, or speculation. Never make customer feel uncertain. Include reassurance when appropriate. Address cost concerns, skepticism, urgency, comfort. Reflect estimator notes accurately. Never output incorrect dates. Never guess at timeline or scheduling. Never make promises SFW cannot guarantee.
>
> ACTIVE LEAK BEHAVIOR:
> Triggers: water coming inside, staining, ceiling soft spots, dripping, tenant complaints, skylight leaks, window leaks, roof valley leaks. Automatically classify as active leak. Use active-leak email. Include priority language in scope.
>
> MISSING INFO:
> If customer name, location, or damage details are unclear, ask one short clarifying question then continue. Never stop or fail to produce output unless absolutely necessary.
>
> SPECIAL INSTRUCTIONS:
> If user says "waive the 4 hour minimum" adjust language accordingly.
> If customer is nervous add warm reassurance in scope.
> Always respect customer-specific instructions: budget caps, not-to-exceed language, realtor requirements.
> If user says "modify," "revise," or "redo" output a complete clean replacement package.
> Never invent details beyond what the user provides.

*Stored verbatim at `src/agreement_tool/prompts/service_agreement.md`. The "current month" date rule is satisfied by passing today's date into the user message (Appendix B) since the model has no clock.*

## Appendix B — User message format (built from form fields)

```
Customer name: {name}
Issue type: {issue_type}
Active leak: {yes|no}
Delivery method: {email|text}
Notes / scope details: {notes_textarea}
```

Plus a `Today's date: {YYYY-Mmm}` line injected server-side so the HEADER date rule ("current month only") can be honored deterministically.

## Appendix C — Section parsing regexes (from the user's spec)

```js
const header = text.match(/HEADER:\n([\s\S]*?)(?=\nDEAL DESCRIPTION:|$)/)?.[1]?.trim()
const deal   = text.match(/DEAL DESCRIPTION:\n([\s\S]*?)(?=\nSCOPE:|$)/)?.[1]?.trim()
const scope  = text.match(/SCOPE:\n([\s\S]*?)(?=\nEMAIL:|$)/)?.[1]?.trim()
const email  = text.match(/EMAIL:\n([\s\S]*?)$/)?.[1]?.trim()
```

The Python implementation mirrors these patterns; the splitter is the single source of truth and is unit-tested.
