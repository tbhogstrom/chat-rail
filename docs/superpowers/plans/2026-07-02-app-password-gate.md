# App-Level Shared-Password Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Tasks 1–4 are TDD repo changes; Task 5 is a deploy runbook (interactive). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Gate the whole web app behind one shared password using a signed stateless cookie, with no separate `x-api-key` prompt for reps.

**Architecture:** A password (`APP_PASSWORD`) signs an HMAC cookie (`sfw_session`). Login routes set it; middleware redirects unauthenticated page loads to `/login`; `verify_api_key` accepts the cookie or the `x-api-key`.

**Tech Stack:** FastAPI/Starlette, `hmac`/`hashlib` (stdlib), vanilla HTML.

## Global Constraints

- Password source `Config.APP_PASSWORD` (env `APP_PASSWORD`); **empty ⇒ gate disabled** (local/tests default open).
- Cookie `sfw_session` = `HMAC-SHA256(APP_PASSWORD, "sfw-authed")` hex; `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=2592000`.
- Protected page paths: `/`, `/dashboard`, `/agreement`. Exempt from redirect: `/login`, `/logout`, `/api/*`.
- Constant-time compares (`hmac.compare_digest`). Open-redirect guard on `next` (must start `/`, not `//`).
- Run tests with `python -m pytest`. Commits: conventional, **no** `Co-Authored-By` trailer.

---

### Task 1: `session.py` token helpers + `APP_PASSWORD` config

**Files:**
- Create: `src/api/session.py`
- Modify: `src/config.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Produces: `make_token(password: str) -> str`; `valid_token(token: str | None, password: str) -> bool`; `Config.APP_PASSWORD: str`.

- [ ] **Step 1: Write the failing tests** — create `tests/test_session.py`:

```python
from src.api.session import make_token, valid_token


def test_make_token_is_deterministic_and_password_specific():
    assert make_token("sellit") == make_token("sellit")
    assert make_token("sellit") != make_token("other")


def test_valid_token_accepts_matching():
    assert valid_token(make_token("sellit"), "sellit") is True


def test_valid_token_rejects_tampered_blank_and_none():
    assert valid_token(make_token("sellit") + "x", "sellit") is False
    assert valid_token("", "sellit") is False
    assert valid_token(None, "sellit") is False


def test_valid_token_false_when_password_empty():
    assert valid_token(make_token(""), "") is False
```

- [ ] **Step 2: Run — expect fail**

Run: `python -m pytest tests/test_session.py -q`
Expected: FAIL (`ModuleNotFoundError: src.api.session`).

- [ ] **Step 3: Implement** — create `src/api/session.py`:

```python
"""Signed stateless session token for the shared-password gate."""
import hashlib
import hmac

_MSG = b"sfw-authed"


def make_token(password: str) -> str:
    """Hex HMAC-SHA256 of a fixed message keyed by the shared password."""
    return hmac.new(password.encode(), _MSG, hashlib.sha256).hexdigest()


def valid_token(token: str | None, password: str) -> bool:
    """True iff `token` matches `make_token(password)` (constant-time).

    False for missing tokens or an empty password (gate treated as off).
    """
    if not token or not password:
        return False
    return hmac.compare_digest(token, make_token(password))
```

Add to `src/config.py` inside `class Config` (after `SALES_SCRIPT_CLAUDE_URL`):

```python
    # Shared password gating the web app. Empty = gate disabled.
    APP_PASSWORD: str = os.environ.get("APP_PASSWORD") or ""
