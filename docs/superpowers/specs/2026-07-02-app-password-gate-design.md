# App-Level Shared-Password Gate — Design

**Date:** 2026-07-02
**Status:** Approved

## Problem

The Vercel web app needs a login for the sales team. Vercel's Password
Protection is a paid add-on (greyed out on the Pro plan), and Vercel SSO bills
per seat and requires each rep to have a Vercel account. We want a free,
account-less shared-password gate built into the app.

## Approach

A signed, **stateless** cookie — no session store, so it works across Vercel
serverless invocations. One shared password gates the whole site; once a rep
logs in, everything (pages and the API the pages call) just works.

- **`Config.APP_PASSWORD`** (env `APP_PASSWORD`). The shared password (`sellit`
  in production). If empty/unset, the gate is **disabled** (open) — so local dev
  and tests are unaffected unless the env var is set.
- **Cookie `sfw_session`** = `HMAC-SHA256(key=APP_PASSWORD, msg="sfw-authed")`
  as hex. The password doubles as the signing key, so the middleware validates
  by recomputing and comparing (constant-time). Stateless; rotating the password
  invalidates all sessions. Cookie attributes: `HttpOnly; Secure; SameSite=Lax;
  Path=/; Max-Age=2592000` (30 days).

## Components

### 1. `src/api/session.py` (new)

Pure helpers (unit-tested, no FastAPI):
- `make_token(password: str) -> str` — hex HMAC-SHA256(password, `"sfw-authed"`).
- `valid_token(token: str | None, password: str) -> bool` — constant-time
  compare (`hmac.compare_digest`) of `token` against `make_token(password)`;
  `False` for `None`/empty. If `password` is empty, returns `False` (callers
  gate on APP_PASSWORD separately).

### 2. `src/config.py`

Add `APP_PASSWORD: str = os.environ.get("APP_PASSWORD") or ""`.

### 3. `src/api/auth.py`

`verify_api_key(request: Request)` now authorizes on **either**:
- a valid `x-api-key` header (== `Config.API_KEY`, as today), **or**
- a valid `sfw_session` cookie (`valid_token(cookie, Config.APP_PASSWORD)`).

Raises 401 only if neither holds. Preserves the machine `x-api-key` path;
adds cookie auth for logged-in browsers. Signature changes from a `Header`
param to taking `Request` (all routers use `Depends(verify_api_key)`, unchanged).

### 4. `src/api/main.py`

- **`GET /login`** → serves a small password form (inline HTML; posts to
  `/login`, carries a hidden `next`).
- **`POST /login`** (form field `password`, optional `next`) → if
  `password == Config.APP_PASSWORD` and APP_PASSWORD non-empty: set the
  `sfw_session` cookie and `303` redirect to a **safe** `next` (must start with
  `/` and not `//`, else `/`). Else re-render the form with an error (401).
- **`GET /logout`** → delete the cookie, redirect to `/login`.
- **Redirect middleware** (added in `create_app`): if `Config.APP_PASSWORD` is
  empty → pass everything. Otherwise, for a request that is **not** authenticated
  (no valid cookie): exempt paths `/login`, `/logout`, and any `/api/*` (those
  are handled by `verify_api_key`) → pass through; all other paths (the pages)
  → `302` to `/login?next=<path>`. Authenticated → pass.

### 5. Static pages — drop the key prompt

`overview.html`, `dashboard.html`, `agreement.html`: remove the
`prompt("…API key…")` / `localStorage` key logic. Same-origin fetches send the
`sfw_session` cookie automatically, and `verify_api_key` accepts it, so no
`x-api-key` is needed. Keep sending requests as before minus the key header
(a missing/empty `x-api-key` is fine when the cookie is valid).

### 6. Deployment

- Set `APP_PASSWORD=sellit` in the Vercel project (production env).
- Redeploy.
- Turn **off** Vercel Authentication (SSO) in Deployment Protection so the app's
  own gate is the one in front (otherwise SSO double-gates).

## Data flow

```
rep → GET /overview  (no cookie) ── middleware ──> 302 /login?next=/overview
rep → POST /login (password=sellit) ── set sfw_session cookie ──> 303 /overview
rep → GET /overview (cookie) ── middleware ok ──> page; fetch /api/calls/reps
      (cookie auto-sent) ── verify_api_key(cookie ok) ──> data
```

## Error handling & edge cases

- `APP_PASSWORD` unset → gate fully disabled (local dev / tests default). No
  redirect, `verify_api_key` falls back to `x-api-key` only (as today).
- Wrong password → 401, form re-rendered with an error message; no cookie set.
- Tampered/empty cookie → `valid_token` false → treated as unauthenticated.
- Open-redirect guard: `next` must be a same-site absolute path (`/…`, not
  `//…` or `http…`), else redirect to `/`.
- API requests without cookie/key still 401 via `verify_api_key` (unchanged).

## Testing

- `session.py`: `make_token` deterministic; `valid_token` true for a matching
  token, false for tampered/blank/`None`, false when password empty.
- `verify_api_key`: authorized with a valid cookie and no key; authorized with a
  valid key and no cookie; 401 with neither. (`@patch` `Config.API_KEY` /
  `Config.APP_PASSWORD`.)
- Middleware (via `TestClient`, `APP_PASSWORD` patched): `GET /overview` without
  cookie → 302 to `/login`; `POST /login` with `sellit` sets the cookie and
  redirects; with a wrong password → 401; `GET /overview` with the cookie → 200;
  with `APP_PASSWORD` empty → `/overview` 200 (gate off).

## Out of scope (YAGNI)

Per-user accounts, login rate-limiting, password-rotation UI, "remember me"
toggle (cookie is always 30 days), CSRF token on the login form (single shared
password, low value).
