from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from src.redis_store import CallStore
from src.api.routes import router, hubspot_router, set_store
from src.agreement_tool.routes import agreement_router


_STATIC_DIR = Path(__file__).parent / "static"
_OVERVIEW_HTML = _STATIC_DIR / "overview.html"
_AGREEMENT_HTML = Path(__file__).parent.parent / "agreement_tool" / "static" / "agreement.html"


def create_app(store: CallStore | None = None) -> FastAPI:
    app = FastAPI(title="SFW Call Intelligence Bridge", version="1.0.0")

    if store:
        set_store(store)

    app.include_router(router)
    app.include_router(hubspot_router)
    app.include_router(agreement_router)

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
