"""Settings API — the "paste a key and it works" path.

Nothing here ever returns the key. The masked form is the only representation
that leaves the process, and every write is validated against the provider first
so a bad key cannot silently become the configured key.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..llm_config import list_models, suggest_models, validate_key
from ..settings_store import SettingsStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


class LlmConfigIn(BaseModel):
    api_key: str | None = Field(default=None, description="Leave empty to keep the stored key")
    model_extraction: str | None = None
    model_assistant: str | None = None
    base_url: str | None = None
    verify: bool = True
    force: bool = False


class TestIn(BaseModel):
    api_key: str | None = None
    base_url: str | None = None


@router.get("/llm")
def get_llm_settings(db: Session = Depends(get_db)):
    return SettingsStore(db).public_view()


@router.post("/llm/test")
def test_key(body: TestIn, db: Session = Depends(get_db)):
    """Check a key without saving it. Uses the stored key when none is supplied."""
    store = SettingsStore(db)
    runtime = store.llm()
    key = (body.api_key or "").strip() or runtime.api_key
    if not key:
        return {"ok": False, "message": "No key to test — paste one first."}
    check = validate_key(key, body.base_url or runtime.base_url)
    return {"ok": check.ok, "message": check.message, "detail": check.detail}


@router.post("/llm")
def save_llm_settings(body: LlmConfigIn, db: Session = Depends(get_db)):
    store = SettingsStore(db)
    runtime = store.llm()
    candidate = (body.api_key or "").strip() or runtime.api_key
    base_url = (body.base_url or runtime.base_url or "").strip() or None

    check_message = None
    verified = False
    if body.verify and candidate:
        check = validate_key(candidate, base_url)
        check_message = check.message
        verified = check.ok
        if not check.ok and not body.force:
            return {
                "saved": False,
                "verified": False,
                "message": check.message,
                "can_force": True,
                "settings": store.public_view(),
            }

    runtime = store.save_llm(
        api_key=(body.api_key if body.api_key is not None else None),
        model_extraction=body.model_extraction,
        model_assistant=body.model_assistant,
        base_url=base_url,
        actor="ui",
        verified=verified,
    )
    log.info("LLM settings updated (configured=%s, verified=%s)", runtime.configured, verified)
    return {
        "saved": True,
        "verified": verified,
        "message": check_message or ("Saved." if runtime.configured else "Key cleared."),
        "settings": store.public_view(),
    }


@router.delete("/llm")
def clear_llm_settings(db: Session = Depends(get_db)):
    store = SettingsStore(db)
    runtime = store.clear_llm(actor="ui")
    return {
        "cleared": True,
        "settings": store.public_view(),
        "message": "Key removed." if not runtime.configured else
                   "Stored key removed; falling back to the key set in the environment.",
    }


@router.get("/models")
def models(db: Session = Depends(get_db), refresh: bool = False):
    store = SettingsStore(db)
    runtime = store.llm()
    catalogue = list_models(runtime.base_url, runtime.api_key)
    return {
        "models": catalogue,
        "suggested": suggest_models(catalogue),
        "current": {
            "extraction": runtime.model_extraction,
            "assistant": runtime.model_assistant,
        },
        "count": len(catalogue),
    }
