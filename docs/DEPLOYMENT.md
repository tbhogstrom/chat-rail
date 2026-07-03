# Deployment & Infrastructure

Production is **three deployables** sharing one **Upstash Redis** (the only channel between them):

| Component | Runs on | What it is |
|---|---|---|
| **Web app** | Vercel — project `sfwc/callrail-chatgpt`, URL `https://callrail-chatgpt-sfwc.vercel.app` | FastAPI app (`api/index.py`): dashboards (`/`, `/dashboard`, `/agreement`) + JSON API |
| **Engine** | Fly.io — app `sfw-engine` (org `personal`, region `sjc`) | `run_engine.py` = RC call monitor + extraction worker + metrics worker |
| **Softphone bridge** | Fly.io — app `sfw-softphone-bridge` (org `personal`, region `sjc`) | Node/Fastify (`softphone-bridge/`): SIP registration + live RTP → Deepgram |

```
Vercel web app ──reads──┐                 ┌──writes── sfw-engine (monitor/extraction/metrics)
                        ├── Upstash Redis ─┤
                        └──               ─┴──writes── sfw-softphone-bridge (transcripts)
engine ──HTTP over Fly .internal──> sfw-softphone-bridge:8787   (starts/stops *80 supervision)
```

## Access / auth

The web app is gated by a **shared password** (app-level, not Vercel's paid add-on). Reps open the
URL → `/login` → enter the password → signed cookie (`sfw_session`, 30 days). Set by
`APP_PASSWORD` (Vercel env; currently `sellit`). Vercel Authentication (SSO) is **off** so this gate
is the front door. The API also still accepts an `x-api-key` header (unused in prod). See
`src/api/session.py`, `src/api/auth.py`, and the middleware in `src/api/main.py`.

## Deploying

CLIs: **Vercel** (`vercel`, logged in to scope `sfwc`) and **Fly** (`flyctl`, at
`~/.fly/bin/flyctl.exe`, logged in as tfalcon@sfwconstruction.com). Both need interactive login
(`vercel login` / `flyctl auth login`) if a fresh machine.

- **Web app:** `vercel deploy --prod` (from repo root). Env via `vercel env add <K> production`.
- **Engine:** `flyctl deploy -c fly.engine.toml -a sfw-engine` (from repo root). Secrets:
  `grep -E '^(RC_|KV_REST_API_|MONITORED_EXTENSIONS)' .env | flyctl secrets import -a sfw-engine`,
  then `flyctl secrets set -a sfw-engine SOFTPHONE_BRIDGE_URL=http://sfw-softphone-bridge.internal:8787 METRICS_TIMEZONE=America/Los_Angeles SOFTPHONE_BRIDGE_API_KEY=<bridge BRIDGE_API_KEY>`.
- **Bridge:** `cd softphone-bridge && flyctl deploy` (deploy this **before** the engine). Secrets:
  `grep -E '^[A-Z_]+=' softphone-bridge/.env | flyctl secrets import -a sfw-softphone-bridge`.
- Logs: `flyctl logs -a <app>`, `vercel logs <url> --scope sfwc`.

Design/plan detail: `docs/superpowers/specs|plans/2026-07-02-production-deployment*` and
`…-app-password-gate*`.

## Config that lives in code

- **Monitored reps** — `MONITORED_EXTENSIONS` (env). Adding a rep also requires adding them as a
  **Monitored** member of RC call-monitoring group **"Chat GPT Rail" (3932052)**, or `*80` is
  rejected. Monitor identity is Tyler Falcon ext 120.
- **Dashboard Claude buttons** — `Config.CLAUDE_TOOLS` list in `src/config.py`. **Add a tool = one
  line + redeploy** (no endpoint/button/handler changes).
- Timezone for metrics day/week boundaries — `METRICS_TIMEZONE` (default `America/Los_Angeles`,
  week starts Monday).

## Gotchas (all hit during first deploy)

- Fly region `sea` is deprecated → use `sjc`.
- Vercel bundle limit (225 MB): `.vercelignore` hides `pyproject.toml` (→ slim `requirements.txt`,
  not the full engine deps) and `.worktrees/` (huge). `.python-version` pins 3.12.
- `/login` uses FastAPI `Form` → `python-multipart` must be in `requirements.txt`.
- `.env` KV values are **double-quoted**; strip surrounding quotes before `vercel env add` (else the
  Upstash URL loses its `https://` and every Redis call 500s).
- Fly private networking (`.internal`) is **IPv6-only** → the bridge listens on `::` (`BRIDGE_HOST`).
- `fly scale count 1` on a 2-machine app (active + standby) can destroy the **active** machine and
  keep the **standby**, which stays stopped by design → app silently down. Check `fly status` after
  scaling; fix with `fly machine update <id> --standby-for "" -y` then `fly machine start <id>`.

## Deploy topology

The engine (`sfw-engine`) must run **exactly one** Fly machine. Sell-o-meter finalization
(`push_sellometer_final` in `src/redis_store.py`) uses a non-idempotent `LPUSH` — two machines
processing the same cycles would each finalize and push, producing duplicate per-rep history
records. Verify with `fly scale show -a sfw-engine` (config: `fly.engine.toml`) before and after
every deploy.

## Known unverified

**RTP audio from Fly** — the softphone *registers* from the cloud, but whether it streams call audio
→ Deepgram → transcript through Fly's NAT is unproven; needs a live monitored call to confirm. If it
fails: `flyctl ips allocate-v4 -a sfw-softphone-bridge` + pin one machine; if still failing, move the
bridge to a plain VM with a public IP (`network_mode: host`) and keep the engine on Fly.
