# Production Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to work Tasks 1–3 (repo changes, TDD). Tasks 4–6 are **interactive deployment runbooks** — they use real secrets, interactive CLI logins, and create billable cloud resources, so they are executed with the user in the loop, NOT by autonomous subagents. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Deploy the web app to Vercel and the always-on engine (monitor + workers + softphone bridge) to Fly.io, verified end-to-end from the terminal.

**Architecture:** Vercel serves the FastAPI app (`api/index.py`). Two Fly apps run the engine (`sfw-engine`, Python `run_engine.py`) and the softphone bridge (`sfw-softphone-bridge`, Node). All three share one Upstash Redis.

**Tech Stack:** Vercel (Python runtime), Fly.io (Docker microVMs), Docker, FastAPI, Node.

## Global Constraints

- Fly org: **`personal`** (Tyler F). Fly CLI: `C:\Users\tfalcon\.fly\bin\flyctl.exe` (already authed as tfalcon@sfwconstruction.com); after a shell restart `flyctl` is also on PATH.
- Engine ↔ bridge is **private** over Fly `.internal` (IPv6-only) → the bridge must listen on `::`. Bridge SIP/RTP is **outbound-only** (no public ports).
- Shared Upstash Redis (`KV_REST_API_URL`/`KV_REST_API_TOKEN`) is the only cross-component channel.
- No secrets in git (`.env`, `softphone-bridge/.env` are gitignored). Secrets reach Fly via `fly secrets import` and Vercel via `vercel env`.
- Run Python tests with `python -m pytest` from repo root. Commit messages: conventional style, **no** `Co-Authored-By: Claude` trailer.

---

### Task 1: Engine app files (`run_engine.py` + Docker/Fly config)

**Files:**
- Create: `run_engine.py`, `requirements-engine.txt`, `Dockerfile.engine`, `fly.engine.toml`, `.dockerignore`
- Test: `tests/test_run_engine.py`

**Interfaces:**
- Produces: `run_engine.build_store() -> CallStore` (raises `RuntimeError` without Upstash creds); `run_engine.build_sidecar() -> SidecarClient | None`; `run_engine.main()` async entrypoint wiring monitor + extraction + metrics.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_engine.py`:

```python
import pytest
from unittest.mock import patch

import run_engine
from src.redis_store import CallStore
from src.sidecar_client import SidecarClient


@patch("run_engine.Config")
def test_build_store_requires_upstash(MockConfig):
    MockConfig.REDIS_URL = ""
    MockConfig.REDIS_TOKEN = ""
    with pytest.raises(RuntimeError):
        run_engine.build_store()


@patch("run_engine.Config")
def test_build_store_with_creds_returns_callstore(MockConfig):
    MockConfig.REDIS_URL = "https://example.upstash.io"
    MockConfig.REDIS_TOKEN = "tok"
    assert isinstance(run_engine.build_store(), CallStore)


@patch("run_engine.Config")
def test_build_sidecar_none_when_unconfigured(MockConfig):
    MockConfig.SOFTPHONE_BRIDGE_URL = ""
    MockConfig.SOFTPHONE_BRIDGE_API_KEY = ""
    assert run_engine.build_sidecar() is None


@patch("run_engine.Config")
def test_build_sidecar_configured(MockConfig):
    MockConfig.SOFTPHONE_BRIDGE_URL = "http://sfw-softphone-bridge.internal:8787"
    MockConfig.SOFTPHONE_BRIDGE_API_KEY = "k"
    assert isinstance(run_engine.build_sidecar(), SidecarClient)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_run_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'run_engine'`

- [ ] **Step 3: Create `run_engine.py`**

```python
"""Production engine entrypoint — monitor + extraction + metrics workers.

No HTTP API (that runs on Vercel). Requires real Upstash (KV_REST_API_*).
"""
import asyncio
import logging

from upstash_redis import Redis

from src.call_monitor import run_monitor
from src.config import Config
from src.extraction_worker import run_extraction_worker
from src.metrics_worker import run_metrics_worker
from src.redis_store import CallStore
from src.sidecar_client import SidecarClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def build_store() -> CallStore:
    if not (Config.REDIS_URL and Config.REDIS_TOKEN):
        raise RuntimeError("Engine requires Upstash Redis (KV_REST_API_URL/TOKEN)")
    return CallStore(Redis(url=Config.REDIS_URL, token=Config.REDIS_TOKEN))


