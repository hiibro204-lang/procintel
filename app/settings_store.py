"""Application settings that the operator can change from the UI.

The LLM API key lives here rather than in an environment variable, because the
whole point is that someone can paste a key into a form and have the platform
start working — no redeploy, no shell access, no editing files on a server.

Security posture, stated plainly:
  * the key is encrypted at rest with a Fernet key held outside the database
    (env `APP_SECRET_KEY`, or a 0600 key file beside the app);
  * it is never written to a log, never included in an API response, and never
    rendered into a template — only a masked form is ever returned;
  * the encryption protects a database dump or a backup, not a compromised host.
    For production, put the secret in a managed secret store and rotate it.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import BASE_DIR, settings
from .models import AppSetting

log = logging.getLogger(__name__)

KEY_FILE = Path(os.environ.get("APP_SECRET_KEY_FILE", str(BASE_DIR / ".secret.key")))

# Setting names
K_API_KEY = "llm.api_key"
K_PROVIDER = "llm.provider"
K_BASE_URL = "llm.base_url"
K_MODEL_EXTRACTION = "llm.model_extraction"
K_MODEL_ASSISTANT = "llm.model_assistant"
K_UPDATED_BY = "llm.updated_by"
K_VERIFIED_AT = "llm.verified_at"

SECRET_KEYS = {K_API_KEY}


class EncryptionUnavailable(RuntimeError):
    """Raised when the encryption dependency or key cannot be loaded."""


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise EncryptionUnavailable(
            "the 'cryptography' package is required to store an API key; "
            "install it with: pip install cryptography"
        ) from exc

    raw = os.environ.get("APP_SECRET_KEY")
    if raw:
        key = raw.encode()
    else:
        if KEY_FILE.exists():
            key = KEY_FILE.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            KEY_FILE.write_bytes(key)
            try:
                KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except OSError:  # pragma: no cover - platform dependent
                log.warning("could not restrict permissions on %s", KEY_FILE)
            log.info("generated a new settings encryption key at %s", KEY_FILE)
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


def mask(secret: str | None) -> str | None:
    """Show enough to recognise a key, never enough to use one."""
    if not secret:
        return None
    if len(secret) <= 10:
        return "•" * len(secret)
    return f"{secret[:8]}{'•' * 12}{secret[-4:]}"


@dataclass
class LlmRuntime:
    """The effective LLM configuration for this request."""

    api_key: str | None
    base_url: str
    model_extraction: str
    model_assistant: str
    source: str          # 'settings' | 'environment' | 'none'
    verified_at: dt.datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


class SettingsStore:
    """Thin, explicit accessor over the app_settings table."""

    def __init__(self, db: Session):
        self.db = db

    # -- raw access ------------------------------------------------------- #
    def get_raw(self, key: str) -> str | None:
        row = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
        if row is None or row.value is None:
            return None
        if row.is_encrypted:
            try:
                return decrypt(row.value)
            except Exception:  # noqa: BLE001 - a rotated key must not crash the app
                log.warning("could not decrypt setting %s (key rotated?)", key)
                return None
        return row.value

    def set_raw(self, key: str, value: str | None, actor: str | None = None, *, encrypted: bool | None = None) -> None:
        row = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
        if row is None:
            row = AppSetting(key=key)
            self.db.add(row)
        if value is None:
            row.value = None
            row.is_encrypted = False
        else:
            should_encrypt = key in SECRET_KEYS if encrypted is None else encrypted
            row.value = encrypt(value) if should_encrypt else value
            row.is_encrypted = should_encrypt
        row.updated_at = dt.datetime.now(dt.timezone.utc)
        row.updated_by = actor
        self.db.add(row)
        self.db.commit()

    # -- LLM configuration ------------------------------------------------ #
    def llm(self) -> LlmRuntime:
        stored_key = self.get_raw(K_API_KEY)
        env_key = settings.openrouter_api_key
        if stored_key:
            api_key, source = stored_key, "settings"
        elif env_key:
            api_key, source = env_key, "environment"
        else:
            api_key, source = None, "none"

        verified_raw = self.get_raw(K_VERIFIED_AT)
        verified_at = None
        if verified_raw:
            try:
                verified_at = dt.datetime.fromisoformat(verified_raw)
            except ValueError:
                verified_at = None

        return LlmRuntime(
            api_key=api_key,
            base_url=self.get_raw(K_BASE_URL) or settings.openrouter_base_url,
            model_extraction=self.get_raw(K_MODEL_EXTRACTION) or settings.extraction_model,
            model_assistant=self.get_raw(K_MODEL_ASSISTANT) or settings.assistant_model,
            source=source,
            verified_at=verified_at,
        )

    def save_llm(
        self,
        *,
        api_key: str | None = None,
        model_extraction: str | None = None,
        model_assistant: str | None = None,
        base_url: str | None = None,
        actor: str | None = None,
        verified: bool = False,
    ) -> LlmRuntime:
        """Save what was provided; leave anything else untouched.

        An empty api_key clears the stored key (falling back to the environment,
        if one is set) — that is how 'disconnect' works.
        """
        if api_key is not None:
            cleaned = api_key.strip()
            self.set_raw(K_API_KEY, cleaned or None, actor)
        if model_extraction is not None and model_extraction.strip():
            self.set_raw(K_MODEL_EXTRACTION, model_extraction.strip(), actor, encrypted=False)
        if model_assistant is not None and model_assistant.strip():
            self.set_raw(K_MODEL_ASSISTANT, model_assistant.strip(), actor, encrypted=False)
        if base_url is not None and base_url.strip():
            self.set_raw(K_BASE_URL, base_url.strip().rstrip("/"), actor, encrypted=False)
        if verified:
            self.set_raw(K_VERIFIED_AT, dt.datetime.now(dt.timezone.utc).isoformat(), actor, encrypted=False)
        if actor:
            self.set_raw(K_UPDATED_BY, actor, actor, encrypted=False)
        return self.llm()

    def clear_llm(self, actor: str | None = None) -> LlmRuntime:
        self.set_raw(K_API_KEY, None, actor)
        self.set_raw(K_VERIFIED_AT, None, actor, encrypted=False)
        return self.llm()

    # -- view model for the UI -------------------------------------------- #
    def public_view(self) -> dict:
        runtime = self.llm()
        return {
            "configured": runtime.configured,
            "source": runtime.source,
            "masked_key": mask(runtime.api_key),
            "base_url": runtime.base_url,
            "model_extraction": runtime.model_extraction,
            "model_assistant": runtime.model_assistant,
            "verified_at": runtime.verified_at.isoformat() if runtime.verified_at else None,
            "encryption": "fernet" if _encryption_available() else "unavailable",
        }


def _encryption_available() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def random_token(n: int = 24) -> str:
    """Used for CSRF-ish form tokens in the UI."""
    return secrets.token_urlsafe(n)
