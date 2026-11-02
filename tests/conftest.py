"""Test configuration.

Environment is set BEFORE any app module is imported, because settings are read
at import time. Tests run on SQLite in a temporary directory and never touch the
network: LLM calls are disabled by clearing the API key, and connectors are
exercised through their fixtures.
"""
from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="procintel-tests-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMP, 'test.db')}")
os.environ.setdefault("ARCHIVE_DIR", os.path.join(_TMP, "archive"))
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ["MIN_N_FOR_STATS"] = "3"
os.environ["MIN_N_FOR_DISTRIBUTION"] = "8"

import pytest  # noqa: E402

from app import entities  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import Base, Source  # noqa: E402
from app.registry import sync_sources  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    init_db()
    with SessionLocal() as db:
        sync_sources(db)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def gem_source(db):
    return db.query(Source).filter(Source.key == "gem_results").one()


@pytest.fixture(scope="session")
def demo_corpus():
    """A small deterministic corpus, seeded once for the whole test session."""
    from scripts.seed_demo import seed

    with SessionLocal() as session:
        counts = seed(session, n_tenders=12, seed_value=7)
        entities.ensure_taxonomy(session)
    return counts


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
