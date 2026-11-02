"""Effective LLM configuration, key validation and the model catalogue.

The key comes from the settings store (pasted in the UI) and falls back to the
environment. Everything that needs a model — document extraction and the
assistant — goes through `client_for`, so a key saved in the UI takes effect on
the very next request with no restart.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from .config import settings
from .extraction.llm import LlmClient
from .settings_store import LlmRuntime, SettingsStore

log = logging.getLogger(__name__)

_MODEL_CACHE: dict[str, tuple[float, list[dict]]] = {}
MODEL_CACHE_TTL = 600  # seconds

# Shown in the picker even when the catalogue cannot be fetched (offline, blocked).
CURATED_MODELS = [
    {"id": "google/gemini-2.0-flash-001", "name": "Gemini 2.0 Flash — cheap, fast, good at extraction"},
    {"id": "google/gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
    {"id": "anthropic/claude-3.5-haiku", "name": "Claude 3.5 Haiku — strong reasoning, low cost"},
    {"id": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4"},
    {"id": "openai/gpt-4o-mini", "name": "GPT-4o mini"},
    {"id": "openai/gpt-4.1-mini", "name": "GPT-4.1 mini"},
    {"id": "meta-llama/llama-3.3-70b-instruct", "name": "Llama 3.3 70B Instruct"},
    {"id": "mistralai/mistral-small-24b-instruct", "name": "Mistral Small 24B"},
    {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat"},
]


def runtime_for(db: Session) -> LlmRuntime:
    return SettingsStore(db).llm()


def client_for(db: Session, role: str = "extraction") -> LlmClient:
    """Build a client for 'extraction' or 'assistant' using the effective config."""
    runtime = runtime_for(db)
    model = runtime.model_assistant if role == "assistant" else runtime.model_extraction
    return LlmClient(api_key=runtime.api_key, base_url=runtime.base_url, model=model)


def is_configured(db: Session) -> bool:
    return runtime_for(db).configured


@dataclass
class KeyCheck:
    ok: bool
    message: str
    detail: dict | None = None


def validate_key(api_key: str, base_url: str | None = None) -> KeyCheck:
    """Prove the key works by asking the provider who it belongs to.

    Returns a structured result rather than raising: the UI needs to show the
    reason, and a failed check must never leave a half-saved configuration.
    """
    key = (api_key or "").strip()
    if not key:
        return KeyCheck(False, "No key provided.")
    if len(key) < 20:
        return KeyCheck(False, "That looks too short to be an API key.")
    base = (base_url or settings.openrouter_base_url).rstrip("/")

    try:
        with httpx.Client(timeout=20.0) as http:
            resp = http.get(
                f"{base}/key",
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        return KeyCheck(
            False,
            "Could not reach the provider from this server "
            f"({type(exc).__name__}). The key was not saved.",
        )

    if resp.status_code == 401:
        return KeyCheck(False, "The provider rejected this key (401). Check it was copied completely.")
    if resp.status_code == 403:
        return KeyCheck(False, "This key is not permitted to use that endpoint (403).")
    if resp.status_code >= 400:
        return KeyCheck(False, f"The provider returned HTTP {resp.status_code}.")

    data: dict = {}
    try:
        payload = resp.json()
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
    except ValueError:
        data = {}

    label = data.get("label") or "your account"
    usage = data.get("usage")
    limit = data.get("limit")
    bits = [f"Key accepted for {label}."]
    if limit:
        remaining = max(0.0, float(limit) - float(usage or 0))
        bits.append(f"${remaining:,.2f} of ${float(limit):,.2f} remaining.")
    elif usage is not None:
        bits.append(f"${float(usage):,.2f} used so far.")
    return KeyCheck(True, " ".join(bits), {"label": label, "usage": usage, "limit": limit})


def list_models(base_url: str | None = None, api_key: str | None = None, timeout: float = 6.0) -> list[dict]:
    """Model catalogue for the picker, cached briefly. Falls back to the curated list."""
    base = (base_url or settings.openrouter_base_url).rstrip("/")
    cached = _MODEL_CACHE.get(base)
    if cached and time.time() - cached[0] < MODEL_CACHE_TTL:
        return cached[1]

    models: list[dict] = []
    try:
        with httpx.Client(timeout=timeout) as http:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            resp = http.get(f"{base}/models", headers=headers)
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                model_id = item.get("id")
                if not model_id:
                    continue
                pricing = item.get("pricing") or {}
                models.append(
                    {
                        "id": model_id,
                        "name": item.get("name") or model_id,
                        "context_length": item.get("context_length"),
                        "prompt_price": pricing.get("prompt"),
                        "completion_price": pricing.get("completion"),
                    }
                )
    except Exception as exc:  # noqa: BLE001 - a catalogue failure must not break the page
        log.info("model catalogue unavailable (%s); using the curated list", exc)
        return CURATED_MODELS

    if not models:
        return CURATED_MODELS
    models.sort(key=lambda m: (m["name"] or "").lower())
    _MODEL_CACHE[base] = (time.time(), models)
    return models


def suggest_models(models: list[dict]) -> dict:
    """Pick sensible defaults from a catalogue: cheap for bulk, strong for chat."""
    ids = [m["id"] for m in models]
    extraction = next((i for i in ids if "gemini-2.0-flash" in i), None) \
        or next((i for i in ids if "flash" in i and "1.5" not in i), None) \
        or next((i for i in ids if "mini" in i), None) \
        or (ids[0] if ids else settings.extraction_model)
    assistant = next((i for i in ids if "claude-3.5-haiku" in i), None) \
        or next((i for i in ids if "haiku" in i), None) \
        or next((i for i in ids if "gpt-4o-mini" in i), None) \
        or extraction
    return {"extraction": extraction, "assistant": assistant}
