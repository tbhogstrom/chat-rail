# Production Deployment (Vercel web app + Fly.io engine) — Design

**Date:** 2026-07-02
**Status:** Approved

## Problem

The sales team needs to use the tools (overview, dashboard, agreement, per-rep
metrics, recent calls). Today everything runs in one local process
(`run_local.py`) on a workstation. We need a production deployment that is
CLI-driven (deploy + test from the terminal).

## Constraint that shapes everything

The system is two deployables with different runtime needs, joined only by the
shared Upstash Redis:

1. **Web app** — FastAPI API + `/`, `/dashboard`, `/overview`, `/agreement`.
   Request/response only. Fits Vercel serverless. Needs Redis + HubSpot +
   Anthropic + CallRail + app key; **not** RC/Deepgram/SIP. The API import graph
   does not import `ringcentral`/`websockets`/`deepgram` (Anthropic is called via
   raw `httpx`), so the Vercel bundle stays on the existing slim
   `requirements.txt`.
2. **Engine** — always-on processes: call monitor (persistent RC WebSocket),
   extraction worker, metrics worker, and the Node softphone bridge (persistent
   SIP registration + live RTP → Deepgram). Cannot run on Vercel serverless.

## Topology

```
┌── Vercel (project: sfw-call-bridge) ──┐     ┌── Fly.io org "personal" ───────────────┐
│  FastAPI app via api/index.py         │     │  app sfw-engine (Python):              │
│  / /dashboard /overview /agreement    │     │    run_engine.py = monitor +           │
│  /api/calls/* /api/hubspot/* ...       │     │    extraction + metrics workers        │
│  Password Protection (edge)           │     │  app sfw-softphone-bridge (Node):      │
└───────────────┬───────────────────────┘     │    SIP register + RTP → Deepgram,      │
                │ reads/writes                 │    HTTP /sessions on [::]:8787          │
                └────────► Upstash Redis ◄──────┘   (engine → bridge over .internal)    │
                            (single shared)   └────────────────────────────────────────┘
```

Fly org for deployment: **`personal` (Tyler F)**.

## Component 1 — Vercel web app

- **Rewrite:** change `vercel.json` to a catch-all so FastAPI serves every path:
  ```json
  { "rewrites": [ { "source": "/(.*)", "destination": "/api/index" } ] }
  ```
  (Today only `/api/(.*)` is routed, so `/dashboard` etc. would 404.)
- **Entrypoint / deps:** `api/index.py` already exposes `create_app`; the slim
  `requirements.txt` (`fastapi`, `upstash-redis`, `httpx`, `python-dotenv`) is
  correct and complete for the API. Static HTML ships in the bundle (in-repo
  under `src/**/static`).
- **Env vars (set via `vercel env add` for production):**
  `KV_REST_API_URL`, `KV_REST_API_TOKEN`, `CALL_BRIDGE_API_KEY`,
  `HUBSPOT_PRIVATE_APP_TOKEN`, `HUBSPOT_PORTAL_ID`, `HUBSPOT_SCOPE_PROPERTY`,
  `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `CALLRAIL_API_KEY`,
  `CALLRAIL_ACCOUNT_ID`, `MONITORED_EXTENSIONS`, `SALES_SCRIPT_CLAUDE_URL`.
- **Auth:** enable Vercel **Password Protection** (one shared site password; no
  Vercel accounts for reps) at the edge, and keep the existing app `x-api-key`
  (dashboards prompt once → localStorage). Two one-time secrets, no code change.
  Machine/ChatGPT-Action access is intentionally not supported in production.
- **Deploy/test (CLI):** `vercel deploy --prod`, `vercel logs`, then load the
  deployment URL and confirm `/overview` and `/dashboard?rep=<extId>` render.

## Component 2 — Fly engine app (`sfw-engine`, Python)

- **New entrypoint `run_engine.py`** — mirrors `run_local.py`'s worker wiring but
  with **no API/uvicorn**: builds the `CallStore` (real Upstash), builds the
  `SidecarClient` from `SOFTPHONE_BRIDGE_URL` + `SOFTPHONE_BRIDGE_API_KEY`, and
  `asyncio.gather(run_monitor(store, sidecar=sidecar), run_extraction_worker(store), run_metrics_worker(store))`.
- **`Dockerfile.engine`** — `python:3.12-slim`, install engine deps
  (`requirements-engine.txt`: `ringcentral`, `upstash-redis`, `httpx`,
  `python-dotenv`, `websockets`, `tzdata`), copy `src/` + `run_engine.py`,
  `CMD ["python", "run_engine.py"]`.
- **`fly.engine.toml`** — no `[http_service]` (it's a worker); one always-on
  Machine (`auto_stop_machines = false`, `min_machines_running = 1`).
- **Secrets (`fly secrets set -a sfw-engine`):** `RC_CLIENT_ID`,
  `RC_CLIENT_SECRET`, `RC_JWT`, `RC_SERVER`, `KV_REST_API_URL`,
  `KV_REST_API_TOKEN`, `MONITORED_EXTENSIONS`, `METRICS_TIMEZONE`,
  `SOFTPHONE_BRIDGE_URL=http://sfw-softphone-bridge.internal:8787`,
  `SOFTPHONE_BRIDGE_API_KEY`.

