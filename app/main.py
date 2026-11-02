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
from .routers import api, pages

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
app.include_router(api.router)
app.include_router(pages.router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "version": settings.version}
