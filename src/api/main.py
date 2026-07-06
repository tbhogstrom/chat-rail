from pathlib import Path
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from src.redis_store import CallStore
from src.api.routes import router, hubspot_router, set_store, set_platform
from src.agreement_tool.routes import agreement_router
from src.config import Config
from src.api.session import make_token, valid_token


_STATIC_DIR = Path(__file__).parent / "static"
_OVERVIEW_HTML = _STATIC_DIR / "overview.html"
_AGREEMENT_HTML = Path(__file__).parent.parent / "agreement_tool" / "static" / "agreement.html"


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


def create_app(store: CallStore | None = None, platform=None) -> FastAPI:
    app = FastAPI(title="SFW Call Intelligence Bridge", version="1.0.0")

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if Config.APP_PASSWORD:
            path = request.url.path
            exempt = path in ("/login", "/logout") or path.startswith("/api/")
            if not exempt and not valid_token(request.cookies.get("sfw_session"),
                                              Config.APP_PASSWORD):
                return RedirectResponse(url=f"/login?next={path}", status_code=302)
        return await call_next(request)

    if store:
        set_store(store)

    if platform:
        set_platform(platform)

    app.include_router(router)
    app.include_router(hubspot_router)
    app.include_router(agreement_router)

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

    @app.get("/")
    def overview() -> FileResponse:
        return FileResponse(_OVERVIEW_HTML)

    @app.get("/dashboard")
    def dashboard() -> FileResponse:
        return FileResponse(_STATIC_DIR / "dashboard.html")

    @app.get("/agreement")
    def agreement() -> FileResponse:
        return FileResponse(_AGREEMENT_HTML)

    return app