```

- [ ] **Step 4: Run — expect pass**

Run: `python -m pytest tests/test_session.py -q`
Expected: PASS (4).

- [ ] **Step 5: Commit**

```bash
git add src/api/session.py src/config.py tests/test_session.py
git commit -m "feat(auth): session token helpers + APP_PASSWORD config"
```

---

### Task 2: `verify_api_key` accepts cookie or key

**Files:**
- Modify: `src/api/auth.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `valid_token` (Task 1).
- Produces: `verify_api_key(request: Request)` — passes on a valid `x-api-key` **or** a valid `sfw_session` cookie; else 401.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api.py`:

```python
@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.config.Config.APP_PASSWORD", "sellit")
def test_session_cookie_authorizes_api_without_key(client):
    from src.api.session import make_token
    client.cookies.set("sfw_session", make_token("sellit"))
    r = client.get("/api/calls/active")   # no x-api-key header
    assert r.status_code == 200
    client.cookies.clear()


@patch("src.api.auth.Config.API_KEY", API_KEY)
@patch("src.config.Config.APP_PASSWORD", "sellit")
def test_bad_cookie_and_no_key_rejected(client):
    from src.api.session import make_token
    client.cookies.set("sfw_session", make_token("wrong"))
    r = client.get("/api/calls/active")
    assert r.status_code == 401
    client.cookies.clear()
```

- [ ] **Step 2: Run — expect fail**

Run: `python -m pytest tests/test_api.py -k "session_cookie or bad_cookie" -v`
Expected: FAIL (cookie path not implemented → 401 on the first test).

- [ ] **Step 3: Implement** — replace `src/api/auth.py` entirely:

```python
from fastapi import Request, HTTPException
from src.config import Config
from src.api.session import valid_token


def verify_api_key(request: Request) -> None:
    """Authorize a request via the x-api-key header OR a valid session cookie."""
    key = request.headers.get("x-api-key")
    if key is not None and key == Config.API_KEY:
        return
    if valid_token(request.cookies.get("sfw_session"), Config.APP_PASSWORD):
        return
    raise HTTPException(status_code=401, detail="Invalid API key")
```

- [ ] **Step 4: Run — expect pass (new + all existing auth tests)**

Run: `python -m pytest tests/test_api.py -q`
Expected: PASS (all — existing `x-api-key` tests still hold since `APP_PASSWORD` defaults empty there).

- [ ] **Step 5: Commit**

```bash
git add src/api/auth.py tests/test_api.py
git commit -m "feat(auth): verify_api_key accepts session cookie or x-api-key"
```

---

### Task 3: Login/logout routes + redirect middleware

**Files:**
- Modify: `src/api/main.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `make_token` (Task 1), `Config.APP_PASSWORD`.
- Produces: `GET/POST /login`, `GET /logout`, and an HTTP middleware redirecting unauthenticated page loads to `/login?next=<path>`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_api.py`:

```python
@patch("src.config.Config.APP_PASSWORD", "sellit")
def test_page_without_cookie_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login?next=/"


@patch("src.config.Config.APP_PASSWORD", "sellit")
def test_login_wrong_password_returns_401(client):
    r = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert r.status_code == 401


