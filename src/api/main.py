from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from src.redis_store import CallStore
from src.api.routes import router, hubspot_router, set_store


_STATIC_DIR = Path(__file__).parent / "static"


def create_app(store: CallStore | None = None) -> FastAPI:
    app = FastAPI(title="SFW Call Intelligence Bridge", version="1.0.0")

    if store:
        set_store(store)

    app.include_router(router)
    app.include_router(hubspot_router)

    @app.get("/dashboard")
    def dashboard() -> FileResponse:
        return FileResponse(_STATIC_DIR / "dashboard.html")

    return app
