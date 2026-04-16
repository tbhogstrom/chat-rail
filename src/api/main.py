from fastapi import FastAPI
from src.redis_store import CallStore
from src.api.routes import router, set_store


def create_app(store: CallStore | None = None) -> FastAPI:
    app = FastAPI(title="SFW Call Intelligence Bridge", version="1.0.0")

    if store:
        set_store(store)

    app.include_router(router)
    return app