@patch("src.config.Config.APP_PASSWORD", "sellit")
def test_login_sets_cookie_and_redirects(client):
    r = client.post("/login", data={"password": "sellit", "next": "/dashboard"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert "sfw_session=" in r.headers.get("set-cookie", "")


@patch("src.config.Config.APP_PASSWORD", "sellit")
def test_login_open_redirect_blocked(client):
    r = client.post("/login", data={"password": "sellit", "next": "//evil.com"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


@patch("src.config.Config.APP_PASSWORD", "sellit")
def test_page_with_valid_cookie_ok(client):
    from src.api.session import make_token
    client.cookies.set("sfw_session", make_token("sellit"))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    client.cookies.clear()


def test_gate_disabled_when_password_empty(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
```

- [ ] **Step 2: Run — expect fail**

Run: `python -m pytest tests/test_api.py -k "login or redirect or gate_disabled or valid_cookie" -v`
Expected: FAIL (no `/login` route, no middleware).

- [ ] **Step 3: Implement** — edit `src/api/main.py`. Add imports at top:

```python
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from src.config import Config
from src.api.session import make_token, valid_token
```

Add helpers above `create_app`:

```python
def _safe_next(next_url: str | None) -> str:
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _login_page(next_url: str, error: str) -> str:
    err = f'<p class="err">{error}</p>' if error else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>SFW Call Bridge — Sign in</title><style>
body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1320;color:#e6ecff;font-family:-apple-system,Segoe UI,Roboto,sans-serif}}
form{{background:#1a2036;border:1px solid #2a3252;border-radius:10px;padding:28px;width:300px}}
h1{{font-size:16px;margin:0 0 16px}}input{{width:100%;box-sizing:border-box;padding:10px;
border-radius:6px;border:1px solid #2a3252;background:#0f1320;color:#e6ecff;font-size:14px}}
button{{width:100%;margin-top:12px;padding:10px;border:none;border-radius:6px;background:#7c9cff;
color:#0a0f1f;font-weight:600;font-size:14px;cursor:pointer}}.err{{color:#f87171;font-size:13px}}
</style></head><body><form method="post" action="/login">
<h1>SFW Call Bridge</h1>{err}
<input type="password" name="password" placeholder="Password" autofocus>
<input type="hidden" name="next" value="{next_url}">
<button type="submit">Sign in</button></form></body></html>"""
```

Inside `create_app`, after `app = FastAPI(...)` (and before the router includes), add the middleware:

```python
    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if Config.APP_PASSWORD:
            path = request.url.path
            exempt = path in ("/login", "/logout") or path.startswith("/api/")
            if not exempt and not valid_token(request.cookies.get("sfw_session"),
                                              Config.APP_PASSWORD):
                return RedirectResponse(url=f"/login?next={path}", status_code=302)
        return await call_next(request)
```

Inside `create_app`, alongside the page routes, add:

```python
    @app.get("/login")
    def login_form(next: str = "/") -> HTMLResponse:
        return HTMLResponse(_login_page(_safe_next(next), ""))

    @app.post("/login")
    def login_submit(password: str = Form(""), next: str = Form("/")):
        if Config.APP_PASSWORD and password == Config.APP_PASSWORD:
            resp = RedirectResponse(url=_safe_next(next), status_code=303)
            resp.set_cookie("sfw_session", make_token(Config.APP_PASSWORD),
                            max_age=2592000, httponly=True, secure=True,
                            samesite="lax", path="/")
            return resp
        return HTMLResponse(_login_page(_safe_next(next), "Incorrect password"),
                            status_code=401)

    @app.get("/logout")
    def logout() -> RedirectResponse:
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie("sfw_session", path="/")
        return resp
```

- [ ] **Step 4: Run — expect pass**

Run: `python -m pytest tests/test_api.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/api/main.py tests/test_api.py
git commit -m "feat(auth): /login + /logout + redirect middleware"
```

---

### Task 4: Drop the `x-api-key` prompt from the pages

**Files:**
- Modify: `src/api/static/overview.html`, `src/api/static/dashboard.html`, `src/agreement_tool/static/agreement.html`

- [ ] **Step 1: overview.html** — replace the API-key bootstrap:

Replace:
```javascript
    let apiKey = localStorage.getItem("sfw-bridge-key");
    if (!apiKey) {
      apiKey = prompt("Enter your SFW Bridge API key (x-api-key):") || "";
      if (apiKey) localStorage.setItem("sfw-bridge-key", apiKey);
    }
    const authHeaders = { "x-api-key": apiKey };
```
with:
```javascript
    // Auth is the shared-password session cookie (sent automatically, same-origin).
    const authHeaders = {};
```

- [ ] **Step 2: dashboard.html** — remove the prompt block:

Delete:
```javascript
    let apiKey = localStorage.getItem("sfw-bridge-key");
    if (!apiKey) {
      apiKey = prompt("Enter your SFW Bridge API key (x-api-key):") || "";
      if (apiKey) localStorage.setItem("sfw-bridge-key", apiKey);
    }
```
and change the `authHeaders` helper to drop the key:
```javascript
    function authHeaders(extra = {}) {
      return { "content-type": "application/json", ...extra };
    }
```

- [ ] **Step 3: agreement.html** — remove the `apiKey()` prompt function and the `"x-api-key": apiKey()` header entry so the fetch headers are just `{ "content-type": "application/json" }`. (Read the file for exact surrounding text; the prompt lives in a `function apiKey()` and is referenced in the fetch `headers`.)

- [ ] **Step 4: Verify pages still serve + suite green**

Run: `python -m pytest -q`
Expected: PASS (all). Then a local smoke test with the gate on:

```bash
APP_PASSWORD=sellit python -c "
from fastapi.testclient import TestClient
from src.api.main import create_app
c = TestClient(create_app())
print('/', c.get('/', follow_redirects=False).status_code)          # 302
print('login', c.post('/login', data={'password':'sellit'}, follow_redirects=False).status_code)  # 303
"
```
Expected: `/ 302` then `login 303`.

- [ ] **Step 5: Commit**

```bash
git add src/api/static/overview.html src/api/static/dashboard.html src/agreement_tool/static/agreement.html
git commit -m "feat(auth): drop x-api-key prompt; pages rely on session cookie"
```

---

### Task 5: Deploy (interactive runbook)

- [ ] **Step 1: Set the password on Vercel**

```bash
printf '%s' "sellit" | vercel env add APP_PASSWORD production --force 2>/dev/null || printf '%s' "sellit" | vercel env add APP_PASSWORD production
```
Expected: `Added Environment Variable APP_PASSWORD to Production`.

- [ ] **Step 2: Deploy**

Run: `vercel deploy --prod --yes`
Expected: a production URL; build succeeds.

- [ ] **Step 3: Turn OFF Vercel Authentication (SSO)**

In `https://vercel.com/sfwc/callrail-chatgpt/settings/deployment-protection`, disable **Vercel Authentication** and save (our app gate replaces it).

- [ ] **Step 4: Verify the gate end-to-end**

```bash
URL=https://callrail-chatgpt-sfwc.vercel.app
curl -s -o /dev/null -w "/ -> %{http_code}\n" "$URL/"                 # 302 to /login
curl -s -o /dev/null -w "/login -> %{http_code}\n" "$URL/login"       # 200
# login, capture cookie, then load / with it:
curl -s -c cj.txt -o /dev/null -X POST "$URL/login" --data "password=sellit&next=/"
curl -s -b cj.txt -o /dev/null -w "/ with cookie -> %{http_code}\n" "$URL/"   # 200
curl -s -b cj.txt "$URL/api/calls/config"                            # JSON (cookie auth)
rm -f cj.txt
```
Expected: `/ -> 302`, `/login -> 200`, `/ with cookie -> 200`, and the config JSON.

- [ ] **Step 5: Report** the production URL + that the shared-password gate is live.

---

## Self-Review Notes

- **Spec coverage:** `session.py` + `APP_PASSWORD` (Task 1); `verify_api_key` cookie-or-key (Task 2); login/logout + middleware + safe `next` (Task 3); page prompt removal (Task 4); Vercel env + deploy + SSO-off + verify (Task 5). All spec sections mapped.
- **Placeholder scan:** none (Task 4 Step 3 references reading agreement.html for exact text — that's an intentional read, the transformation is fully specified).
- **Type consistency:** `make_token`/`valid_token`, cookie `sfw_session`, `Config.APP_PASSWORD`, `_safe_next`, exempt `/api/*`/`/login`/`/logout` — consistent across tasks.
- **Test nuance:** cookie-present tests set `client.cookies.set(...)` directly (a `Secure` cookie won't round-trip over TestClient's http); login tests assert on the `set-cookie` response header, not a round-trip.
