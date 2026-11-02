"""The "paste a key and it works" path.

The security assertions matter more than the happy path: the key must never be
returned, never stored in plaintext, and never saved when the provider rejects it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AppSetting
from app.settings_store import K_API_KEY, SettingsStore, mask

FAKE_KEY = "sk-or-v1-" + "a1b2c3d4e5" * 4


@pytest.fixture(autouse=True)
def _clean(db):
    """Start and finish with no stored key so other tests are unaffected."""
    store = SettingsStore(db)
    store.clear_llm(actor="test")
    yield
    store.clear_llm(actor="test")


def test_mask_never_reveals_the_middle():
    masked = mask(FAKE_KEY)
    assert masked.startswith(FAKE_KEY[:8])
    assert masked.endswith(FAKE_KEY[-4:])
    assert FAKE_KEY[10:-4] not in masked


def test_key_is_encrypted_at_rest(db):
    store = SettingsStore(db)
    store.set_raw(K_API_KEY, FAKE_KEY, "test")
    row = db.scalar(select(AppSetting).where(AppSetting.key == K_API_KEY))
    assert row is not None
    assert row.is_encrypted is True
    assert FAKE_KEY not in (row.value or "")
    assert store.get_raw(K_API_KEY) == FAKE_KEY, "the value must round-trip through decryption"


def test_public_view_never_contains_the_key(db):
    store = SettingsStore(db)
    store.save_llm(api_key=FAKE_KEY, actor="test")
    view = store.public_view()
    assert view["configured"] is True
    assert FAKE_KEY not in str(view)
    assert view["masked_key"] == mask(FAKE_KEY)


def test_clearing_falls_back_to_environment(db, monkeypatch):
    store = SettingsStore(db)
    store.save_llm(api_key=FAKE_KEY, actor="test")
    assert store.llm().source == "settings"
    runtime = store.clear_llm(actor="test")
    assert runtime.api_key in (None, "") or runtime.source == "environment"
    assert store.public_view()["masked_key"] in (None, "")


def test_rejected_key_is_not_saved(client):
    response = client.post(
        "/api/v1/settings/llm",
        json={"api_key": "sk-or-v1-definitely-not-a-real-key-000000", "verify": True},
    )
    body = response.json()
    # Either the provider rejected it (expected) or the network is unreachable here.
    if not body.get("saved"):
        assert body["verified"] is False
        assert "message" in body
    view = client.get("/api/v1/settings/llm").json()
    assert view["configured"] is False, "a failed verification must not configure the platform"


def test_force_save_stores_the_key_and_masks_it(client):
    body = client.post(
        "/api/v1/settings/llm",
        json={"api_key": FAKE_KEY, "verify": False, "model_extraction": "test/extract",
              "model_assistant": "test/assistant"},
    ).json()
    assert body["saved"] is True
    view = client.get("/api/v1/settings/llm").json()
    assert view["configured"] is True
    assert view["model_assistant"] == "test/assistant"
    assert FAKE_KEY not in str(view)

    cleared = client.delete("/api/v1/settings/llm").json()
    assert cleared["settings"]["configured"] is False


def test_test_endpoint_handles_a_missing_key(client):
    body = client.post("/api/v1/settings/llm/test", json={}).json()
    assert body["ok"] is False
    assert "paste" in body["message"].lower()


def test_models_endpoint_returns_a_catalogue(client):
    body = client.get("/api/v1/settings/models").json()
    assert body["count"] >= 1
    assert body["models"][0]["id"]
    assert "extraction" in body["suggested"]


def test_settings_page_renders_the_connection_card(client):
    page = client.get("/settings")
    assert page.status_code == 200
    assert "AI connection" in page.text
    assert "API key" in page.text


def test_page_shows_connected_state_after_saving(client):
    client.post("/api/v1/settings/llm", json={"api_key": FAKE_KEY, "verify": False})
    page = client.get("/settings")
    assert "AI connected" in page.text
    assert FAKE_KEY not in page.text, "the key must never be rendered into HTML"
