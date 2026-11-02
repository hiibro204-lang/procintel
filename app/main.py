"""Application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db, session_scope
from .registry import sync_sources
from .routers import api, pages, settings as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("procintel")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the schema and load the source registry on boot.

    Both are idempotent, so a restart never destroys data and a first boot on a
    fresh database always produces a runnable system.
    """
    init_db()
    with session_scope() as db:
        result = sync_sources(db)
    log.info("registry synced: %s", result)
    log.info("LLM extraction %s", "enabled" if settings.llm_enabled else "disabled (no API key)")
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "India-wide procurement intelligence: tenders, awarded prices, L1/L2/L3 ladders, "
        "suppliers and buyers. Every figure carries its provenance."
    ),
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def attach_llm_state(request, call_next):
    """Expose the effective LLM configuration to every template.

    The header shows a connected/off chip on every page, so the state has to be
    available globally rather than threaded through each route.
    """
    if request.url.path.startswith("/static"):
        return await call_next(request)
    from .db import session_scope
    from .settings_store import SettingsStore

    try:
        with session_scope() as db:
            request.state.llm = SettingsStore(db).llm()
    except Exception:  # noqa: BLE001 - a settings read must never break a page
        request.state.llm = None
    return await call_next(request)
app.include_router(api.router)
app.include_router(settings_router.router)
app.include_router(pages.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "version": settings.version}