## Component 3 — Fly softphone bridge app (`sfw-softphone-bridge`, Node)

- **`Dockerfile`** (in `softphone-bridge/`) — `node:24-slim`, `npm ci`,
  `npm run build`, `CMD ["node", "dist/index.js"]`.
- **Listen on IPv6:** change `server.listen({ host: "0.0.0.0", ... })` to
  `host: "::"` (accepts both stacks). Fly private networking (`.internal`) is
  IPv6-only, so the engine can only reach the bridge's `/sessions` if it binds
  `::`. Make the host an env-overridable value defaulting to `::`.
- **`fly.toml`** — no public services (SIP/RTP is **outbound only**; the HTTP
  `/sessions` API is reached privately by `sfw-engine` over `.internal`). One
  always-on Machine.
- **Secrets (`fly secrets set -a sfw-softphone-bridge`):** `SIP_INFO_DOMAIN`,
  `SIP_INFO_OUTBOUND_PROXY`, `SIP_INFO_USERNAME`, `SIP_INFO_PASSWORD`,
  `SIP_INFO_AUTHORIZATION_ID`, `DEEPGRAM_API_KEY`, `KV_REST_API_URL`,
  `KV_REST_API_TOKEN`, `BRIDGE_API_KEY`, `BRIDGE_PORT=8787`.

## Component 4 — Shared state & secrets

- One Upstash Redis (the existing DB) is the only cross-component channel;
  Vercel, `sfw-engine`, and `sfw-softphone-bridge` all point at it.
- No secrets in git (`.env` already gitignored). Vercel env via `vercel env`;
  Fly env via `fly secrets`. Fly/Vercel CLIs authenticate interactively (user
  runs `fly auth login` / `vercel login`); scripted steps run from the terminal.

## Risks & verification (the real work, all terminal-driven)

1. **Symmetric RTP from Fly (primary unknown).** The softphone is outbound-only
   (SIP/TLS signaling + outbound RTP/UDP; RC replies to the observed source), so
   no inbound public UDP is needed — but Fly's egress NAT must keep a stable
   source mapping for the RTP flow. **Verify:** `fly logs -a sfw-softphone-bridge`
   shows `softphone registered`, then a live call shows `audio packets` climbing
   and Deepgram `Results`. **Fallback if audio fails:** allocate a dedicated IPv4
   (`fly ips allocate-v4 -a sfw-softphone-bridge`) and/or pin a single Machine;
   if Fly still mangles RTP, fall back to a Fly Machine with a dedicated IP or a
   plain VM for the bridge only (engine stays on Fly).
2. **Engine → bridge private networking.** Confirm `sfw-engine` reaches
   `http://sfw-softphone-bridge.internal:8787/health` (depends on the `::` bind).
3. **End-to-end.** Place a real call to a monitored rep → `fly logs -a sfw-engine`
   shows `Supervision START` → transcript appears in Upstash and on the Vercel
   `/dashboard?rep=<extId>`.
4. **Vercel catch-all.** Confirm `/dashboard` and `/overview` load on the
   deployment URL (not just `/api/*`), and Password Protection gates access.

## Out of scope (YAGNI)

Custom domain (`.vercel.app` + Fly defaults are fine for launch), CI/CD
auto-deploy on push, per-user SSO, engine high-availability/failover,
multi-region.