def build_sidecar() -> SidecarClient | None:
    if Config.SOFTPHONE_BRIDGE_URL and Config.SOFTPHONE_BRIDGE_API_KEY:
        return SidecarClient(Config.SOFTPHONE_BRIDGE_URL,
                             Config.SOFTPHONE_BRIDGE_API_KEY)
    return None


async def main():
    store = build_store()
    sidecar = build_sidecar()
    logger.info("Live transcription %s",
                "enabled via " + Config.SOFTPHONE_BRIDGE_URL if sidecar
                else "disabled (no sidecar configured)")
    logger.info("Engine starting: monitor + extraction + metrics")
    await asyncio.gather(
        run_monitor(store, sidecar=sidecar),
        run_extraction_worker(store),
        run_metrics_worker(store),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEngine stopped.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_run_engine.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Create the Docker + Fly config**

`requirements-engine.txt`:

```
ringcentral>=0.9
upstash-redis>=1.1
httpx>=0.28
python-dotenv>=1.0
websockets>=14.0
tzdata>=2025.1
```

`Dockerfile.engine`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements-engine.txt .
RUN pip install --no-cache-dir -r requirements-engine.txt
COPY src ./src
COPY run_engine.py .
CMD ["python", "run_engine.py"]
```

`.dockerignore`:

```
.git
.venv
__pycache__
*.pyc
.env
softphone-bridge
docs
tests
scripts
*.md
```

`fly.engine.toml`:

```toml
app = "sfw-engine"
primary_region = "sea"

[build]
  dockerfile = "Dockerfile.engine"

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
```

- [ ] **Step 6: Commit**

```bash
git add run_engine.py requirements-engine.txt Dockerfile.engine fly.engine.toml .dockerignore tests/test_run_engine.py
git commit -m "feat(deploy): run_engine.py entrypoint + Fly engine Docker config"
```

---

### Task 2: Bridge app files (IPv6 bind + Docker/Fly config)

**Files:**
- Modify: `softphone-bridge/src/config.ts`, `softphone-bridge/src/index.ts`
- Create: `softphone-bridge/Dockerfile`, `softphone-bridge/fly.toml`, `softphone-bridge/.dockerignore`

**Interfaces:**
- Produces: bridge listens on `config.bridge.host` (default `::`) so `sfw-engine` can reach `/sessions` over `.internal`.

- [ ] **Step 1: Make the listen host configurable (default `::`)**

In `softphone-bridge/src/config.ts`, in the `bridge` object, add a `host`:

```ts
  bridge: {
    host: process.env.BRIDGE_HOST || "::",
    port: Number(process.env.BRIDGE_PORT || 8787),
    apiKey: required("BRIDGE_API_KEY"),
  },
```

In `softphone-bridge/src/index.ts`, change the listen call:

```ts
await server.listen({ host: config.bridge.host, port: config.bridge.port });
```

- [ ] **Step 2: Typecheck**

Run: `cd softphone-bridge && npm run typecheck`
Expected: no errors.

- [ ] **Step 3: Create the Docker + Fly config**

`softphone-bridge/Dockerfile` (full `node:24` image — the bridge has native deps that need build tools):

```dockerfile
FROM node:24
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build
CMD ["node", "dist/index.js"]
```

`softphone-bridge/fly.toml` (no public services — SIP/RTP is outbound, HTTP is private via `.internal`):

```toml
app = "sfw-softphone-bridge"
primary_region = "sea"

[build]

[env]
  BRIDGE_PORT = "8787"
  BRIDGE_HOST = "::"

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
```

`softphone-bridge/.dockerignore`:

```
node_modules
dist
.env
.git
```

- [ ] **Step 4: Commit**

```bash
git add softphone-bridge/src/config.ts softphone-bridge/src/index.ts softphone-bridge/Dockerfile softphone-bridge/fly.toml softphone-bridge/.dockerignore
git commit -m "feat(deploy): bridge listens on :: + Fly Docker config"
```

---

### Task 3: Vercel catch-all rewrite

**Files:**
- Modify: `vercel.json`

- [ ] **Step 1: Replace the rewrite with a catch-all**

`vercel.json`:

```json
{
  "rewrites": [
    { "source": "/(.*)", "destination": "/api/index" }
  ]
}
```

- [ ] **Step 2: Sanity-check the app imports under the Vercel entrypoint**

Run: `python -c "import api.index; print('vercel entrypoint OK')"`
Expected: `vercel entrypoint OK`

- [ ] **Step 3: Commit**

```bash
git add vercel.json
git commit -m "fix(deploy): route all paths to the FastAPI function on Vercel"
```

---

### Task 4: Deploy the web app to Vercel (interactive runbook)

**Prereq:** `.env` holds the production values. Vercel CLI installed + logged in.

- [ ] **Step 1: Install + authenticate the Vercel CLI** (interactive — user runs)

```
! npm i -g vercel
! vercel login
```
Expected: `> Success! ... logged in`.

- [ ] **Step 2: Link the project**

Run: `vercel link --yes` (creates/links a Vercel project in the current dir)
Expected: `Linked to <scope>/<project>`.

- [ ] **Step 3: Push production env vars from `.env`**

For each key the web app needs, add it to the production environment (values read from `.env`, never printed):

```bash
for K in KV_REST_API_URL KV_REST_API_TOKEN CALL_BRIDGE_API_KEY \
  HUBSPOT_PRIVATE_APP_TOKEN HUBSPOT_PORTAL_ID HUBSPOT_SCOPE_PROPERTY \
  ANTHROPIC_API_KEY ANTHROPIC_MODEL CALLRAIL_API_KEY CALLRAIL_ACCOUNT_ID \
  MONITORED_EXTENSIONS SALES_SCRIPT_CLAUDE_URL; do
  V=$(grep -E "^${K}=" .env | cut -d= -f2-)
  if [ -n "$V" ]; then printf '%s' "$V" | vercel env add "$K" production --force; fi
done
```
Expected: each `Added Environment Variable ... to Production`.
(`ANTHROPIC_MODEL`/`HUBSPOT_SCOPE_PROPERTY` may be blank — the code has defaults; skipping them is fine.)

- [ ] **Step 4: Deploy to production**

Run: `vercel deploy --prod`
Expected: a `https://<project>.vercel.app` URL; build succeeds.

- [ ] **Step 5: Verify the app serves (catch-all + API)**

```bash
vercel deploy --prod    # prints URL; export it as $URL
KEY=$(grep -E '^CALL_BRIDGE_API_KEY=' .env | cut -d= -f2-)
curl -s -o /dev/null -w "overview %{http_code}\n" "$URL/overview"
curl -s -H "x-api-key: $KEY" "$URL/api/calls/config"
```
Expected: `overview 200` (or 401 if Password Protection is already on), and the `/api/calls/config` JSON.

- [ ] **Step 6: Enable Password Protection** (dashboard step — requires Vercel Pro)

In Vercel → Project → Settings → **Deployment Protection** → enable **Password Protection**, set a shared password, save. If on the Hobby plan, skip this step and rely on the app `x-api-key` for v1 (note this to the user).

- [ ] **Step 7: Note completion**

No repo commit (deployment is out-of-repo). Record the production URL for the sales team.

---

### Task 5: Deploy the engine + bridge to Fly (interactive runbook)

**Prereq:** Fly CLI authed (done). Use `flyctl` (full path `C:\Users\tfalcon\.fly\bin\flyctl.exe` if PATH not refreshed). Deploy the **bridge first** so the engine can reach it.

- [ ] **Step 1: Create the two Fly apps (no deploy yet)**

```
flyctl apps create sfw-softphone-bridge -o personal
flyctl apps create sfw-engine -o personal
```
Expected: `New app created: ...` for each.

- [ ] **Step 2: Import bridge secrets from `softphone-bridge/.env`**

```bash
grep -E '^[A-Z_]+=' softphone-bridge/.env | flyctl secrets import -a sfw-softphone-bridge
```
Expected: `Secrets are staged for the first deployment`.

- [ ] **Step 3: Deploy the bridge**

Run: `cd softphone-bridge && flyctl deploy -a sfw-softphone-bridge`
Expected: build + push succeed; a Machine starts. Then:

```
flyctl logs -a sfw-softphone-bridge
```
Expected: `[bridge] softphone registered` and `HTTP listening on :8787`.

- [ ] **Step 4: Import engine secrets + set Fly-specific overrides**

```bash
grep -E '^(RC_|KV_REST_API_|MONITORED_EXTENSIONS)' .env | flyctl secrets import -a sfw-engine
flyctl secrets set -a sfw-engine \
  SOFTPHONE_BRIDGE_URL="http://sfw-softphone-bridge.internal:8787" \
  METRICS_TIMEZONE="America/Los_Angeles" \
  SOFTPHONE_BRIDGE_API_KEY="$(grep -E '^BRIDGE_API_KEY=' softphone-bridge/.env | cut -d= -f2-)"
```
Expected: secrets staged. (The bridge's `BRIDGE_API_KEY` is the engine's `SOFTPHONE_BRIDGE_API_KEY` — they must match.)

- [ ] **Step 5: Deploy the engine**

Run: `flyctl deploy -c fly.engine.toml -a sfw-engine` (from repo root)
Expected: build succeeds; Machine starts. Then:

```
flyctl logs -a sfw-engine
```
Expected: `Authenticated with RingCentral`, `Persisted roster for 4 monitored rep(s)`, `Metrics refreshed for 4 rep(s)`.

- [ ] **Step 6: Confirm private engine → bridge connectivity**

```
flyctl ssh console -a sfw-engine -C "python -c \"import httpx,os; print(httpx.get('http://sfw-softphone-bridge.internal:8787/health', headers={'x-bridge-key': os.environ['SOFTPHONE_BRIDGE_API_KEY']}).text)\""
```
Expected: `{"ok":true,"active":[]}` — proves the `::` bind + `.internal` path work.

---

### Task 6: End-to-end verification (the RTP proof)

- [ ] **Step 1: Place a real call to a monitored rep**

With `flyctl logs -a sfw-engine` and `flyctl logs -a sfw-softphone-bridge` both tailing, have a rep (e.g. Doug, ext 119) take or make a call.
Expected engine log: `Supervision START s-... (rep 576959052, ext number 119)`.
Expected bridge log: `[sup:...] attempt 1/...`, `[deepgram] socket open`, `audio packets: N` climbing, `[deepgram] ... Results` with transcript text.

- [ ] **Step 2: Confirm the transcript reached Redis + the dashboard**

```bash
KEY=$(grep -E '^CALL_BRIDGE_API_KEY=' .env | cut -d= -f2-)
curl -s -H "x-api-key: $KEY" "$URL/api/calls/latest?rep=576959052" | python -m json.tool
```
Expected: JSON with a non-empty `transcript`. Also open `$URL/dashboard?rep=576959052` and confirm the live transcript renders.

- [ ] **Step 3: If audio packets stay at 0 / no transcript — apply the RTP fallback**

Symmetric-RTP-through-Fly-NAT failure. In order:
1. Pin one Machine + allocate a dedicated IPv4 for the bridge:
   ```
   flyctl scale count 1 -a sfw-softphone-bridge
   flyctl ips allocate-v4 -a sfw-softphone-bridge
   ```
   Redeploy, retry Step 1.
2. If still failing, the bridge (only) moves off Fly to a plain VM with a public IP and `network_mode: host` (the engine stays on Fly, reaching the bridge over the VM's address instead of `.internal`). This is a separate follow-up; stop and report to the user with the `flyctl logs` evidence.

- [ ] **Step 4: Report**

Summarize: Vercel URL, both Fly apps healthy, end-to-end transcript verified (or the RTP fallback taken). No repo commit for deployment.

---

## Self-Review Notes

- **Spec coverage:** Vercel catch-all + deps + env + auth (Tasks 3,4); `run_engine.py` (Task 1); engine Docker/Fly (Task 1); bridge `::` bind + Docker/Fly (Task 2); shared Upstash (secrets steps); RTP risk + fallback (Task 6). All spec sections map to a task.
- **Placeholder scan:** none; commands and files are complete.
- **Type consistency:** `build_store`/`build_sidecar`/`main` in `run_engine.py`; `config.bridge.host` ↔ `BRIDGE_HOST` ↔ `::`; `SOFTPHONE_BRIDGE_URL=http://sfw-softphone-bridge.internal:8787` and matching `SOFTPHONE_BRIDGE_API_KEY`/`BRIDGE_API_KEY` used consistently.
- **Ordering:** bridge deploys before engine (engine depends on the bridge's `.internal` address).
- **Interactive/consequential:** Tasks 4–6 use real secrets, interactive logins, and billable resources — executed with the user, with consent at execution time.
